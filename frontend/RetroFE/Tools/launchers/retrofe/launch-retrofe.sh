#!/usr/bin/env bash
# Relocatable RetroFE entry point for terminal, KDE menu and desktop shortcuts.
set -uo pipefail
ORIGINAL_ARGUMENTS=("$@")

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
find_project_root() {
    local candidate="$SCRIPT_DIR"
    while [[ "$candidate" != '/' ]]; do
        if [[ -x "$candidate/RetroFE" && -f "$candidate/settings.conf" && -d "$candidate/layouts" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
        candidate="$(dirname -- "$candidate")"
    done
    return 1
}

ROOT="$(find_project_root)" || {
    printf 'RetroFE: não encontrei a raiz do projeto acima de %s\n' "$SCRIPT_DIR" >&2
    exit 1
}
APP="$ROOT/RetroFE"
ICON="$ROOT/RetroFE.png"
[[ -f "$ICON" ]] || ICON='applications-games'
LIBDIR="$ROOT/core/linux/lib"
LAUNCHER="$SCRIPT_DIR/launch-retrofe.sh"
INPUT_SESSION_TOOL="$SCRIPT_DIR/input-session.py"
THEME_PROFILE_TOOL="$SCRIPT_DIR/theme-profile.py"
THEME_TRANSITION_TOOL="$SCRIPT_DIR/theme-transition.py"
DISPLAY_PROFILE_TOOL="$SCRIPT_DIR/display-profile.py"
STEAM_INPUT_TOOL="$SCRIPT_DIR/steam-input.py"
WINDOW_PLACEMENT_TOOL="$SCRIPT_DIR/window_placement.py"
INPUT_DEBUG_TOOL="$SCRIPT_DIR/input-debug-overlay.py"
NAVIGATION_PANEL_TOOL="$SCRIPT_DIR/navigation-panel.py"
MEDIA_PATH_TOOL="$ROOT/../../tools/roms/materialize-collection-media-paths.py"
DESKTOP_ID="retrofe-canonical.desktop"

MODE='launch'
if [[ "${1:-}" == '--check' ]]; then
    MODE='check'
    shift
elif [[ "${1:-}" == '--install-shortcuts' ]]; then
    MODE='shortcuts'
    shift
elif [[ "${1:-}" == '--version' ]]; then
    exec "$APP" "$@"
fi
INPUT_DEBUG_OVERRIDE=''
APP_ARGUMENTS=()
while (($#)); do
    case "$1" in
        --input-debug) INPUT_DEBUG_OVERRIDE='1' ;;
        --no-input-debug) INPUT_DEBUG_OVERRIDE='0' ;;
        *) APP_ARGUMENTS+=("$1") ;;
    esac
    shift
done
set -- "${APP_ARGUMENTS[@]}"

HOST_CONFIG="${XDG_CONFIG_HOME:-${HOME:-/tmp}/.config}/retrofe/host.env"
DISPLAY_CONFIG="$ROOT/Config/display.env"
if [[ -r "$DISPLAY_CONFIG" ]]; then
    # Portable display defaults; the optional per-user host.env can override them.
    # shellcheck disable=SC1090
    . "$DISPLAY_CONFIG"
fi
load_saved_display_profile() {
    local values key value
    [[ -r "$ROOT/UserData/retrofe/display-profile.json" && -r "$DISPLAY_PROFILE_TOOL" ]] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    values="$(python3 "$DISPLAY_PROFILE_TOOL" --root "$ROOT" env 2>/dev/null)" || return 0
    while IFS='=' read -r key value; do
        case "$key" in
            RETROFE_DISPLAY_PROFILE_ID) RETROFE_DISPLAY_PROFILE_ID="$value" ;;
            RETROFE_WINDOW_WIDTH) RETROFE_WINDOW_WIDTH="$value" ;;
            RETROFE_WINDOW_HEIGHT) RETROFE_WINDOW_HEIGHT="$value" ;;
        esac
    done <<< "$values"
}
if [[ -r "$HOST_CONFIG" ]]; then
    # User-owned, optional overrides for ROMs, BIOS and host-installed emulators.
    # shellcheck disable=SC1090
    . "$HOST_CONFIG"
fi
case "$INPUT_DEBUG_OVERRIDE" in
    1) RETROFE_INPUT_DEBUG='1' ;;
    0) RETROFE_INPUT_DEBUG='0' ;;
esac
case "${RETROFE_INPUT_DEBUG:-0}" in
    1|yes|true|on|ON|YES|TRUE) RETROFE_INPUT_DEBUG='1' ;;
    *) RETROFE_INPUT_DEBUG='0' ;;
esac
# An explicit RetroFE display choice is a user preference and takes priority
# over portable defaults and optional host window overrides.
load_saved_display_profile
RETROFE_WINDOW_WIDTH="${RETROFE_WINDOW_WIDTH:-1920}"
RETROFE_WINDOW_HEIGHT="${RETROFE_WINDOW_HEIGHT:-1080}"
RETROFE_FULLSCREEN="${RETROFE_FULLSCREEN:-no}"
RETROFE_SCREEN_NUM="${RETROFE_SCREEN_NUM:-0}"
RETROFE_TARGET_OUTPUT="${RETROFE_TARGET_OUTPUT:-auto}"
RETROFE_ATTRACT_MODE_TIME="${RETROFE_ATTRACT_MODE_TIME:-90}"

consume_pending_theme() {
    [[ -r "$THEME_PROFILE_TOOL" ]] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    local args=(--root "$ROOT" --width "$RETROFE_WINDOW_WIDTH" --height "$RETROFE_WINDOW_HEIGHT")
    [[ -n "${RETROFE_DISPLAY_PROFILE_ID:-}" ]] && args+=(--display-profile-id "$RETROFE_DISPLAY_PROFILE_ID")
    local output
    if ! output="$(python3 "$THEME_PROFILE_TOOL" "${args[@]}" consume-pending 2>&1)"; then
        report "[AVISO] $output"
        return 1
    fi
    [[ -n "$output" ]] && report "[TEMA] $output"
    return 0
}

load_saved_theme_profile() {
    [[ -r "$THEME_PROFILE_TOOL" ]] || return 0
    command -v python3 >/dev/null 2>&1 || return 0
    local args=(--root "$ROOT" --width "$RETROFE_WINDOW_WIDTH" --height "$RETROFE_WINDOW_HEIGHT")
    [[ -n "${RETROFE_DISPLAY_PROFILE_ID:-}" ]] && args+=(--display-profile-id "$RETROFE_DISPLAY_PROFILE_ID")
    local values key value
    values="$(python3 "$THEME_PROFILE_TOOL" "${args[@]}" env 2>&1)" || {
        show_error "Não consegui carregar o tema/perfil de tela salvo:\n\n$values"
        return 1
    }
    RETROFE_LAYOUT=''
    RETROFE_LAYOUT_VARIANT=''
    RETROFE_FIRST_COLLECTION=''
    while IFS='=' read -r key value; do
        case "$key" in
            RETROFE_THEME_ID) RETROFE_THEME_ID="$value" ;;
            RETROFE_LAYOUT) RETROFE_LAYOUT="$value" ;;
            RETROFE_LAYOUT_VARIANT) RETROFE_LAYOUT_VARIANT="$value" ;;
            RETROFE_FIRST_COLLECTION) RETROFE_FIRST_COLLECTION="$value" ;;
        esac
    done <<< "$values"
    [[ -n "$RETROFE_LAYOUT" ]] || {
        show_error 'O catálogo de temas não forneceu um layout válido.'
        return 1
    }
    report "[TEMA] ${RETROFE_THEME_ID:-tema atual} → $RETROFE_LAYOUT${RETROFE_LAYOUT_VARIANT:+ ($RETROFE_LAYOUT_VARIANT)}."
}

