#!/usr/bin/env python3
"""Bridge the Surface Laptop 7 IPU7 raw-Bayer capture to a v4l2loopback device.

Sets up the media pipeline, configures sensor gains, then captures raw Bayer
from /dev/video0 via `v4l2-ctl --stream-mmap` (as a subprocess, since the direct
python3-v4l2 ioctl path has buffer-index round-trip bugs). Debayers + AWB +
gamma in OpenCV, writes I420 to /dev/video42.
"""
import ctypes
import fcntl
import os
import signal
import subprocess
import time
import numpy as np
import cv2
from v4l2 import (
    v4l2_format,
    V4L2_BUF_TYPE_VIDEO_OUTPUT,
    V4L2_PIX_FMT_YUV420,
    V4L2_FIELD_NONE,
    VIDIOC_S_FMT,
)

MEDIA = "/dev/media0"
CAPTURE = "/dev/video0"
OUTPUT = "/dev/video42"
WIDTH = 1928
HEIGHT = 1092
STRIDE = 3904                   # bytes per line (1952 pixels × 2 bytes for SGRBG10)
FRAME_SIZE = STRIDE * HEIGHT    # 4,263,168 bytes
GAMMA = 2.2                     # sRGB-ish
LOG2_SHIFT = 2                  # 10-bit → 8-bit for fast debayer
AWB_SMOOTHING = 0.85
AWB_STRIDE = 8                  # subsample stride for AWB — 64× fewer pixels
AWB_P = 2                       # Shades-of-Gray p-norm (1=gray-world, higher=closer to white-patch)
# Optional manual WB override: set env SURFACE_WEBCAM_WB="B,G,R" (floats) to freeze gains.
_wb_override = os.environ.get("SURFACE_WEBCAM_WB")
_wb_override = (tuple(float(x) for x in _wb_override.split(",")) if _wb_override else None)
LUT_REBUILD_EVERY = 5           # frames between LUT rebuilds (5 = ~0.3s at 15fps)
DEBUG_EVERY = 0                 # 0 = silent
FPS_LOG_SEC = 5.0               # emit one FPS line every N seconds (0 = off)
CSI2_ENTITY = "Intel IPU7 CSI2 0"
ISYS_ENTITY = "Intel IPU7 ISYS Capture 0"

_wb_bgr = np.array([1.0, 1.0, 1.0], dtype=np.float32)
# Per-channel LUT combining WB gain + clamp + gamma. Shape (1,256,3) for cv2.LUT on HxWx3.
_color_lut = np.zeros((1, 256, 3), dtype=np.uint8)
_frame_count = 0
_fps_last_t = 0.0
_fps_last_count = 0


def rebuild_color_lut():
    """Recompute the per-channel WB+gamma LUT from current _wb_bgr."""
    idx = np.arange(256, dtype=np.float32)
    for c in range(3):
        scaled = np.minimum(idx * _wb_bgr[c], 255.0)
        out = (scaled / 255.0) ** (1.0 / GAMMA) * 255.0
        _color_lut[0, :, c] = np.clip(out, 0, 255).astype(np.uint8)


def setup_output():
    fd = os.open(OUTPUT, os.O_WRONLY)
    fmt = v4l2_format()
    fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
    fmt.fmt.pix.width = WIDTH
    fmt.fmt.pix.height = HEIGHT
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUV420
    fmt.fmt.pix.field = V4L2_FIELD_NONE
    fmt.fmt.pix.bytesperline = WIDTH
    fmt.fmt.pix.sizeimage = WIDTH * HEIGHT * 3 // 2
    fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
    return fd


def find_sensor_subdev():
    """Find the /dev/v4l-subdev* node belonging to ov02c10."""
    for entry in sorted(os.listdir("/sys/class/video4linux")):
        if not entry.startswith("v4l-subdev"):
            continue
        try:
            with open(f"/sys/class/video4linux/{entry}/name") as f:
                if f.read().startswith("ov02c10"):
                    return f"/dev/{entry}"
        except OSError:
            pass
    return None


