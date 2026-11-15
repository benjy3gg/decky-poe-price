#!/usr/bin/env bash
# Get the Awakened PoE Trade sources the engine is built from (MIT, github.com/SnosMe/awakened-poe-trade).
# Pinned to an APT release tag (github.com/SnosMe/awakened-poe-trade/releases); bump APT_REF for a new
# league. The plugin's panel can also update the engine to the latest release on the Deck itself.
set -euo pipefail
cd "$(dirname "$0")"
APT_REF="${APT_REF:-v3.29.108}"
if [ ! -d vendor/awakened-poe-trade/.git ]; then
  git clone --filter=blob:none https://github.com/SnosMe/awakened-poe-trade.git vendor/awakened-poe-trade
fi
git -C vendor/awakened-poe-trade fetch --quiet --tags origin
git -C vendor/awakened-poe-trade -c advice.detachedHead=false checkout --quiet "$APT_REF"
echo "APT at $(git -C vendor/awakened-poe-trade describe --tags 2>/dev/null || git -C vendor/awakened-poe-trade rev-parse --short HEAD)"