resolve_project_path() {
    local path="$1"
    if [[ "$path" == '~/'* ]]; then
        path="${HOME:-}/${path:2}"
    fi
    [[ "$path" == /* ]] || path="$ROOT/$path"
    if command -v realpath >/dev/null 2>&1; then
        realpath -m -- "$path"
    else
        printf '%s\n' "$path"
    fi
}

RETROFE_ROM_ROOT="$(resolve_project_path "${RETROFE_ROM_ROOT:-$ROOT/../../roms}")"
RETROFE_BIOS_ROOT="$(resolve_project_path "${RETROFE_BIOS_ROOT:-$ROOT/../../bios}")"
RETROFE_RETROARCH_CORE_ROOT="$(resolve_project_path "${RETROFE_RETROARCH_CORE_ROOT:-$ROOT/../../emulator/linux/retroarch-cores}")"
RETROFE_MAME="${RETROFE_MAME:-$(command -v mame 2>/dev/null || printf 'mame')}"
if [[ "$RETROFE_MAME" == */* && "$RETROFE_MAME" != /* ]]; then
    RETROFE_MAME="$(resolve_project_path "$RETROFE_MAME")"
fi
export RETROFE_ROM_ROOT RETROFE_BIOS_ROOT RETROFE_RETROARCH_CORE_ROOT RETROFE_MAME

STATE_BASE="${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}"
STATE_DIR="$STATE_BASE/retrofe"
if ! mkdir -p "$STATE_DIR" 2>/dev/null; then
    STATE_DIR="${TMPDIR:-/tmp}/retrofe-launcher-${UID:-0}"
    mkdir -p "$STATE_DIR" 2>/dev/null || true
fi
LOG_FILE="$STATE_DIR/launcher.log"
REPAIR_LOG="$STATE_DIR/dependency-repair.log"

export RETROFE_PATH="$ROOT"
export LD_LIBRARY_PATH="$LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Use one X11/XWayland coordinate space for RetroFE, its transition overlay,
# and Steam's controller window when the window-management tools are available.
RETROFE_WINDOW_BACKEND=''
RETROFE_CAN_PLACE_WINDOW=0
if [[ -n "${RETROFE_SDL_VIDEO_DRIVER:-}" ]]; then
    export SDL_VIDEODRIVER="$RETROFE_SDL_VIDEO_DRIVER"
    RETROFE_WINDOW_BACKEND="SDL $SDL_VIDEODRIVER (host override)"
    if [[ "${SDL_VIDEODRIVER,,}" == 'x11' && -n "${DISPLAY:-}" ]] \
        && command -v wmctrl >/dev/null 2>&1 \
        && command -v xdotool >/dev/null 2>&1 \
        && command -v xprop >/dev/null 2>&1 \
        && command -v xrandr >/dev/null 2>&1; then
        RETROFE_CAN_PLACE_WINDOW=1
    fi
elif [[ -n "${DISPLAY:-}" ]] \
    && command -v wmctrl >/dev/null 2>&1 \
    && command -v xdotool >/dev/null 2>&1 \
    && command -v xprop >/dev/null 2>&1 \
    && command -v xrandr >/dev/null 2>&1; then
    export SDL_VIDEODRIVER='x11'
    RETROFE_CAN_PLACE_WINDOW=1
    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
        RETROFE_WINDOW_BACKEND='SDL XWayland com seleção da saída ativa e geometria sincronizada'
    else
        RETROFE_WINDOW_BACKEND='SDL X11 com seleção da saída ativa e geometria sincronizada'
    fi
fi
# Expose the RetroFE audio stream through the KDE/PipeWire-Pulse application mixer.
export SDL_AUDIODRIVER="${RETROFE_SDL_AUDIO_DRIVER:-${SDL_AUDIODRIVER:-pulseaudio}}"
export SDL_AUDIO_DEVICE_STREAM_NAME="${RETROFE_AUDIO_STREAM_NAME:-${SDL_AUDIO_DEVICE_STREAM_NAME:-RetroFE}}"

RETROFE_MUSIC_PLAYER="${RETROFE_MUSIC_PLAYER:-mpv}"
RETROFE_MUSIC_VOLUME="${RETROFE_MUSIC_VOLUME:-100}"
RETROFE_MUSIC_FILE="${RETROFE_MUSIC_FILE:-}"
if [[ -z "$RETROFE_MUSIC_FILE" ]]; then
    ACTIVE_LAYOUT="$(awk -F= '/^[[:space:]]*layout[[:space:]]*=/ {value=$2; sub(/[[:space:]]*#.*/, "", value); gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); print value; exit}' "$ROOT/settings.conf")"
    if [[ "$ACTIVE_LAYOUT" == 'Horizontal - psX ui' ]]; then
        RETROFE_MUSIC_FILE='layouts/Horizontal - psX ui/music/ambience.mp3'
    fi
fi
if [[ -n "$RETROFE_MUSIC_FILE" ]]; then
    RETROFE_MUSIC_FILE="$(resolve_project_path "$RETROFE_MUSIC_FILE")"
fi
RETROFE_MUSIC_SOCKET="$STATE_DIR/music.sock"
export RETROFE_MUSIC_FILE RETROFE_MUSIC_SOCKET

log_line() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || printf 'time-unknown')" "$1" >> "$LOG_FILE" 2>/dev/null || true
}

report() {
    printf '%s\n' "$1"
    log_line "$1"
}

show_error() {
    local message="$1"
    log_line "ERROR: $message"
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$message" --title 'RetroFE — diagnóstico do host' >/dev/null 2>&1 || true
    elif [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v zenity >/dev/null 2>&1; then
        zenity --error --title='RetroFE — diagnóstico do host' --text="$message" >/dev/null 2>&1 || true
    else
        printf 'RetroFE: %s\n' "$message" >&2
    fi
}

RETROFE_TARGET_OUTPUT_NAME=''
RETROFE_TARGET_MONITOR_X=''
RETROFE_TARGET_MONITOR_Y=''
RETROFE_TARGET_MONITOR_WIDTH=''
RETROFE_TARGET_MONITOR_HEIGHT=''
RETROFE_TARGET_MONITOR_REASON=''

capture_target_monitor() {
    [[ "$RETROFE_CAN_PLACE_WINDOW" == '1' ]] || return 0
    [[ "$RETROFE_WINDOW_WIDTH" =~ ^[1-9][0-9]*$ \
        && "$RETROFE_WINDOW_HEIGHT" =~ ^[1-9][0-9]*$ ]] || return 0
    if [[ ! -r "$WINDOW_PLACEMENT_TOOL" ]] || ! command -v python3 >/dev/null 2>&1; then
        report '[AVISO] Não consegui consultar a saída ativa; o RetroFE abrirá com o posicionamento padrão do SDL.'
        return 0
    fi

    local requested="$RETROFE_TARGET_OUTPUT"
    [[ "${requested,,}" == 'auto' ]] && requested=''
    local target_info
    if ! target_info="$(python3 "$WINDOW_PLACEMENT_TOOL" monitor-target --output-name "$requested" 2>&1)"; then
        if [[ -n "$requested" ]]; then
            show_error "A saída configurada em RETROFE_TARGET_OUTPUT não está disponível:\n\n$target_info"
            return 1
        fi
        report "[AVISO] Não consegui identificar a saída ativa; o RetroFE abrirá com o posicionamento padrão do SDL. ($target_info)"
        return 0
    fi

    IFS=$'\t' read -r RETROFE_TARGET_OUTPUT_NAME RETROFE_TARGET_MONITOR_X \
        RETROFE_TARGET_MONITOR_Y RETROFE_TARGET_MONITOR_WIDTH \
        RETROFE_TARGET_MONITOR_HEIGHT RETROFE_TARGET_MONITOR_REASON <<< "$target_info"
    if [[ ! "$RETROFE_TARGET_MONITOR_X" =~ ^-?[0-9]+$ \
        || ! "$RETROFE_TARGET_MONITOR_Y" =~ ^-?[0-9]+$ \
        || ! "$RETROFE_TARGET_MONITOR_WIDTH" =~ ^[1-9][0-9]*$ \
        || ! "$RETROFE_TARGET_MONITOR_HEIGHT" =~ ^[1-9][0-9]*$ ]]; then
        show_error "O diagnóstico do monitor retornou geometria inválida:\n\n$target_info"
        return 1
    fi

    local window_x window_y
    case "${RETROFE_FULLSCREEN,,}" in
        yes|true|1)
        window_x="$RETROFE_TARGET_MONITOR_X"
        window_y="$RETROFE_TARGET_MONITOR_Y"
        ;;
        *)
        window_x=$((RETROFE_TARGET_MONITOR_X + (RETROFE_TARGET_MONITOR_WIDTH - RETROFE_WINDOW_WIDTH) / 2))
        window_y=$((RETROFE_TARGET_MONITOR_Y + (RETROFE_TARGET_MONITOR_HEIGHT - RETROFE_WINDOW_HEIGHT) / 2))
        ((window_x < RETROFE_TARGET_MONITOR_X)) && window_x="$RETROFE_TARGET_MONITOR_X"
        ((window_y < RETROFE_TARGET_MONITOR_Y)) && window_y="$RETROFE_TARGET_MONITOR_Y"
        ;;
    esac
    export SDL_VIDEO_WINDOW_POS="$window_x,$window_y"
    report "[JANELA] Saída $RETROFE_TARGET_OUTPUT_NAME selecionada ($RETROFE_TARGET_MONITOR_REASON), geometria X11/XWayland ${RETROFE_TARGET_MONITOR_X},${RETROFE_TARGET_MONITOR_Y} ${RETROFE_TARGET_MONITOR_WIDTH}x${RETROFE_TARGET_MONITOR_HEIGHT}; posição inicial SDL ${SDL_VIDEO_WINDOW_POS}."
}

