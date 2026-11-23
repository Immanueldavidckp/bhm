#!/usr/bin/env bash
# Build an offline install bundle (no internet needed on the board):
#   dist/bhm-gateway-<version>.tar.gz  = gateway code + vendored pyserial & paho-mqtt (pure Python)
set -euo pipefail
cd "$(dirname "$0")/.."
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' bhm_gateway/__init__.py)
OUT=build/bhm-gateway
rm -rf build && mkdir -p "$OUT/lib" dist
if command -v uv >/dev/null; then
  uv pip install --quiet --no-deps --only-binary=:all: --target "$OUT/lib" -r requirements.txt
else
  python3 -m pip install --quiet --no-deps --only-binary=:all: --target "$OUT/lib" -r requirements.txt
fi
rm -rf "$OUT"/lib/*.dist-info/RECORD "$OUT"/lib/bin "$OUT"/lib/.lock
cp -r bhm_gateway "$OUT/lib/"
find "$OUT" -name __pycache__ -prune -exec rm -rf {} +
cp deploy/install.sh deploy/bhm-gateway.service deploy/bhm-sim.service config/bhm.example.toml "$OUT/"
tar -C build -czf "dist/bhm-gateway-$VER.tar.gz" bhm-gateway
echo "dist/bhm-gateway-$VER.tar.gz ($(du -h "dist/bhm-gateway-$VER.tar.gz" | cut -f1))"
