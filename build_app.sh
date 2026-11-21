#!/bin/bash
# Builds a standalone "MPC2000XL Kit Builder.app" into ./dist
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt pyinstaller || exit 1
.venv/bin/pyinstaller --windowed --noconfirm --name "MPC2000XL Kit Builder" \
  --icon icon.icns --add-data "icon.png:." \
  --collect-binaries sounddevice --collect-binaries soundfile app.py
echo "Done: dist/MPC2000XL Kit Builder.app"