align_frontend_window() {
    [[ "$RETROFE_CAN_PLACE_WINDOW" == '1' && -n "$RETROFE_TARGET_OUTPUT_NAME" ]] || return 0
    local result
    if ! result="$(python3 "$WINDOW_PLACEMENT_TOOL" place-retrofe \
        --pid "$APP_PID" \
        --monitor-x "$RETROFE_TARGET_MONITOR_X" \
        --monitor-y "$RETROFE_TARGET_MONITOR_Y" \
        --monitor-width "$RETROFE_TARGET_MONITOR_WIDTH" \
        --monitor-height "$RETROFE_TARGET_MONITOR_HEIGHT" \
        --width "$RETROFE_WINDOW_WIDTH" \
        --height "$RETROFE_WINDOW_HEIGHT" \
        --fullscreen "$RETROFE_FULLSCREEN" 2>&1)"; then
        report "[AVISO] Não consegui confirmar o posicionamento da janela em $RETROFE_TARGET_OUTPUT_NAME; o RetroFE continua aberto. ($result)"
        return 0
    fi
    report "[JANELA] RetroFE posicionado em $RETROFE_TARGET_OUTPUT_NAME: $result"
}

if [[ "$MODE" == 'launch' ]]; then
    consume_pending_theme || true
fi
load_saved_theme_profile || exit 1
if [[ "$MODE" == 'launch' ]]; then
    capture_target_monitor || exit 1
fi

desktop_exec_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    printf '"%s"' "$value"
}

write_desktop_entry() {
    local destination="$1"
    local escaped_launcher temporary
    escaped_launcher="$(desktop_exec_quote "$LAUNCHER")"
    temporary="$(mktemp "${destination}.XXXXXX")" || return 1
    cat > "$temporary" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=RetroFE
Name[pt_BR]=RetroFE (Emulação)
GenericName=Frontend de emulação
Comment=Abre o RetroFE após verificar e preparar o host
Exec=$escaped_launcher
Path=$ROOT
Icon=$ICON
Terminal=false
StartupNotify=true
Categories=Game;Emulator;
EOF
    if [[ -f "$destination" ]] && cmp -s "$temporary" "$destination"; then
        rm -f "$temporary"
    else
        mv -f "$temporary" "$destination" || return 1
        SHORTCUTS_CHANGED=1
    fi
    chmod 755 "$destination" || return 1
}

install_kde_shortcuts() {
    local desktop_dir applications_dir
    local data_home="${XDG_DATA_HOME:-${HOME:-/tmp}/.local/share}"
    local changed_before="$SHORTCUTS_CHANGED"

    if command -v xdg-user-dir >/dev/null 2>&1; then
        desktop_dir="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    else
        desktop_dir=""
    fi
    [[ -n "$desktop_dir" ]] || desktop_dir="${HOME:-/tmp}/Desktop"
    applications_dir="$data_home/applications"

    if ! mkdir -p "$desktop_dir" "$applications_dir" 2>/dev/null; then
        report '[AVISO] Não foi possível preparar as pastas de atalhos do KDE.'
        return 1
    fi
    if ! write_desktop_entry "$desktop_dir/$DESKTOP_ID" || ! write_desktop_entry "$applications_dir/$DESKTOP_ID"; then
        report '[AVISO] Não foi possível criar ou atualizar os dois atalhos do RetroFE.'
        return 1
    fi

    if [[ "$SHORTCUTS_CHANGED" != "$changed_before" ]]; then
        if command -v kbuildsycoca6 >/dev/null 2>&1; then
            kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
        elif command -v kbuildsycoca5 >/dev/null 2>&1; then
            kbuildsycoca5 --noincremental >/dev/null 2>&1 || true
        fi
        report "[OK] Atalhos do Desktop e do menu KDE instalados/atualizados (ícone: $ICON)."
    else
        report '[OK] Atalhos do Desktop e do menu KDE já estavam atualizados.'
    fi
}

show_repair_confirmation() {
    local missing="$1"
    local packages="$2"
    local message
    message="Falta um componente de execução necessário para abrir o RetroFE:\n\n$missing\n\nO reparo instalará apenas dependências de execução do RetroFE, sem atualizar o sistema inteiro:\n$packages\n\nAutorizar agora?"
    if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v kdialog >/dev/null 2>&1; then
        kdialog --warningyesno "$message" --title 'RetroFE — reparar dependências'
    elif [[ -t 0 ]]; then
        printf '%s\n' "$message"
        read -r -p 'Instalar as dependências? [s/N] ' answer
        [[ "$answer" =~ ^[sS]([iI][mM])?$ ]]
    else
        return 1
    fi
}

install_runtime_packages() {
    local missing="$1"
    local family=""
    local id=""
    local id_like=""
    local package_names=""
    local sdl2_package='sdl2'
    local command_line=()
    local privilege=()

    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"
        id_like="${ID_LIKE:-}"
    fi
    family="$id $id_like"

    if [[ "$family" == *arch* ]] && command -v pacman >/dev/null 2>&1; then
        if ! pacman -Si sdl2 >/dev/null 2>&1 && pacman -Si sdl2-compat >/dev/null 2>&1; then
            sdl2_package='sdl2-compat'
        fi
        package_names="$sdl2_package sdl2_image sdl2_mixer sdl2_ttf gstreamer gst-plugins-base gst-plugins-base-libs gst-plugins-good"
        command_line=(pacman -S --needed --noconfirm "$sdl2_package" sdl2_image sdl2_mixer sdl2_ttf gstreamer gst-plugins-base gst-plugins-base-libs gst-plugins-good)
    elif [[ "$family" == *debian* || "$family" == *ubuntu* ]] && command -v apt-get >/dev/null 2>&1; then
        package_names='libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good'
        command_line=(env DEBIAN_FRONTEND=noninteractive apt-get install -y libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-mixer-2.0-0 libsdl2-ttf-2.0-0 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good)
    elif [[ "$family" == *fedora* || "$family" == *rhel* ]] && command -v dnf >/dev/null 2>&1; then
        package_names='SDL2 SDL2_image SDL2_mixer SDL2_ttf gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good'
        command_line=(dnf install -y SDL2 SDL2_image SDL2_mixer SDL2_ttf gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good)
    else
        show_error "O host ainda não resolve estas bibliotecas: $missing\n\nO reparo automático está preparado para Arch/Manjaro/BigLinux, Debian/Ubuntu e Fedora. Instale os pacotes de SDL2 e GStreamer correspondentes à sua distribuição e tente novamente."
        return 1
    fi

    if ! show_repair_confirmation "$missing" "$package_names"; then
        show_error "O RetroFE não foi aberto. As dependências ausentes são: $missing"
        return 1
    fi

    if command -v pkexec >/dev/null 2>&1; then
        privilege=(pkexec)
    elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
        privilege=(sudo)
    else
        show_error "Não encontrei um agente de autorização gráfica (pkexec). Execute o reparo pelo terminal ou instale: $package_names"
        return 1
    fi

    report "[REPARO] Instalando dependências necessárias: $package_names"
    if ! "${privilege[@]}" "${command_line[@]}" > "$REPAIR_LOG" 2>&1; then
        local detail
        detail="$(tail -n 16 "$REPAIR_LOG" 2>/dev/null || true)"
        show_error "Não foi possível instalar as dependências.\n\n$detail"
        return 1
    fi
    report '[OK] Reparo das dependências terminou.'
}

