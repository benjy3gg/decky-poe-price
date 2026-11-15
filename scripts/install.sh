#!/usr/bin/env bash
# Build PoE Price Check and install it into Decky Loader on this Deck (asks for your sudo password).
set -euo pipefail
cd "$(dirname "$0")/.."
# everything (frontend, APT engine, Node.js) into out/decky-poe-price; options go to build.sh (--no-node)
scripts/build.sh --no-zip "$@"
dest="$HOME/homebrew/plugins/decky-poe-price"
sudo rm -rf "$dest"
sudo mkdir -p "$dest"
sudo cp -r out/decky-poe-price/. "$dest/"
sudo systemctl restart plugin_loader
echo "Installed to $dest and restarted Decky Loader."
