#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
DRIVER=${1:?Informe o driver Model 2}
exec "$ROOT/emulator/launch.sh" --system-id model2 --item "$ROOT/roms/model2/roms/${DRIVER%.zip}.zip"