install_python_runtime() {
    local id='' id_like='' family='' package_names='' manager='' details_json='' romtool=''
    local command_line=() privilege=() record_output=''
    if [[ -r /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="${ID:-}"
        id_like="${ID_LIKE:-}"
    fi
    family="$id $id_like"
    if [[ "$family" == *arch* ]] && command -v pacman >/dev/null 2>&1; then
        package_names='python'
        manager='pacman'
        command_line=(pacman -S --needed --noconfirm python)
    elif [[ "$family" == *debian* || "$family" == *ubuntu* ]] && command -v apt-get >/dev/null 2>&1; then
        package_names='python3'
        manager='apt-get'
        command_line=(env DEBIAN_FRONTEND=noninteractive apt-get install -y python3)
    elif [[ "$family" == *fedora* || "$family" == *rhel* ]] && command -v dnf >/dev/null 2>&1; then
        package_names='python3'
        manager='dnf'
        command_line=(dnf install -y python3)
    elif [[ "$family" == *opensuse* || "$family" == *suse* ]] && command -v zypper >/dev/null 2>&1; then
        package_names='python3'
        manager='zypper'
        command_line=(zypper --non-interactive install python3)
    else
        show_error 'Python 3 é necessário para o launcher portátil, mas não encontrei um gerenciador suportado para instalá-lo automaticamente. Instale Python 3 pelo gerenciador oficial da distribuição e tente novamente.'
        return 1
    fi

    if ! show_repair_confirmation 'Python 3 (necessário para Steam Input e preferências de tema)' "$package_names"; then
        show_error "O RetroFE não foi aberto. Instale Python 3 para habilitar Steam Input e as opções portáteis."
        return 1
    fi
    if command -v pkexec >/dev/null 2>&1; then
        privilege=(pkexec)
    elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
        privilege=(sudo)
    else
        show_error "Não encontrei uma autorização de administrador para instalar Python 3 pelo $manager."
        return 1
    fi
    report "[REPARO] Instalando Python 3 pelo gerenciador $manager."
    if ! "${privilege[@]}" "${command_line[@]}" > "$REPAIR_LOG" 2>&1; then
        local detail
        detail="$(tail -n 16 "$REPAIR_LOG" 2>/dev/null || true)"
        show_error "Não foi possível instalar Python 3.\n\n$detail"
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        show_error 'A instalação terminou, mas o comando python3 continua indisponível.'
        return 1
    fi
    report '[OK] Python 3 instalado e disponível.'

    romtool="$(resolve_project_path "$ROOT/../../tools/roms/romtool.py")"
    if [[ -f "$romtool" ]]; then
        details_json="{\"package_manager\":\"$manager\",\"packages\":\"$package_names\",\"runtime\":\"python3\"}"
        record_output="$(python3 "$romtool" record-change --change-type retrofe-python-runtime-bootstrap --reason 'Instalar a dependência Python 3 exigida pelo launcher portátil Steam Input.' --details-json "$details_json" 2>&1)" || {
            report "[AVISO] Não consegui registrar a instalação Python no histórico: $record_output"
            return 0
        }
        report '[REGISTRO] Instalação Python 3 adicionada ao histórico ambiental do romtool.'
    else
        report '[AVISO] romtool.py não acompanha esta cópia; registre a instalação no histórico do projeto hospedeiro.'
    fi
}

find_missing_libraries() {
    local dependencies
    if ! command -v ldd >/dev/null 2>&1; then
        show_error 'O comando ldd não existe no host; não consigo validar as bibliotecas do RetroFE.'
        return 2
    fi
    if ! dependencies="$(ldd "$APP" 2>&1)"; then
        show_error "Não consegui inspecionar as bibliotecas do executável:\n\n$dependencies"
        return 2
    fi
    MISSING_LIBRARIES="$(printf '%s\n' "$dependencies" | awk '/not found/ {print $1}' | sort -u)"
    return 0
}

check_optional_host_content() {
    local rom_root="$RETROFE_ROM_ROOT"
    local bios_root="$RETROFE_BIOS_ROOT"
    local mame_path="$RETROFE_MAME"

    if [[ -d "$rom_root" ]]; then
        report "[OK] ROMs configuradas no host: $rom_root"
    else
        show_error "Pasta de ROMs não encontrada: $rom_root\n\nConfira a montagem do disco/cartão ou o caminho no perfil do host antes de abrir o RetroFE."
        return 1
    fi
    if [[ -d "$bios_root" ]]; then
        report "[OK] BIOS configuradas no host: $bios_root"
    else
        report "[AVISO] Pasta de BIOS não encontrada: $bios_root (o frontend ainda abrirá)."
    fi
    if [[ -n "$mame_path" && -x "$mame_path" ]]; then
        report "[OK] MAME disponível: $mame_path"
    elif command -v "$mame_path" >/dev/null 2>&1; then
        report "[OK] MAME disponível: $(command -v "$mame_path")"
    else
        report '[AVISO] MAME não encontrado; coleções que dependem dele não abrirão jogos (o frontend ainda abrirá).'
    fi
    if ! command -v flatpak >/dev/null 2>&1; then
        report '[AVISO] Flatpak não encontrado; emuladores instalados por Flatpak não serão iniciados (o frontend ainda abrirá).'
    else
        report "[OK] Flatpak disponível: $(command -v flatpak)"
    fi
    if [[ -d "$RETROFE_RETROARCH_CORE_ROOT" ]]; then
        report "[OK] Núcleos RetroArch encontrados: $RETROFE_RETROARCH_CORE_ROOT"
    else
        report "[AVISO] Pasta de núcleos RetroArch não encontrada: $RETROFE_RETROARCH_CORE_ROOT (o frontend ainda abrirá)."
    fi
}

validate_display_preferences() {
    if [[ ! "$RETROFE_WINDOW_WIDTH" =~ ^[1-9][0-9]*$ || ! "$RETROFE_WINDOW_HEIGHT" =~ ^[1-9][0-9]*$ ]]; then
        show_error 'A largura e a altura da janela precisam ser números inteiros positivos (RETROFE_WINDOW_WIDTH e RETROFE_WINDOW_HEIGHT).'
        return 1
    fi
    if [[ ! "$RETROFE_SCREEN_NUM" =~ ^[0-9]+$ ]]; then
        show_error 'RETROFE_SCREEN_NUM precisa ser um índice de monitor inteiro maior ou igual a zero.'
        return 1
    fi
    case "${RETROFE_FULLSCREEN,,}" in
        yes|true|1) RETROFE_FULLSCREEN='yes' ;;
        no|false|0) RETROFE_FULLSCREEN='no' ;;
        *)
            show_error 'RETROFE_FULLSCREEN aceita somente yes/no, true/false ou 1/0.'
            return 1
            ;;
    esac
    return 0
}

update_runtime_settings() {
    local settings="$ROOT/settings.conf"
    local temporary line prefix key comment
    local -A overrides seen
    overrides=(
        [baseItemPath]="$RETROFE_ROM_ROOT"
        [fullscreen]="$RETROFE_FULLSCREEN"
        [fullscreen0]="$RETROFE_FULLSCREEN"
        [fullscreen1]="$RETROFE_FULLSCREEN"
        [exitOnFirstPageBack]='no'
        [attractModeTime]="$RETROFE_ATTRACT_MODE_TIME"
        [horizontal]="$RETROFE_WINDOW_WIDTH"
        [horizontal0]="$RETROFE_WINDOW_WIDTH"
        [horizontal1]="$RETROFE_WINDOW_WIDTH"
        [screenNum0]="$RETROFE_SCREEN_NUM"
        [vertical]="$RETROFE_WINDOW_HEIGHT"
        [vertical0]="$RETROFE_WINDOW_HEIGHT"
        [vertical1]="$RETROFE_WINDOW_HEIGHT"
    )
    if [[ -n "${RETROFE_FIRST_COLLECTION:-}" ]]; then
        overrides[firstCollection]="$RETROFE_FIRST_COLLECTION"
    fi
    if [[ -n "${RETROFE_LAYOUT:-}" ]]; then
        overrides[layout]="$RETROFE_LAYOUT"
    fi
    if [[ -n "${RETROFE_LAYOUT:-}" ]]; then
        # The engine has no external modal pause API. Keep its selection stable
        # while the global Options panel is open, even after prolonged idleness.
        overrides[attractModeTime]='0'
        # CoinOPS subcollection jumps otherwise replace the advertised alphabet.
        overrides[cfwLetterSub]='no'
        overrides[jumpInputClear]='yes'
    fi
    temporary="$(mktemp "$settings.XXXXXX")" || return 1
    chmod --reference="$settings" "$temporary" 2>/dev/null || true
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" =~ ^([[:space:]]*([A-Za-z0-9_.-]+)[[:space:]]*=[[:space:]]*).*$ ]] && [[ -n "${overrides[${BASH_REMATCH[2]}]+x}" ]]; then
            prefix="${BASH_REMATCH[1]}"
            key="${BASH_REMATCH[2]}"
            if [[ -n "${seen[$key]+x}" ]]; then
                continue
            fi
            seen[$key]=1
            comment=""
            if [[ "$line" == *'#'* ]]; then
                comment=" #${line#*#}"
            fi
            printf '%s%s%s\n' "$prefix" "${overrides[$key]}" "$comment" >> "$temporary"
        else
            printf '%s\n' "$line" >> "$temporary"
        fi
    done < "$settings"
    for key in "${!overrides[@]}"; do
        if [[ -z "${seen[$key]+x}" ]]; then
            printf '%s = %s\n' "$key" "${overrides[$key]}" >> "$temporary"
        fi
    done
    if cmp -s "$temporary" "$settings"; then
        rm -f "$temporary"
    else
        if ! mv -f "$temporary" "$settings"; then
            rm -f "$temporary"
            return 1
        fi
    report "[OK] Configuração aplicada: janela ${RETROFE_WINDOW_WIDTH}x${RETROFE_WINDOW_HEIGHT}, fullscreen=$RETROFE_FULLSCREEN, screenNum=$RETROFE_SCREEN_NUM, ROMs=$RETROFE_ROM_ROOT"
    fi
}

