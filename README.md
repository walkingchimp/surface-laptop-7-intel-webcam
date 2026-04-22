# Surface Laptop 7 (Intel Lunar Lake) — Linux webcam workaround

First working Linux setup (that I'm aware of) for the built-in **OV02C10** camera on the **Intel Surface Laptop 7**. As of April 2026, every SL7 Intel user report in the [linux-surface tracker](https://github.com/linux-surface/linux-surface/issues/1710) says the camera is not working. This repo makes it work, **with caveats**.

> ⚠️ **This is a workaround, not a proper fix.** Microsoft wired the sensor to a 12 MHz external clock, but the upstream `ov02c10` driver hard-requires 19.2 MHz. The patch here downgrades that check to a warning so the sensor probes, but **the PLL register sequences still assume 19.2 MHz input**, so pixel timing is off by ~37%. The IPU7 tolerates it (one `csi2-0: Short packet discarded` log line per capture session; frames still arrive) and picture quality is fine for video calls, but this should not be sent upstream until somebody recomputes correct PLL registers for 12 MHz from the OV02C10 datasheet.

## Result

- `/dev/video42` "Surface Webcam" device, selectable in Chrome, Cheese, Zoom, etc.
- 1928×1092 @ ~12 fps (ceiling is Python processing; sensor-side PLL-mismatched ceiling is ~18 fps).
- Manual white-balance calibration via a white card.
- One-shot `camera-on` / `camera-off` scripts; sensor only binds when you want it (privacy LED is off when idle).

## Hardware

- Surface Laptop 7, **Intel Core Ultra 2xxV (Lunar Lake)**, any SKU.
- Sensor: OmniVision `OV02C10` (ACPI `OVTI02C1`) on i2c addr `0x36`.
- Power controller: `INT3472` (discrete).
- Image processor: Intel IPU7 (PCI `8086:645d`).
- SAM hub: `MSHW0551` (relevant for the *keyboard* fix, which is already in [linux-surface](https://github.com/linux-surface/linux-surface)).

## Prerequisites

- The **[linux-surface](https://github.com/linux-surface/linux-surface) kernel** (≥ 6.18.x-surface-1). Stock Ubuntu / Fedora kernels lack Surface-specific support (especially keyboard). I use the Debian apt repo at `https://pkg.surfacelinux.com/debian`.
- Ubuntu 25.10 (I haven't tried other distros, but anything with kernel 6.18+ and PipeWire should work).
- Packages: `build-essential dkms python3-opencv python3-v4l2 v4l-utils ffmpeg git`.

## Install

Steps, in order. Each is a single command / short block.

### 1. Build and install the patched `ov02c10` kernel module via DKMS

```bash
sudo apt install -y build-essential dkms

# Stage the patched source
sudo mkdir -p /usr/src/ov02c10-patched-1.0
# Fetch upstream ov02c10.c at your kernel's version, then apply the patch here:
sudo curl -sL "https://raw.githubusercontent.com/torvalds/linux/v6.18/drivers/media/i2c/ov02c10.c" \
  -o /usr/src/ov02c10-patched-1.0/ov02c10.c
sudo patch -d /usr/src/ov02c10-patched-1.0/ -p1 < kernel/ov02c10-12mhz.patch
sudo install -m 644 kernel/Makefile  /usr/src/ov02c10-patched-1.0/Makefile
sudo install -m 644 kernel/dkms.conf /usr/src/ov02c10-patched-1.0/dkms.conf

# Register + build + install
sudo dkms add     -m ov02c10-patched -v 1.0
sudo dkms build   -m ov02c10-patched -v 1.0
sudo dkms install -m ov02c10-patched -v 1.0
sudo depmod -a

# Reload the module so the new one is live (otherwise takes effect at next boot)
sudo rmmod ov02c10 2>/dev/null || true
sudo modprobe ov02c10
```

Verify: `modinfo ov02c10 | grep filename` → `.../updates/dkms/ov02c10.ko`.

### 2. Install v4l2loopback 0.15.3 (upstream) — Ubuntu's package fails on kernel 6.18

```bash
sudo apt purge -y v4l2loopback-dkms 2>/dev/null || true
sudo apt install -y git
git clone --depth 1 -b v0.15.3 https://github.com/umlaeute/v4l2loopback.git /tmp/v4l2loopback
sudo cp -r /tmp/v4l2loopback /usr/src/v4l2loopback-0.15.3
sudo dkms add     -m v4l2loopback -v 0.15.3
sudo dkms build   -m v4l2loopback -v 0.15.3
sudo dkms install -m v4l2loopback -v 0.15.3
sudo depmod -a
```

### 3. Install the userspace bridge and system files

```bash
sudo apt install -y python3-opencv python3-v4l2 v4l-utils

# Userspace bridge
sudo install -d /usr/local/lib/surface-webcam
sudo install -m 755 userspace/surface-webcam.py /usr/local/lib/surface-webcam/

# systemd + modprobe
sudo install -m 644 system/surface-webcam.service /etc/systemd/system/surface-webcam.service
sudo install -m 644 system/modprobe.conf          /etc/modprobe.d/surface-webcam.conf
sudo install -m 644 system/modules-load.conf      /etc/modules-load.d/surface-webcam.conf

# Camera-toggle scripts + passwordless sudo for just this service
install -d ~/bin
install -m 755 userspace/camera-on  ~/bin/camera-on
install -m 755 userspace/camera-off ~/bin/camera-off
install -m 755 userspace/calibrate-wb.sh ~/surface-webcam/calibrate-wb.sh 2>/dev/null || \
  install -D -m 755 userspace/calibrate-wb.sh ~/surface-webcam/calibrate-wb.sh

# sudoers drop-in: edit the template to replace <YOUR_USER> with your actual username, then install
sed "s/<YOUR_USER>/$USER/" system/sudoers-drop-in.template | \
  sudo tee /etc/sudoers.d/surface-webcam-$USER > /dev/null
sudo chmod 440 /etc/sudoers.d/surface-webcam-$USER
sudo visudo -c    # should print "parsed OK"

# Reload systemd config
sudo systemctl daemon-reload

# We intentionally do NOT `systemctl enable` the service — keeps the camera LED off
# until you explicitly run `camera-on`. If you want it auto-starting at boot,
# `sudo systemctl enable surface-webcam.service` yourself.

source ~/.profile && hash -r   # pick up ~/bin
```

### 4. Calibrate white balance

Point the camera at a plain white surface (paper, wall, shirt — anything that fills most of the frame and should look neutral under your lighting). Then:

```bash
~/surface-webcam/calibrate-wb.sh
```

The script captures a reference frame, computes WB gains, and prints a line like:
```
SURFACE_WEBCAM_WB=0.891,0.806,1.568
```

Lock those gains via a systemd drop-in:

```bash
sudo mkdir -p /etc/systemd/system/surface-webcam.service.d
echo -e '[Service]\nEnvironment="SURFACE_WEBCAM_WB=0.891,0.806,1.568"' | \
  sudo tee /etc/systemd/system/surface-webcam.service.d/wb.conf
sudo systemctl daemon-reload
```

(Replace the numbers with yours.) If you skip this step, the bridge falls back to a Shades-of-Gray auto-WB, which works but biases toward whatever colour dominates the scene.

### 5. Use it

```bash
camera-on    # LED lights up, /dev/video42 is live
# … use it in Chrome / Cheese / Zoom / etc. …
camera-off   # LED goes out
```

Chrome should show **Surface Webcam** in `chrome://settings/content/camera`.

## What works, what doesn't

✅ Frames flow at ~12 fps, 1928×1092, YUV420. \
✅ Works in Chrome, Cheese, Zoom, OBS, browser WebRTC. \
✅ Manual WB calibration gives pleasing colours. \
✅ Privacy LED is off when `camera-off`. \
✅ Survives reboots (DKMS rebuilds module on kernel upgrades).

⚠️ **~12 fps ceiling.** Python-side debayer + WB + gamma + YUV conversion at 2 MP caps at ~12 fps on Lunar Lake. Sensor-side ceiling from the PLL mismatch is ~18 fps. Downscaling the output to 720p would roughly double processing throughput; I haven't done that. \
⚠️ **One `csi2-0: Short packet discarded` dmesg line per capture session.** Non-fatal — just the IPU7 noting CSI2 clock skew from the PLL mismatch. \
⚠️ **No image signal processor.** No auto-exposure, no auto-gain (I set constants), no colour matrix. Gray-world AWB only. \
⚠️ **Microphone doesn't work.** Separate problem — the RT721 + RT1320 SDCA codecs on Lunar Lake don't have a matching SOF machine driver / topology in mainline yet. External USB/Bluetooth mics work fine. \
⚠️ **Sensor is upside-down.** The bridge rotates 180° in software (cheap).

## Non-obvious findings (for anyone continuing this work)

1. **V4L2 `SGRBG10` maps to OpenCV `COLOR_BayerGB2BGR`**, not `COLOR_BayerGR2BGR`. Their 4-letter naming conventions disagree by one position. Discovered empirically by trying all four patterns and asking a human which looked right.
2. **`v4l2loopback-dkms` 0.15.0 (Ubuntu 25.10 package) won't build on kernel 6.18** because of the `v4l2_fh_add(fh, filp)` signature change. Upstream 0.15.3 (tagged November 2025) has the fix.
3. **`exclusive_caps=1` is required** for Chrome. With `=0` (the default and what most v4l2loopback tutorials suggest), the device advertises both `V4L2_CAP_VIDEO_CAPTURE` and `V4L2_CAP_VIDEO_OUTPUT`, and Chrome's device filter rejects it as "not a real webcam".
4. **Ubuntu's `python3-v4l2` bindings don't round-trip `buf.index` or `buf.bytesused` through `VIDIOC_DQBUF`.** The direct ioctl path either reads the same stale buffer forever, or returns EINVAL on requeue. We sidestep by delegating capture to a `v4l2-ctl --stream-mmap` subprocess and piping the raw bytes into Python.
5. **IVSC at PCI `8086:A862` is a red herring** on this machine for the camera. The PCI device is unbound, but we confirmed it's not in the actual camera path — the sensor's i2c bus is directly accessible via INT3472. The real blocker was always the 12 MHz clock check.
6. The **keyboard** fix is already in linux-surface: `MSHW0551` → `ssam_node_group_sl7` in `surface_aggregator_registry.c`. Using linux-surface's kernel is sufficient; no patching needed on that side.

## TODO / help wanted

- **Proper PLL recomputation for 12 MHz input** in `ov02c10.c`. This would eliminate the `Short packet discarded` warnings, fix frame-rate drift, and be upstreamable to `linux-media@vger.kernel.org`. It needs:
  - The OmniVision OV02C10 datasheet (non-public). If you have it, even a timing diagram would unblock this.
  - Alternatively, reverse-engineering the PLL register map by sweeping values and checking the MIPI output clock.
- **720p downscale path** for ~2× FPS — straightforward but I haven't implemented it.
- **A proper ISP chain** (AE/AWB/colour matrix). libcamera doesn't have an IPU7 pipeline handler yet; Intel's out-of-tree ipu7-camera-hal stack is the realistic path but heavy.
- **Audio**: RT721 (codec) + RT1320 ×2 (speaker amps) on SoundWire. No matching `sof_sdw` quirk entry for this combo; no `.tplg` topology shipped. Separate work.

## Layout

```
kernel/
  ov02c10-12mhz.patch     one-hunk diff vs upstream 6.18
  Makefile                DKMS out-of-tree build
  dkms.conf               DKMS module config
userspace/
  surface-webcam.py       the bridge
  calibrate-wb.sh         white-balance calibration helper
  camera-on / camera-off  service toggle shortcuts
system/
  surface-webcam.service
  modprobe.conf           v4l2loopback options (video_nr=42, exclusive_caps=1, etc.)
  modules-load.conf       autoload v4l2loopback at boot
  sudoers-drop-in.template
LICENSE                   GPL-2.0-only (matches upstream ov02c10.c)
```

## Credits

- Upstream `ov02c10.c` authors: Hans de Goede, Heimir Thor Sverrisson, Hao Yao.
- [linux-surface](https://github.com/linux-surface/linux-surface) community for the kernel base and the SAM keyboard work.
- [umlaeute/v4l2loopback](https://github.com/umlaeute/v4l2loopback).

This workaround was put together in a single afternoon with a lot of iteration. If you're on an SL7 Intel and this helps you, please comment on [linux-surface issue #1710](https://github.com/linux-surface/linux-surface/issues/1710) so the community knows the trick works across units. Issues and PRs welcome.
