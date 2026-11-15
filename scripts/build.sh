#!/usr/bin/env bash
# Build the complete PoE Price Check plugin into out/decky-poe-price and out/decky-poe-price-<version>.zip:
#   1. frontend (pnpm + rollup)
#   2. Awakened PoE Trade engine: APT sources (engine/fetch-apt.sh, pinned) -> engine/dist
#   3. Node.js runtime for the engine (official linux-x64 build, checksum-verified, cached in .cache/)
#   4. assemble + zip
#   (plus engine/kit, so the plugin can rebuild the engine for a new APT release on the Deck)
# Usage: scripts/build.sh [--no-node] [--no-zip]
#   --no-node  don't bundle Node.js (the plugin then looks for one on the system / in nvm)
# Nothing here touches the game; the plugin only reads the clipboard after the user's own Ctrl+Alt+C.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

NODE_VERSION="${NODE_VERSION:-v24.18.0}"
NODE_ARCH="linux-x64"
WITH_NODE=1
WITH_ZIP=1
for a in "$@"; do
  case "$a" in
    --no-node) WITH_NODE=0 ;;
    --no-zip) WITH_ZIP=0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

step=0
STEPS=4
phase() { step=$((step + 1)); echo; echo "==> [$step/$STEPS] $*"; }
VERSION=$(node -p "require('./package.json').version")
OUT="$ROOT/out/decky-poe-price"

phase "frontend"
pnpm install --silent
pnpm build

phase "Awakened PoE Trade engine"
if [ ! -d engine/vendor/awakened-poe-trade/renderer/src ]; then
  echo "fetching APT sources (git clone, ~60 MB)…"
  engine/fetch-apt.sh
else
  echo "APT sources: engine/vendor/awakened-poe-trade @ $(git -C engine/vendor/awakened-poe-trade rev-parse --short HEAD)"
fi
(cd engine && pnpm install --silent && node build.mjs)

phase "Node.js runtime"
NODE_DIR="node-$NODE_VERSION-$NODE_ARCH"
if [ "$WITH_NODE" = 1 ]; then
  mkdir -p .cache
  if [ ! -x ".cache/$NODE_DIR/bin/node" ]; then
    url="https://nodejs.org/dist/$NODE_VERSION"
    echo "downloading $NODE_DIR.tar.xz from nodejs.org (~30 MB)…"
    curl -fL --progress-bar -o ".cache/$NODE_DIR.tar.xz" "$url/$NODE_DIR.tar.xz"
    curl -fsSL -o .cache/SHASUMS256.txt "$url/SHASUMS256.txt"
    (cd .cache && grep " $NODE_DIR.tar.xz\$" SHASUMS256.txt | sha256sum -c -)
    tar -xJf ".cache/$NODE_DIR.tar.xz" -C .cache "$NODE_DIR/bin/node" "$NODE_DIR/LICENSE"
    rm ".cache/$NODE_DIR.tar.xz"
  fi
  echo "Node.js $(".cache/$NODE_DIR/bin/node" --version) ($NODE_ARCH)"
else
  echo "skipped (--no-node): the plugin uses Node.js from the system or nvm"
fi

phase "assemble"
rm -rf "$OUT"
mkdir -p "$OUT/engine"
cp -r dist py_modules main.py clipwatch.py overlay.py menu.py plugin.json package.json README.md LICENSE "$OUT/"
cp -r engine/dist "$OUT/engine/"
# build kit: lets the plugin rebuild the engine for a new APT release on the Deck (panel → Update).
# Flat node_modules (no symlinks), since Decky's ZIP install doesn't keep links or executable bits.
KIT="$OUT/engine/kit"
mkdir -p "$KIT"
cp -r engine/build.mjs engine/package.json engine/pnpm-lock.yaml engine/pnpm-workspace.yaml engine/shims engine/src \
  engine/sample-item.txt "$KIT/"
(cd "$KIT" && pnpm install --silent --config.node-linker=hoisted && rm -rf node_modules/.bin node_modules/.pnpm \
  node_modules/.modules.yaml node_modules/@rollup)
echo "engine build kit: $(du -sh "$KIT" | cut -f1)"
rm -rf "$OUT"/dist/*.map "$OUT"/py_modules/__pycache__
if [ "$WITH_NODE" = 1 ]; then
  mkdir -p "$OUT/bin"
  cp ".cache/$NODE_DIR/bin/node" "$OUT/bin/node"
  cp ".cache/$NODE_DIR/LICENSE" "$OUT/bin/LICENSE-nodejs"
fi
echo "plugin folder: out/decky-poe-price ($(du -sh "$OUT" | cut -f1))"
if [ "$WITH_ZIP" = 1 ]; then
  zip_name="decky-poe-price-$VERSION.zip"
  rm -f "out/$zip_name"
  (cd out && zip -qr "$zip_name" decky-poe-price)
  echo "zip: out/$zip_name ($(du -h "out/$zip_name" | cut -f1)) — install with Decky's 'Install plugin from ZIP file'"
fi