MUSIC_PID=''
APP_PID=''
THEME_TRANSITION_DIR=''
THEME_TRANSITION_PID=''
THEME_TRANSITION_ACTIVE=0
APP_LOG_MTIME_BEFORE=''
APP_LOG_LINES_BEFORE=0
APP_LOG_FIRST_LINE_BEFORE=''
APP_READY=0
INPUT_GUARD_ACTIVE=0
INPUT_GUARD_PID=''
INPUT_GUARD_DIR="$STATE_DIR/input-session"
INPUT_DEBUG_PID=''
NAVIGATION_PANEL_PID=''
NAVIGATION_PANEL_READY=''
NAVIGATION_FAILURE=0
STEAM_SESSION_ACTIVE=0

acquire_steam_gamepad_session() {
    local output line
    output="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-acquire --pid "$$" 2>&1)" || {
        show_error "Não consegui suspender o perfil Desktop do Steam. O RetroFE não será aberto com entradas conflitantes.\n\n$output"
        return 1
    }
    while IFS= read -r line; do
        if [[ "$line" == 'RETROFE_STEAM_GAMEPAD_SESSION=1' ]]; then
            STEAM_SESSION_ACTIVE=1
            # Use Steam's virtual gamepad instead of also opening the raw Deck
            # HID driver. This environment is inherited by emulator children.
            export SDL_JOYSTICK_HIDAPI_STEAMDECK=0
            export SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD=1
        elif [[ "$line" =~ ^RETROFE_STEAM_SESSION_TOKEN=[a-f0-9]{32}$ ]]; then
            export RETROFE_STEAM_SESSION_TOKEN="${line#*=}"
        elif [[ -n "$line" ]]; then
            report "$line"
        fi
    done <<< "$output"
}

release_steam_gamepad_session() {
    [[ "$STEAM_SESSION_ACTIVE" == '1' ]] || return 0
    local output
    output="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-release --pid "$$" 2>&1)" || {
        report "[AVISO] Restauração Desktop será retomada pelo guardião: $output"
        return 1
    }
    [[ -n "$output" ]] && report "$output"
    STEAM_SESSION_ACTIVE=0
    return 0
}

inspect_input_session() {
    local recovery summary line policy
    if [[ ! -r "$INPUT_SESSION_TOOL" ]] || ! command -v python3 >/dev/null 2>&1; then
        report '[AVISO] Diagnóstico portátil de controles indisponível; nenhum dispositivo será alterado.'
        INPUT_SHORTCUT_POLICY='preserve'
        return 0
    fi

    recovery="$(python3 "$INPUT_SESSION_TOOL" guard recover --state-dir "$INPUT_GUARD_DIR" 2>&1)" || {
        report "[AVISO] $recovery"
    }
    [[ -n "$recovery" ]] && report "[INPUT] $recovery."
    summary="$(python3 "$INPUT_SESSION_TOOL" inspect --root "$ROOT" 2>&1)" || {
        report "[AVISO] Não consegui identificar os controles: $summary"
        INPUT_SHORTCUT_POLICY='preserve'
        return 0
    }
    while IFS= read -r line; do
        [[ -n "$line" ]] && report "$line"
    done <<< "$summary"
    policy="$(python3 "$INPUT_SESSION_TOOL" decision --root "$ROOT" 2>/dev/null)" || policy='preserve'
    INPUT_SHORTCUT_POLICY="$policy"
}

apply_input_shortcut_guard() {
    [[ "${INPUT_SHORTCUT_POLICY:-preserve}" == 'block' ]] || return 0
    local guard_args=(guard acquire --state-dir "$INPUT_GUARD_DIR" --pid "$APP_PID" --root "$ROOT")
    if [[ "$STEAM_SESSION_ACTIVE" == '1' ]]; then
        # Steam already has a durable watchdog for detached game processes.
        # A second watchdog could itself be captured as a Steam descendant.
        guard_args+=(--steam-owner-pid "$$" --no-watchdog)
    fi
    if python3 "$INPUT_SESSION_TOOL" "${guard_args[@]}"; then
        INPUT_GUARD_ACTIVE=1
        INPUT_GUARD_PID="$APP_PID"
        report '[INPUT] Atalhos globais do KDE suspensos; o guardião acompanha a sessão para restaurá-los após o encerramento.'
    else
        report '[AVISO] Não consegui suspender os atalhos KDE em conflito; o RetroFE continuará com os controles de jogo ativos.'
    fi
}

release_input_shortcut_guard() {
    if [[ "$INPUT_GUARD_ACTIVE" == '1' && -n "$INPUT_GUARD_PID" ]]; then
        local guard_result
        guard_result="$(python3 "$INPUT_SESSION_TOOL" guard release --state-dir "$INPUT_GUARD_DIR" --pid "$INPUT_GUARD_PID" 2>&1)" || {
            report "[AVISO] $guard_result"
        }
        [[ -n "$guard_result" ]] && report "[INPUT] $guard_result."
        INPUT_GUARD_ACTIVE=0
        INPUT_GUARD_PID=''
    fi
}

notify_theme_manual_fallback() {
    local message='A troca de tema foi salva. Pressione Q para fechar o RetroFE com segurança e continuar.'
    if command -v notify-send >/dev/null 2>&1; then
        notify-send --app-name='RetroFE' 'Troca de tema' "$message" >/dev/null 2>&1 || true
    else
        report "[TEMA] $message"
    fi
}

notify_theme_startup_slow() {
    local message='O frontend ainda não confirmou a abertura do novo layout. Confira o log do RetroFE; a interface foi revelada para não ocultar a inicialização.'
    if command -v notify-send >/dev/null 2>&1; then
        notify-send --app-name='RetroFE' 'Inicialização do tema' "$message" >/dev/null 2>&1 || true
    else
        report "[TEMA] $message"
    fi
}

start_theme_transition() {
    local output
    if [[ ! -r "$THEME_TRANSITION_TOOL" ]]; then
        report '[TEMA] A ferramenta visual de transição não está disponível; mantendo a saída manual por Q.'
        notify_theme_manual_fallback
        return 1
    fi
    THEME_TRANSITION_DIR="$(mktemp -d "$STATE_DIR/theme-transition.XXXXXX" 2>/dev/null)" || {
        report '[TEMA] Não consegui criar estado temporário para a transição visual.'
        notify_theme_manual_fallback
        return 1
    }
    if ! output="$(python3 "$THEME_TRANSITION_TOOL" begin --root "$ROOT" --state-dir "$THEME_TRANSITION_DIR" --app-pid "$APP_PID" 2>&1)"; then
        report "[TEMA] $output"
        if [[ -r "$THEME_TRANSITION_DIR/overlay.json" ]]; then
            THEME_TRANSITION_PID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("pid", ""))' "$THEME_TRANSITION_DIR/overlay.json" 2>/dev/null || true)"
        fi
        stop_theme_transition ''
        notify_theme_manual_fallback
        return 1
    fi
    THEME_TRANSITION_PID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("pid", ""))' "$THEME_TRANSITION_DIR/overlay.json" 2>/dev/null || true)"
    THEME_TRANSITION_ACTIVE=1
    report "[TEMA] $output"
    return 0
}

retarget_theme_transition() {
    [[ "$THEME_TRANSITION_ACTIVE" == '1' && -n "$THEME_TRANSITION_DIR" ]] || return 0
    local output
    output="$(python3 "$THEME_TRANSITION_TOOL" retarget --state-dir "$THEME_TRANSITION_DIR" --app-pid "$APP_PID" 2>&1)" || {
        report "[TEMA] Não consegui acompanhar a janela nova: $output"
        return 1
    }
    [[ -n "$output" ]] && report "[TEMA] $output"
}

set_theme_transition_message() {
    [[ "$THEME_TRANSITION_ACTIVE" == '1' && -n "$THEME_TRANSITION_DIR" ]] || return 0
    python3 "$THEME_TRANSITION_TOOL" status --state-dir "$THEME_TRANSITION_DIR" --message "$1" >/dev/null 2>&1 || true
}

