#!/bin/bash
# Calibrate the surface-webcam WB against a white reference.
# Usage: point the camera at a plain white surface (paper, wall, etc.) that fills
# the frame, then run this script. It prints a SURFACE_WEBCAM_WB=... line you
# can paste into the service unit to lock those gains.
set -e

echo "Stopping bridge so we can capture directly from /dev/video0..."
sudo systemctl stop surface-webcam.service

echo "Configuring media pipeline..."
sudo media-ctl -d /dev/media0 --set-v4l2 '"Intel IPU7 CSI2 0":0[fmt:SGRBG10_1X10/1928x1092]'
sudo media-ctl -d /dev/media0 --set-v4l2 '"Intel IPU7 CSI2 0":1[fmt:SGRBG10_1X10/1928x1092]'
sudo media-ctl -d /dev/media0 --links '"Intel IPU7 CSI2 0":1 -> "Intel IPU7 ISYS Capture 0":0[1]'

SENSOR_SD=""
for sd in /sys/class/video4linux/v4l-subdev*; do
  name=$(cat "$sd/name" 2>/dev/null)
  [[ "$name" == ov02c10* ]] && { SENSOR_SD="/dev/$(basename "$sd")"; break; }
done
[ -n "$SENSOR_SD" ] || { echo "couldn't find ov02c10 subdev"; exit 1; }

v4l2-ctl -d "$SENSOR_SD" --set-ctrl=analogue_gain=64 --set-ctrl=digital_gain=1024
v4l2-ctl -d /dev/video0 --set-fmt-video=width=1928,height=1092,pixelformat=BA10

echo "Capturing reference frame..."
v4l2-ctl -d /dev/video0 --stream-mmap --stream-count=1 --stream-to=/tmp/wb-ref.raw

python3 - <<'EOF'
import numpy as np, cv2
raw = np.frombuffer(open('/tmp/wb-ref.raw','rb').read(), dtype='<u2').reshape(1092, 1952)[:, :1928]
raw8 = (raw >> 2).astype(np.uint8)
bgr = cv2.cvtColor(raw8, cv2.COLOR_BayerGB2BGR)
h, w = bgr.shape[:2]
center = bgr[h//4:3*h//4, w//4:3*w//4]           # avoid edge vignetting
means = center.reshape(-1, 3).mean(axis=0)        # [B, G, R]
gray = means.mean()
wb = gray / np.maximum(means, 1.0)
print(f"\nraw center means (BGR):  B={means[0]:.1f}  G={means[1]:.1f}  R={means[2]:.1f}")
print(f"\n---- paste this into the service unit ----")
print(f"SURFACE_WEBCAM_WB={wb[0]:.3f},{wb[1]:.3f},{wb[2]:.3f}")
EOF

echo ""
echo "Restarting bridge..."
sudo systemctl start surface-webcam.service
