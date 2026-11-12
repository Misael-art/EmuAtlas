#!/bin/sh
# Compatibility entry; the canonical profile selects the native runtime and shared data.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
if [ "$#" -ne 1 ]; then
    echo "Informe um único jogo em roms/<system_id>/; opções do emulador são geridas pelo perfil." >&2
    exit 2
fi
exec "$ROOT/emulator/launch.sh" --item "$1"