stop_theme_transition() {
    [[ -n "$THEME_TRANSITION_DIR" ]] || return 0
    if [[ -r "$THEME_TRANSITION_TOOL" ]]; then
        python3 "$THEME_TRANSITION_TOOL" release --state-dir "$THEME_TRANSITION_DIR" --message "$1" >/dev/null 2>&1 || true
    fi
    if [[ -n "$THEME_TRANSITION_PID" ]]; then
        local attempt
        for attempt in {1..24}; do
            kill -0 "$THEME_TRANSITION_PID" 2>/dev/null || break
            sleep 0.05
        done
        if kill -0 "$THEME_TRANSITION_PID" 2>/dev/null; then
            kill -TERM "$THEME_TRANSITION_PID" 2>/dev/null || true
        fi
    fi
    if [[ -d "$THEME_TRANSITION_DIR" ]]; then
        rm -f -- "$THEME_TRANSITION_DIR"/control.json "$THEME_TRANSITION_DIR"/ready.json \
            "$THEME_TRANSITION_DIR"/closed.json "$THEME_TRANSITION_DIR"/overlay.json \
            "$THEME_TRANSITION_DIR"/overlay-error.json "$THEME_TRANSITION_DIR"/overlay.log \
            "$THEME_TRANSITION_DIR"/target.json "$THEME_TRANSITION_DIR"/shown.json
        rmdir -- "$THEME_TRANSITION_DIR" 2>/dev/null || true
    fi
    THEME_TRANSITION_DIR=''
    THEME_TRANSITION_PID=''
    THEME_TRANSITION_ACTIVE=0
}

new_frontend_log_has() {
    local pattern="$1" line_count first_line
    line_count="$(wc -l < "$ROOT/log.txt" 2>/dev/null || printf '0')"
    first_line="$(head -n 1 "$ROOT/log.txt" 2>/dev/null || true)"
    if [[ "$first_line" != "$APP_LOG_FIRST_LINE_BEFORE" ]] || [[ "$line_count" -lt "$APP_LOG_LINES_BEFORE" ]]; then
        grep -Fq "$pattern" "$ROOT/log.txt"
    elif [[ "$line_count" -gt "$APP_LOG_LINES_BEFORE" ]]; then
        tail -n +"$((APP_LOG_LINES_BEFORE + 1))" "$ROOT/log.txt" | grep -Fq "$pattern"
    else
        return 1
    fi
}

frontend_startup_ready() {
    local current_mtime expected_layout
    [[ -r "$ROOT/log.txt" ]] || return 1
    current_mtime="$(stat -c '%y' "$ROOT/log.txt" 2>/dev/null || true)"
    [[ -n "$current_mtime" && "$current_mtime" != "$APP_LOG_MTIME_BEFORE" ]] || return 1
    expected_layout="$ROOT/layouts/${RETROFE_LAYOUT:-Aeon Nox}/"
    new_frontend_log_has "$expected_layout" || return 1
    new_frontend_log_has '[Layout] Initialized' || return 1
    if [[ "$THEME_TRANSITION_ACTIVE" == '1' ]]; then
        new_frontend_log_has "[Menu] Found: \"$ROOT/collections/Main/menu.txt\"" || return 1
    else
        new_frontend_log_has '[Menu] Found:' || return 1
    fi
    new_frontend_log_has '[RetroFE] Loading playlist:' || return 1
    return 0
}

wait_for_frontend() {
    local theme_request_seen=0 ready_observations=0 navigation_restarts=0 startup_deadline=$((SECONDS + 30))
    APP_READY=0
    while kill -0 "$APP_PID" 2>/dev/null; do
        if [[ "$NAVIGATION_FAILURE" == '0' ]] && ! kill -0 "${NAVIGATION_PANEL_PID:-0}" 2>/dev/null; then
            report '[OPÇÕES] Painel interrompido; tentando recuperar a navegação essencial.'
            stop_navigation_panel
            if [[ "$navigation_restarts" -eq 0 ]] && start_navigation_panel; then
                navigation_restarts=1
            else
                NAVIGATION_FAILURE=1
                report '[OPÇÕES] Não há painel seguro; solicitando saída normal para restaurar o Desktop.'
                python3 "$NAVIGATION_PANEL_TOOL" --root "$ROOT" --app-pid "$APP_PID" --quit || true
            fi
        fi
        if [[ "$theme_request_seen" == '0' && -f "$ROOT/UserData/retrofe/theme-switch-pending.json" ]]; then
            theme_request_seen=1
            start_theme_transition || true
        fi

        if [[ "$APP_READY" == '0' && "$NAVIGATION_FAILURE" == '0' && -f "$NAVIGATION_PANEL_READY" ]] && frontend_startup_ready; then
            ready_observations=$((ready_observations + 1))
            if [[ "$ready_observations" -ge 3 ]]; then
                APP_READY=1
                python3 "$THEME_PROFILE_TOOL" --root "$ROOT" mark-started >/dev/null 2>&1 || true
                if [[ "$THEME_TRANSITION_ACTIVE" == '1' ]]; then
                    set_theme_transition_message "${RETROFE_LAYOUT:-Novo tema} carregado"
                    stop_theme_transition 'A interface está pronta'
                    report '[TEMA] O novo tema e o menu Main foram carregados; a transição visual foi encerrada.'
                fi
            fi
        else
            ready_observations=0
        fi

        if [[ "$THEME_TRANSITION_ACTIVE" == '1' && "$APP_READY" == '0' && "$SECONDS" -ge "$startup_deadline" ]]; then
            report '[TEMA] O RetroFE ainda não registrou a abertura do menu Main; encerrando a tela de transição para não ocultar um possível erro de inicialização.'
            stop_theme_transition 'A inicialização está demorando mais que o esperado'
            notify_theme_startup_slow
            startup_deadline=$((SECONDS + 30))
        fi
        sleep 0.15
    done
    if wait "$APP_PID"; then
        APP_STATUS=0
    else
        APP_STATUS=$?
    fi
    [[ "$NAVIGATION_FAILURE" == '0' ]] || APP_STATUS=70
}

start_background_music() {
    local player_path
    [[ -n "$RETROFE_MUSIC_FILE" ]] || return 0
    if [[ ! -r "$RETROFE_MUSIC_FILE" ]]; then
        report "[AVISO] Faixa de música não encontrada ou ilegível: $RETROFE_MUSIC_FILE"
        return 0
    fi
    player_path="$(command -v "$RETROFE_MUSIC_PLAYER" 2>/dev/null || true)"
    if [[ -z "$player_path" || ! -x "$player_path" ]]; then
        report "[AVISO] Player de música não encontrado ($RETROFE_MUSIC_PLAYER); o RetroFE será aberto sem a faixa de ambience."
        return 0
    fi

    rm -f "$RETROFE_MUSIC_SOCKET"
    env -u LD_LIBRARY_PATH "$player_path" \
        --no-config --no-video --force-window=no --no-terminal --really-quiet \
        --loop-file=inf --ao=pulse --audio-client-name='RetroFE Music' \
        --input-ipc-server="$RETROFE_MUSIC_SOCKET" --volume="$RETROFE_MUSIC_VOLUME" \
        "$RETROFE_MUSIC_FILE" >> "$STATE_DIR/background-music.log" 2>&1 &
    MUSIC_PID=$!
    sleep 0.25
    if ! kill -0 "$MUSIC_PID" 2>/dev/null; then
        wait "$MUSIC_PID" 2>/dev/null || true
        MUSIC_PID=''
        report "[AVISO] Não consegui iniciar a faixa de música; consulte $STATE_DIR/background-music.log"
        return 0
    fi
    report '[OK] Música aberta em um fluxo separado, ajustável pelo mixer do KDE.'
}

stop_background_music() {
    if [[ -n "$MUSIC_PID" ]] && kill -0 "$MUSIC_PID" 2>/dev/null; then
        kill -TERM "$MUSIC_PID" 2>/dev/null || true
        wait "$MUSIC_PID" 2>/dev/null || true
    fi
    MUSIC_PID=''
    rm -f "$RETROFE_MUSIC_SOCKET"
}

start_input_debug_overlay() {
    [[ "$RETROFE_INPUT_DEBUG" == '1' ]] || return 0
    if [[ ! -r "$INPUT_DEBUG_TOOL" ]] || ! command -v python3 >/dev/null 2>&1; then
        report '[AVISO] O painel de diagnóstico de entradas está indisponível; o RetroFE continuará aberto.'
        return 0
    fi
    python3 "$INPUT_DEBUG_TOOL" --root "$ROOT" --app-pid "$APP_PID" \
        >> "$STATE_DIR/input-debug-overlay.log" 2>&1 &
    INPUT_DEBUG_PID=$!
    sleep 0.2
    if kill -0 "$INPUT_DEBUG_PID" 2>/dev/null; then
        report '[INPUT DEBUG] Painel passivo aberto sobre a janela do RetroFE; use os botões e compare evento e ação esperada.'
    else
        wait "$INPUT_DEBUG_PID" 2>/dev/null || true
        INPUT_DEBUG_PID=''
        report "[AVISO] O painel de diagnóstico não iniciou; consulte $STATE_DIR/input-debug-overlay.log"
    fi
}