def setup_pipeline():
    """Configure the IPU7 media graph and sensor gains. Idempotent."""
    fmt = f"fmt:SGRBG10_1X10/{WIDTH}x{HEIGHT}"
    subprocess.run(["media-ctl", "-d", MEDIA, "--set-v4l2",
                    f'"{CSI2_ENTITY}":0[{fmt}]'], check=True)
    subprocess.run(["media-ctl", "-d", MEDIA, "--set-v4l2",
                    f'"{CSI2_ENTITY}":1[{fmt}]'], check=True)
    subprocess.run(["media-ctl", "-d", MEDIA, "--links",
                    f'"{CSI2_ENTITY}":1 -> "{ISYS_ENTITY}":0[1]'], check=True)
    sensor = find_sensor_subdev()
    if sensor:
        subprocess.run(["v4l2-ctl", "-d", sensor,
                        "--set-ctrl=analogue_gain=64",
                        "--set-ctrl=digital_gain=1024"], check=False)
    # Video capture format follows the media-ctl pipeline format.
    subprocess.run(["v4l2-ctl", "-d", CAPTURE,
                    f"--set-fmt-video=width={WIDTH},height={HEIGHT},pixelformat=BA10"],
                   check=True)


def process_frame(raw_bytes):
    global _wb_bgr, _frame_count
    _frame_count += 1

    raw = np.frombuffer(raw_bytes, dtype=np.uint16).reshape(HEIGHT, STRIDE // 2)[:, :WIDTH]
    raw8 = (raw >> LOG2_SHIFT).astype(np.uint8)         # 10-bit → 8-bit
    # V4L2 SGRBG10 ↔ OpenCV BayerGB (naming conventions disagree by one position).
    bgr = cv2.cvtColor(raw8, cv2.COLOR_BayerGB2BGR)
    bgr = cv2.rotate(bgr, cv2.ROTATE_180)               # sensor mounted upside-down

    if _wb_override is not None:
        _wb_bgr[:] = _wb_override
    else:
        # Shades-of-Gray AWB on a subsample; robust-ish to scenes dominated by one color.
        sub = bgr[::AWB_STRIDE, ::AWB_STRIDE].astype(np.float32)
        norms = np.power((sub ** AWB_P).mean(axis=(0, 1)), 1.0 / AWB_P)
        norms = np.maximum(norms, 1.0)
        gray = norms.mean()
        target = np.clip(gray / norms, 0.3, 3.0)
        _wb_bgr = AWB_SMOOTHING * _wb_bgr + (1.0 - AWB_SMOOTHING) * target

    # Rebuild LUT occasionally; WB drifts slowly so no need every frame.
    if _frame_count % LUT_REBUILD_EVERY == 0:
        rebuild_color_lut()

    # WB + clamp + gamma in a single uint8 LUT lookup — ~10× faster than float math.
    bgr = cv2.LUT(bgr, _color_lut)
    yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)

    if FPS_LOG_SEC:
        global _fps_last_t, _fps_last_count
        now = time.monotonic()
        if _fps_last_t == 0.0:
            _fps_last_t = now
            _fps_last_count = _frame_count
        elif now - _fps_last_t >= FPS_LOG_SEC:
            fps = (_frame_count - _fps_last_count) / (now - _fps_last_t)
            print(f"fps={fps:.1f} | wb B={_wb_bgr[0]:.2f} G={_wb_bgr[1]:.2f} R={_wb_bgr[2]:.2f}",
                  flush=True)
            _fps_last_t = now
            _fps_last_count = _frame_count
    return yuv.tobytes()


def read_exact(f, n):
    """Read exactly n bytes from file, or return None on EOF."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = f.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def start_capture():
    return subprocess.Popen(
        [
            "v4l2-ctl", "-d", CAPTURE,
            "--stream-mmap=4",
            "--stream-to=-",
            "--stream-count=0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def stop_capture(cap):
    if cap is None:
        return
    try:
        cap.terminate()
        cap.wait(timeout=2)
    except Exception:
        try:
            cap.kill()
        except Exception:
            pass


def main():
    setup_pipeline()
    out_fd = setup_output()
    cap = start_capture()

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        while running:
            raw = read_exact(cap.stdout, FRAME_SIZE)
            if raw is None:
                print("capture EOF", flush=True)
                break
            try:
                os.write(out_fd, process_frame(raw))
            except BrokenPipeError:
                # Consumer vanished mid-write; keep going.
                pass
    finally:
        stop_capture(cap)
        os.close(out_fd)


if __name__ == "__main__":
    main()
