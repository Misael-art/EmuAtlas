#!/usr/bin/env bash
set -euo pipefail
INSTALL_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null 2>&1; then
    # Only bootstrap Python after explicit installation was requested.
    if [[ " ${*:-} " != *" --apply "* ]]; then
        printf 'Falta Python 3. Execute ./install.sh install --apply para preparar o host.\n' >&2
        exit 2
    fi
    . /etc/os-release
    family="${ID:-} ${ID_LIKE:-}"
    case "$family" in
        *steamos*|*bazzite*|*silverblue*|*kinoite*) printf 'Instale Python pelo mecanismo suportado do host imutável.\n' >&2; exit 2 ;;
        *arch*|*manjaro*|*biglinux*) bootstrap=(pacman -S --needed --noconfirm python) ;;
        *debian*|*ubuntu*) bootstrap=(apt-get install -y python3 python3-venv) ;;
        *fedora*|*rhel*) bootstrap=(dnf install -y python3) ;;
        *) printf 'Distribuição sem bootstrap de Python suportado.\n' >&2; exit 2 ;;
    esac
    if [[ "$EUID" -eq 0 ]]; then
        "${bootstrap[@]}"
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "${bootstrap[@]}"
    else
        sudo "${bootstrap[@]}"
    fi
fi
cd -- "$INSTALL_ROOT"
if (($# == 0)); then set -- plan; fi
exec python3 "$INSTALL_ROOT/tools/distribution/setup.py" "$@"