start_navigation_panel() {
    local attempt
    NAVIGATION_PANEL_READY="$STATE_DIR/navigation-panel-$APP_PID.json"
    rm -f -- "$NAVIGATION_PANEL_READY"
    python3 "$NAVIGATION_PANEL_TOOL" --root "$ROOT" --app-pid "$APP_PID" \
        --ready-file "$NAVIGATION_PANEL_READY" \
        >> "$STATE_DIR/navigation-panel.log" 2>&1 &
    NAVIGATION_PANEL_PID=$!
    for ((attempt=0; attempt<80; attempt++)); do
        kill -0 "$NAVIGATION_PANEL_PID" 2>/dev/null || break
        if [[ -f "$NAVIGATION_PANEL_READY" ]]; then
            export RETROFE_NAVIGATION_PANEL=1
            report "[OPÇÕES] ${RETROFE_LAYOUT}: Start/F10 e saída confirmada prontos."
            return 0
        fi
        sleep .1
    done
    report "[FALTA] O painel Options não confirmou inicialização; consulte $STATE_DIR/navigation-panel.log"
    stop_navigation_panel
    return 1
}

stop_navigation_panel() {
    if [[ -n "$NAVIGATION_PANEL_PID" ]]; then
        if kill -0 "$NAVIGATION_PANEL_PID" 2>/dev/null; then
            kill -TERM "$NAVIGATION_PANEL_PID" 2>/dev/null || true
        fi
        wait "$NAVIGATION_PANEL_PID" 2>/dev/null || true
    fi
    NAVIGATION_PANEL_PID=''
    [[ -z "$NAVIGATION_PANEL_READY" ]] || rm -f -- "$NAVIGATION_PANEL_READY"
    NAVIGATION_PANEL_READY=''
    unset RETROFE_NAVIGATION_PANEL
}

stop_input_debug_overlay() {
    if [[ -n "$INPUT_DEBUG_PID" ]]; then
        if kill -0 "$INPUT_DEBUG_PID" 2>/dev/null; then
            kill -TERM "$INPUT_DEBUG_PID" 2>/dev/null || true
        fi
        wait "$INPUT_DEBUG_PID" 2>/dev/null || true
    fi
    INPUT_DEBUG_PID=''
}

cleanup_children() {
    stop_navigation_panel
    stop_input_debug_overlay
    if [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null; then
        kill -TERM "$APP_PID" 2>/dev/null || true
        wait "$APP_PID" 2>/dev/null || true
    fi
    APP_PID=''
    stop_theme_transition ''
    stop_background_music
    release_steam_gamepad_session || true
    release_input_shortcut_guard
}

validate_host() {
    local missing_status
    MISSING_LIBRARIES=''

    if [[ ! -f "$APP" ]]; then
        show_error "Executável ausente na raiz do projeto: $APP"
        return 1
    fi
    if [[ ! -x "$APP" ]] && ! chmod u+x "$APP"; then
        show_error "O executável existe, mas não pode receber permissão de execução: $APP"
        return 1
    fi
    if [[ ! -f "$ICON" ]]; then
        show_error "Ícone do RetroFE ausente: $ICON"
        return 1
    fi
    if [[ ! -d "$LIBDIR" ]]; then
        show_error "Bibliotecas portáteis ausentes: $LIBDIR"
        return 1
    fi
    if [[ ! -r "$ROOT/settings.conf" ]]; then
        show_error "Configuração principal ausente ou ilegível: $ROOT/settings.conf"
        return 1
    fi
    if [[ ! -r "$ROOT/controls.conf" ]]; then
        show_error "Mapeamento de teclado e controles ausente ou ilegível: $ROOT/controls.conf"
        return 1
    fi
    if [[ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        show_error 'Nenhum display gráfico foi encontrado. Abra o RetroFE a partir de uma sessão KDE ativa.'
        return 1
    fi
    if ! mkdir -p "$ROOT/UserData" 2>/dev/null || [[ ! -w "$ROOT" || ! -w "$ROOT/UserData" ]]; then
        show_error "A pasta do projeto ou UserData não permite gravação. O RetroFE precisa salvar log e dados locais:\n\n$ROOT"
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        if [[ "$MODE" == 'check' ]]; then
            show_error 'Diagnóstico sem reparo: Python 3 é necessário para o launcher portátil e não está instalado.'
            return 1
        fi
        install_python_runtime || return 1
        load_saved_display_profile
        load_saved_theme_profile || return 1
    fi
    validate_display_preferences || return 1
    if [[ -n "${RETROFE_LAYOUT:-}" && ! -f "$ROOT/layouts/$RETROFE_LAYOUT/layout.xml" ]]; then
        show_error "O layout selecionado não existe nesta instalação: $RETROFE_LAYOUT"
        return 1
    fi
    if [[ -n "${RETROFE_LAYOUT_VARIANT:-}" && ! -f "$ROOT/layouts/${RETROFE_LAYOUT:-Aeon Nox}/$RETROFE_LAYOUT_VARIANT" ]]; then
        show_error "A variante de tela selecionada não existe nesta instalação: $ROOT/layouts/${RETROFE_LAYOUT:-Aeon Nox}/$RETROFE_LAYOUT_VARIANT"
        return 1
    fi

    report '[OK] Arquivos do projeto, ícone, tela gráfica e diretórios graváveis encontrados.'
    if ! find_missing_libraries; then
        return 1
    fi
    if [[ -n "$MISSING_LIBRARIES" ]]; then
        report "[FALTA] Bibliotecas compartilhadas: ${MISSING_LIBRARIES//$'\n'/, }"
        if [[ "$MODE" == 'check' ]]; then
            show_error "Diagnóstico sem reparo: faltam estas bibliotecas: $MISSING_LIBRARIES"
            return 1
        fi
        install_runtime_packages "$MISSING_LIBRARIES" || return 1
        if ! find_missing_libraries; then
            return 1
        fi
        if [[ -n "$MISSING_LIBRARIES" ]]; then
            show_error "O host ainda não resolve estas bibliotecas após o reparo: $MISSING_LIBRARIES"
            return 1
        fi
    fi
    report '[OK] Bibliotecas do executável resolvidas com as bibliotecas do projeto e do host.'
    if [[ -n "${RETROFE_LAYOUT:-}" ]]; then
        local navigation_check
        navigation_check="$(python3 "$NAVIGATION_PANEL_TOOL" --root "$ROOT" --check 2>&1)" || {
            show_error "O tema não preserva o painel de opções e a saída segura.\n\n$navigation_check"
            return 1
        }
        report "$navigation_check"
    fi
    check_optional_host_content || return 1
    report "[OK] Exibição configurada para ${RETROFE_WINDOW_WIDTH}x${RETROFE_WINDOW_HEIGHT}, screenNum=$RETROFE_SCREEN_NUM (fullscreen=$RETROFE_FULLSCREEN)."
    report '[OK] Diagnóstico do host passou; seguindo para validar ROMs e mídias.'
[[ -n "$RETROFE_WINDOW_BACKEND" ]] && report "[JANELA] $RETROFE_WINDOW_BACKEND."
if [[ -n "${WAYLAND_DISPLAY:-}" && -z "$RETROFE_WINDOW_BACKEND" ]]; then
    report '[AVISO] Não há backend XWayland para posicionar o RetroFE/overlay ou ancorar o editor de controles; o frontend usará o posicionamento padrão do SDL.'
fi
    return 0
}

SHORTCUTS_CHANGED=0

if [[ "$MODE" == 'shortcuts' ]]; then
    install_kde_shortcuts
    exit $?
fi

if ! validate_host; then
    exit 1
fi

if ! RETROFE_INPUT_BACKEND="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" input-backend 2>&1)"; then
    show_error "$RETROFE_INPUT_BACKEND"
    exit 1
fi
export RETROFE_INPUT_BACKEND
report "[CONTROLE] Entrada selecionada: $RETROFE_INPUT_BACKEND."

if [[ "$MODE" == 'check' ]]; then
    python3 "$MEDIA_PATH_TOOL" check-runtime || exit 1
    exit 0
fi

# The Python supervisor queues on the existing media lock and retains it
# through preflight, Steam setup, gameplay, theme relaunches and Desktop cleanup.
# --check stays read-only. The inherited descriptor is validated by preflight.
if [[ -z "${RETROFE_MEDIA_SESSION_FD:-}" ]]; then
    exec python3 "$MEDIA_PATH_TOOL" launch-runtime -- "${ORIGINAL_ARGUMENTS[@]}"
fi

if ! MEDIA_SETUP_OUTPUT="$(python3 "$MEDIA_PATH_TOOL" ensure-runtime 2>&1)"; then
    show_error "Não consegui validar os caminhos de ROMs e mídias dos jogos.\n\n$MEDIA_SETUP_OUTPUT"
    exit 1
fi
report "[ROMS/MÍDIA] $MEDIA_SETUP_OUTPUT"
# Only preflight needs the inherited descriptor. The supervising Python process
# retains the lease; close here so Steam/emulators cannot inherit and strand it.
exec {RETROFE_MEDIA_SESSION_FD}>&-
unset RETROFE_MEDIA_SESSION_FD

# Refresh the theme's presentation links after the canonical media preflight.
# This runs before Desktop suspension; a conflict leaves the host usable.
if [[ "${RETROFE_LAYOUT:-}" == 'Aeon Nox' ]]; then
    if ! AEON_SETUP_OUTPUT="$(python3 "$ROOT/Tools/materializers/materialize-aeon-nox-layouts.py" --root "$ROOT" --apply --quiet 2>&1)"; then
        show_error "Não consegui preparar os layouts e as referências de capas do Aeon Nox. Os arquivos conflitantes foram preservados.\n\n$AEON_SETUP_OUTPUT"
        exit 1
    fi
fi

install_kde_shortcuts || true

if [[ "$RETROFE_INPUT_BACKEND" == 'steam' ]]; then
    if ! RECOVERY_SETUP_OUTPUT="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-recovery-ensure 2>&1)"; then
        show_error "Não consegui preparar a recuperação independente do Desktop. O perfil não será suspenso.\n\n$RECOVERY_SETUP_OUTPUT"
        exit 1
    fi
    report "$RECOVERY_SETUP_OUTPUT"

    if [[ ! -r "$STEAM_INPUT_TOOL" ]] || ! command -v python3 >/dev/null 2>&1; then
        show_error "O assistente portátil de Steam Input está ausente ou Python 3 não está disponível:\n\n$STEAM_INPUT_TOOL"
        exit 1
    fi
    STEAM_SETUP_OUTPUT=''
    if ! STEAM_RECOVERY_OUTPUT="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-recover 2>&1)"; then
        show_error "Há um perfil Desktop aguardando restauração. Confira Steam/controle antes de abrir o RetroFE.\n\n$STEAM_RECOVERY_OUTPUT"
        exit 1
    fi
    [[ -n "$STEAM_RECOVERY_OUTPUT" ]] && report "$STEAM_RECOVERY_OUTPUT"
    if ! STEAM_SETUP_OUTPUT="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" ensure 2>&1)"; then
        [[ -n "$STEAM_SETUP_OUTPUT" ]] && report "[STEAM] $STEAM_SETUP_OUTPUT"
        show_error "O Steam Input do perfil Desktop ainda não está pronto. Siga a ação indicada no diagnóstico abaixo e tente abrir o RetroFE novamente.\n\n$STEAM_SETUP_OUTPUT"
        exit 1
    fi
    while IFS= read -r line; do
        [[ -n "$line" ]] && report "[STEAM] $line"
    done <<< "$STEAM_SETUP_OUTPUT"

    if ! STEAM_SESSION_PREPARE="$(python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-prepare 2>&1)"; then
        show_error "A preparação do perfil de controle ainda não está pronta.\n\n$STEAM_SESSION_PREPARE"
        exit 1
    fi
    [[ -n "$STEAM_SESSION_PREPARE" ]] && report "$STEAM_SESSION_PREPARE"
fi

if [[ -f "$ROOT/Tools/materializers/materialize-aeon-nox-navigation.py" ]] && command -v python3 >/dev/null 2>&1; then
    if ! python3 "$ROOT/Tools/materializers/materialize-aeon-nox-navigation.py" --root "$ROOT" --favorites-only --apply --quiet; then
        report '[AVISO] Não foi possível atualizar o agregado de favoritos; as listas existentes foram preservadas.'
    fi
fi

if ! update_runtime_settings; then
    show_error "Não consegui aplicar os caminhos e a resolução de janela configurados:\n\n$ROOT/settings.conf"
    exit 1
fi

cd "$ROOT" || exit 1
trap cleanup_children EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
if [[ "$RETROFE_INPUT_BACKEND" == 'steam' ]]; then
    acquire_steam_gamepad_session || exit 1
    inspect_input_session
else
    INPUT_SHORTCUT_POLICY='preserve'
fi
if ! python3 "$NAVIGATION_PANEL_TOOL" --root "$ROOT" --prepare-controls; then
    show_error 'Não consegui preparar os controles SDL e reservar Start. As configurações e seus backups foram preservados.'
    exit 1
fi
start_background_music
while :; do
    APP_LOG_MTIME_BEFORE="$(stat -c '%y' "$ROOT/log.txt" 2>/dev/null || true)"
    APP_LOG_LINES_BEFORE="$(wc -l < "$ROOT/log.txt" 2>/dev/null || printf '0')"
    APP_LOG_FIRST_LINE_BEFORE="$(head -n 1 "$ROOT/log.txt" 2>/dev/null || true)"
    "$APP" "$@" &
    APP_PID=$!
    if [[ "$STEAM_SESSION_ACTIVE" == '1' ]]; then
        python3 "$STEAM_INPUT_TOOL" --root "$ROOT" session-attach --pid "$$" --child-pid "$APP_PID" || {
            report '[AVISO] O guardião Steam acompanha o launcher; não conseguiu registrar o PID desta janela.'
        }
    fi
    align_frontend_window || true
    retarget_theme_transition || true
    NAVIGATION_FAILURE=0
    if ! start_navigation_panel; then
        NAVIGATION_FAILURE=1
        python3 "$NAVIGATION_PANEL_TOOL" --root "$ROOT" --app-pid "$APP_PID" --quit || true
    fi
    apply_input_shortcut_guard
    start_input_debug_overlay
    wait_for_frontend
    stop_navigation_panel
    stop_input_debug_overlay
    APP_PID=''
    release_input_shortcut_guard

    if [[ "$APP_STATUS" -ne 0 ]]; then
        ROLLBACK_ARGS=(--root "$ROOT" --width "$RETROFE_WINDOW_WIDTH" --height "$RETROFE_WINDOW_HEIGHT")
        [[ -n "${RETROFE_DISPLAY_PROFILE_ID:-}" ]] && ROLLBACK_ARGS+=(--display-profile-id "$RETROFE_DISPLAY_PROFILE_ID")
        ROLLBACK_OUTPUT=''
        if ROLLBACK_OUTPUT="$(python3 "$THEME_PROFILE_TOOL" "${ROLLBACK_ARGS[@]}" rollback-failed 2>&1)" && [[ "$ROLLBACK_OUTPUT" == *'TEMA_ROLLBACK=restored:'* ]]; then
            report "[TEMA] $ROLLBACK_OUTPUT"
            set_theme_transition_message 'Restaurando o tema anterior'
            if ! load_saved_theme_profile || ! validate_display_preferences || ! update_runtime_settings; then
                show_error 'O novo tema falhou e não consegui restaurar a configuração anterior. O RetroFE não será reiniciado com configuração parcial.'
                exit 1
            fi
            report '[TEMA] O layout escolhido falhou ao iniciar; reabrindo em Main com o tema anterior.'
            continue
        fi
        [[ -n "$ROLLBACK_OUTPUT" && "$ROLLBACK_OUTPUT" != 'TEMA_ROLLBACK=none' ]] && report "[TEMA] $ROLLBACK_OUTPUT"
        exit "$APP_STATUS"
    fi

    if [[ ! -f "$ROOT/UserData/retrofe/theme-switch-pending.json" ]]; then
        exit "$APP_STATUS"
    fi

    if ! consume_pending_theme; then
        report '[TEMA] A solicitação foi recusada; restaurando as preferências anteriores e reabrindo em Main.'
        set_theme_transition_message 'Mantendo o tema atual'
        if ! load_saved_theme_profile || ! validate_display_preferences || ! update_runtime_settings; then
            show_error 'A troca do tema foi recusada e não consegui relançar a configuração anterior.'
            exit 1
        fi
        continue
    fi
    if ! load_saved_theme_profile || ! validate_display_preferences || ! update_runtime_settings; then
        show_error 'Não consegui aplicar a troca de tema. O RetroFE não será reiniciado com configuração parcial.'
        exit 1
    fi
    set_theme_transition_message "Carregando ${RETROFE_LAYOUT:-o novo tema}"
    report '[TEMA] O RetroFE foi encerrado normalmente; reabrindo com o tema escolhido em Main.'
done
