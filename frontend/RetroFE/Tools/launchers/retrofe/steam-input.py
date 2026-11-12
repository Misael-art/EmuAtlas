#!/usr/bin/env python3
"""Prepare Steam Input for the RetroFE Steam Deck controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

import window_placement
import desktop_environment
import steam_desktop_session


DEFAULT_ROOT = Path(__file__).resolve().parents[3]
APP_ID_DESKTOP = "413080"
FLATPAK_APP_ID = "com.valvesoftware.Steam"
TEMPLATE_NAME = "retrofe_steamdeck_desktop.vdf"
TEMPLATE_SOURCE_REL = Path("Config/steam-input/retrofe_steamdeck_desktop.vdf.in")
SETUP_STATE_REL = Path("UserData/retrofe/steam-input-setup.json")
ACCOUNT_ID64_BASE = 76561197960265728
VDF_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{}]|//[^\r\n]*')


class SteamInstall(NamedTuple):
    kind: str
    command: tuple[str, ...]
    root: Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def xdg_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name)
    return Path(value).expanduser() if value else fallback


def data_home() -> Path:
    return xdg_path("XDG_DATA_HOME", Path.home() / ".local/share")


def config_home() -> Path:
    return xdg_path("XDG_CONFIG_HOME", Path.home() / ".config")


def steam_roots(kind: str | None = None) -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    override = os.environ.get("RETROFE_STEAM_ROOT") or os.environ.get("STEAM_DIR")
    if override:
        roots.append(Path(override).expanduser())
    if kind == "flatpak":
        roots.append(data_home() / "Steam") if data_home() != home / ".local/share" else None
        roots.append(home / ".var/app" / FLATPAK_APP_ID / ".local/share/Steam")
    roots.extend([
        home / ".steam/steam",
        home / ".steam/root",
        home / ".local/share/Steam",
        home / ".var/app" / FLATPAK_APP_ID / ".local/share/Steam",
    ])
    roots.extend(Path(item).expanduser() for item in os.environ.get("STEAM_COMPAT_CLIENT_INSTALL_PATH", "").split(os.pathsep) if item)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in roots:
        resolved = path.resolve(strict=False)
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def find_steam() -> SteamInstall | None:
    native = shutil.which("steam")
    if native:
        for root in steam_roots("native"):
            if (root / "steamapps").is_dir() or (root / "controller_base").is_dir() or root.name == "Steam":
                return SteamInstall("native", (native,), root)
        return SteamInstall("native", (native,), steam_roots("native")[0])
    flatpak = shutil.which("flatpak")
    if flatpak:
        result = run([flatpak, "info", "--show-ref", FLATPAK_APP_ID], timeout=30)
        if result.returncode == 0:
            root = next((path for path in steam_roots("flatpak") if path.name == "Steam"), steam_roots("flatpak")[0])
            return SteamInstall("flatpak", (flatpak, "run", FLATPAK_APP_ID), root)
    return None


def system_family() -> str:
    values: list[str] = []
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                values.append(line.split("=", 1)[1].strip().strip('"').casefold())
    except OSError:
        pass
    return " ".join(values)


def privileged(command: list[str]) -> list[str]:
    pkexec = shutil.which("pkexec")
    if pkexec:
        return [pkexec, *command]
    sudo = shutil.which("sudo")
    if sudo and sys.stdin.isatty():
        return [sudo, *command]
    raise RuntimeError("Falta pkexec para autorizar a instalação do Steam; abra o launcher no KDE ou instale Steam pelo gerenciador do host.")


def ensure_flathub(flatpak: str) -> None:
    result = run([flatpak, "remotes", "--user", "--columns=name"], timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Não consegui consultar os repositórios Flatpak do usuário.")
    if "flathub" in {line.strip().casefold() for line in result.stdout.splitlines()}:
        return
    result = run([flatpak, "remote-add", "--user", "--if-not-exists", "flathub", "https://flathub.org/repo/flathub.flatpakrepo"], timeout=180)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Não consegui habilitar o repositório Flathub do usuário.")


def install_steam() -> SteamInstall:
    family = system_family()
    flatpak = shutil.which("flatpak")
    native_command: list[str] | None = None
    if ("arch" in family or shutil.which("pacman")) and shutil.which("pacman"):
        native_command = ["pacman", "-S", "--needed", "steam"]
    elif ("debian" in family or "ubuntu" in family) and shutil.which("apt-get"):
        native_command = ["apt-get", "install", "-y", "steam-installer"]
    elif ("fedora" in family or "rhel" in family) and shutil.which("dnf"):
        native_command = ["dnf", "install", "-y", "steam"]
    elif ("opensuse" in family or "suse" in family) and shutil.which("zypper"):
        native_command = ["zypper", "--non-interactive", "install", "steam"]

    failures: list[str] = []
    if native_command:
        try:
            command = privileged(native_command)
            # Package managers must not wait on a terminal that a KDE launcher
            # does not provide. The user's system authorization still happens
            # through pkexec/sudo before this noninteractive package action.
            if command[-len(native_command):][0] == "pacman" and "--noconfirm" not in command:
                command.append("--noconfirm")
            result = run(command, timeout=1800)
            if result.returncode == 0:
                found = find_steam()
                if found:
                    return found
            failures.append(result.stderr.strip() or result.stdout.strip() or "O gerenciador nativo não instalou Steam.")
        except RuntimeError as exc:
            failures.append(str(exc))
    if flatpak:
        ensure_flathub(flatpak)
        result = run([flatpak, "install", "--user", "--noninteractive", "flathub", FLATPAK_APP_ID], timeout=1800)
        if result.returncode == 0:
            found = find_steam()
            if found:
                return found
        failures.append(result.stderr.strip() or result.stdout.strip() or "Flatpak não instalou Steam.")
    details = "\n".join(item for item in failures if item)
    if not native_command and not flatpak:
        details = "Este host não tem um gerenciador de pacotes compatível nem Flatpak."
    raise RuntimeError("Não consegui instalar Steam pelo repositório oficial do host." + (f"\n\n{details}" if details else ""))


def process_running() -> bool:
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip().casefold()
        except OSError:
            continue
        if comm == "steam":
            return True
    return False


def start_steam(install: SteamInstall, timeout: float = 45.0) -> None:
    if process_running():
        return
    command = [*install.command, "-silent"]
    systemd_run = shutil.which("systemd-run")
    if systemd_run and run(["systemctl", "--user", "show-environment"], timeout=5).returncode == 0:
        # A new POSIX session alone still belongs to the launcher's cgroup.
        command = [systemd_run, "--user", "--scope", "--quiet", "--collect",
                   "--unit=emulation-steam", "--", *command]
    subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_running():
            return
        time.sleep(0.25)
    raise RuntimeError("Steam recebeu o pedido de abertura, mas o processo não ficou ativo em 45 segundos. Confira a janela de login/atualização do Steam.")


def decode_vdf_string(token: str) -> str:
    return json.loads(token)


def vdf_tokens(text: str) -> list[re.Match[str]]:
    return [match for match in VDF_TOKEN.finditer(text) if not match.group().startswith("//")]


def matching_brace(tokens: list[re.Match[str]], open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(tokens)):
        token = tokens[index].group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("VDF sem fechamento de bloco; conteúdo preservado.")


def find_scope(tokens: list[re.Match[str]], name: str) -> tuple[int, int] | None:
    for index, token in enumerate(tokens[:-1]):
        if token.group().startswith('"') and decode_vdf_string(token.group()) == name and tokens[index + 1].group() == "{":
            opening = index + 1
            return opening, matching_brace(tokens, opening)
    return None


def update_controller_selector(text: str, template_name: str) -> tuple[str, bool]:
    tokens = vdf_tokens(text)
    scope = find_scope(tokens, "controller_config")
    encoded = json.dumps(template_name, ensure_ascii=False)
    if scope is None:
        if text.strip():
            raise ValueError("VDF não contém controller_config; seletor Steam preservado.")
        return f'"controller_config"\n{{\n\t"{APP_ID_DESKTOP}"\n\t{{\n\t\t"template"\t{encoded}\n\t}}\n}}\n', True
    root_open, root_close = scope
    app_scope: tuple[int, int] | None = None
    index = root_open + 1
    while index < root_close:
        if tokens[index].group().startswith('"') and index + 1 < root_close and tokens[index + 1].group() == "{":
            key = decode_vdf_string(tokens[index].group())
            child_open = index + 1
            child_close = matching_brace(tokens, child_open)
            if key == APP_ID_DESKTOP:
                app_scope = (child_open, child_close)
                break
            index = child_close + 1
            continue
        index += 1
    if app_scope is None:
        insertion = tokens[root_close].start()
        block = f'\n\t"{APP_ID_DESKTOP}"\n\t{{\n\t\t"template"\t{encoded}\n\t}}\n'
        return text[:insertion] + block + text[insertion:], True
    app_open, app_close = app_scope
    index = app_open + 1
    while index + 1 < app_close:
        token = tokens[index].group()
        if token.startswith('"') and decode_vdf_string(token) == "template" and tokens[index + 1].group().startswith('"'):
            value = tokens[index + 1]
            if decode_vdf_string(value.group()) == template_name:
                return text, False
            return text[:value.start()] + encoded + text[value.end():], True
        index += 1
    insertion = tokens[app_close].start()
    return text[:insertion] + f'\n\t\t"template"\t{encoded}\n\t' + text[insertion:], True


def active_account_id64(root: Path) -> tuple[str | None, str | None]:
    loginusers = root / "config/loginusers.vdf"
    if not loginusers.is_file():
        return None, None
    text = loginusers.read_text(encoding="utf-8", errors="replace")
    tokens = vdf_tokens(text)
    users_scope = find_scope(tokens, "users")
    if users_scope is None:
        return None, None
    users_open, users_close = users_scope
    found: list[tuple[str, bool]] = []
    index = users_open + 1
    while index < users_close:
        if tokens[index].group().startswith('"') and index + 1 < users_close and tokens[index + 1].group() == "{":
            steam_id = decode_vdf_string(tokens[index].group())
            child_open = index + 1
            child_close = matching_brace(tokens, child_open)
            if steam_id.isdigit() and len(steam_id) == 17:
                child = text[tokens[child_open].end():tokens[child_close].start()]
                recent = bool(re.search(r'(?m)^\s*"MostRecent"\s*"1"', child))
                found.append((steam_id, recent))
            index = child_close + 1
            continue
        index += 1
    selected = next((steam_id for steam_id, recent in found if recent), found[0][0] if found else None)
    if not selected:
        return None, None
    account32 = str(int(selected) - ACCOUNT_ID64_BASE)
    return selected, account32 if int(account32) > 0 else None


def controller_config_path(root: Path, account32: str | None) -> Path | None:
    if not account32:
        return None
    direct = root / "steamapps/common/Steam Controller Configs" / account32 / "config/configset_controller_neptune.vdf"
    candidates = [direct]
    for library in (root / "steamapps/libraryfolders.vdf", root / "config/libraryfolders.vdf"):
        if not library.is_file():
            continue
        text = library.read_text(encoding="utf-8", errors="replace")
        paths = re.findall(r'"path"\s*"((?:\\.|[^"\\])*)"', text)
        for raw_path in paths:
            path = Path(raw_path.replace("\\\\", "\\").replace("\\\"", '"')).expanduser()
            candidate = path / "steamapps/common/Steam Controller Configs" / account32 / "config/configset_controller_neptune.vdf"
            if candidate not in candidates:
                candidates.append(candidate)
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def template_payload(root: Path, steam_id64: str | None, source_rel: Path = TEMPLATE_SOURCE_REL) -> bytes:
    source = root / source_rel
    payload = source.read_bytes()
    placeholder = b"@STEAM_ACCOUNT_ID64@"
    if payload.count(placeholder) != 1:
        raise ValueError(f"Template Steam inválido ou placeholder duplicado: {source}")
    creator = (steam_id64 or "0").encode("ascii")
    return payload.replace(placeholder, creator, 1)


def template_semantic(payload: bytes) -> bytes:
    # Steam's creator field belongs to the profile/account that first wrote
    # the template. The active file is Steam-owned, so compare its mappings
    # while ignoring only that account-specific metadata field.
    return re.sub(rb'("creator"\s+")[0-9]+("\s*)', rb'\1@STEAM_ACCOUNT_ID64@\2', payload, count=1)


def atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    if path.is_symlink():
        raise ValueError(f"Destino Steam é symlink e foi preservado: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_template(root: Path, install: SteamInstall, steam_id64: str | None,
                     template_name: str = TEMPLATE_NAME,
                     source_rel: Path = TEMPLATE_SOURCE_REL) -> tuple[Path, str]:
    source = root / source_rel
    if not source.is_file():
        raise RuntimeError(f"Template Steam Deck ausente no projeto: {source}")
    destination = install.root / "controller_base/templates" / template_name
    expected = template_payload(root, steam_id64, source_rel)
    if destination.is_symlink():
        raise RuntimeError(f"Template Steam existente é symlink e foi preservado: {destination}")
    if destination.is_file():
        current = destination.read_bytes()
        if template_semantic(current) == template_semantic(expected):
            return destination, "matching"
        return destination, "conflict-preserved"
    atomic_write(destination, expected, 0o644)
    return destination, "installed"


def menu_entry_candidates() -> list[Path]:
    return [
        Path("/usr/share/applications/steam.desktop"),
        Path("/usr/local/share/applications/steam.desktop"),
        data_home() / "applications/steam.desktop",
        data_home() / f"applications/{FLATPAK_APP_ID}.desktop",
        data_home() / "applications/retrofe-steam-input.desktop",
        data_home() / "applications/retrofe-steam-input-managed.desktop",
    ]


def current_desktop() -> Path:
    command = shutil.which("xdg-user-dir")
    if command:
        result = run([command, "DESKTOP"], timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).expanduser()
    user_dirs = config_home() / "user-dirs.dirs"
    if user_dirs.is_file():
        match = re.search(r'^XDG_DESKTOP_DIR="\$HOME/(.*)"$', user_dirs.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
        if match:
            return Path.home() / match.group(1)
    return Path.home() / "Desktop"


def make_desktop_entry(install: SteamInstall) -> bytes:
    if install.kind == "native":
        exec_line = "steam %U"
        icon = "steam"
    else:
        exec_line = f"flatpak run {FLATPAK_APP_ID} %U"
        icon = FLATPAK_APP_ID
    return (
        "[Desktop Entry]\nVersion=1.0\nType=Application\nName=Steam\nName[pt_BR]=Steam\n"
        "Comment=Abre o Steam Input e os jogos Steam\n"
        f"Exec={exec_line}\nIcon={icon}\nTerminal=false\nCategories=Game;\n"
        "X-RetroFE-Steam-Input=true\n"
        "MimeType=x-scheme-handler/steam;x-scheme-handler/steamlink;\n"
    ).encode("utf-8")


def is_managed_steam_entry(path: Path) -> bool:
    try:
        return "X-RetroFE-Steam-Input=true" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def ensure_shortcuts(install: SteamInstall) -> dict[str, bool]:
    menu_entries = [path for path in menu_entry_candidates() if path.is_file()]
    user_entry_candidates = [data_home() / "applications/retrofe-steam-input.desktop", data_home() / "applications/retrofe-steam-input-managed.desktop"]
    user_entry = next((path for path in user_entry_candidates if path.is_file() and is_managed_steam_entry(path)), None)
    if user_entry is None:
        user_entry = next((path for path in user_entry_candidates if not path.exists()), user_entry_candidates[-1])
    menu_created = False
    usable_menu_entries = [path for path in menu_entries if desktop_entry_usable(path, install)]
    if not usable_menu_entries:
        if user_entry.is_file():
            if is_managed_steam_entry(user_entry) and not desktop_entry_usable(user_entry, install):
                atomic_write(user_entry, make_desktop_entry(install), 0o755)
                menu_created = True
        elif not user_entry.exists():
            atomic_write(user_entry, make_desktop_entry(install), 0o755)
            menu_created = True
    desktop_dir = current_desktop()
    desktop_created = False
    desktop_dir.mkdir(parents=True, exist_ok=True)
    desktop_candidates = [desktop_dir / name for name in ("steam.desktop", f"{FLATPAK_APP_ID}.desktop", "retrofe-steam-input.desktop", "Steam Input.desktop", "RetroFE Steam Input.desktop")]
    usable_desktop_entries = [path for path in desktop_candidates if path.is_file() and desktop_entry_usable(path, install)]
    if not usable_desktop_entries:
        source = next((path for path in usable_menu_entries if path.is_file()), user_entry if user_entry.is_file() else None)
        existing_managed = next((path for path in desktop_candidates if path.is_file() and is_managed_steam_entry(path)), None)
        existing_desktop = next((path for path in desktop_candidates if path.exists()), None)
        if existing_managed:
            destination = existing_managed
        elif existing_desktop:
            destination = next(
                (desktop_dir / name for name in ("retrofe-steam-input.desktop", "Steam Input.desktop", "RetroFE Steam Input.desktop") if not (desktop_dir / name).exists()),
                None,
            )
        else:
            destination = desktop_dir / "steam.desktop"
        if destination is None:
            raise RuntimeError("Já existem atalhos Steam Desktop não gerenciados; nenhum será sobrescrito.")
        atomic_write(destination, source.read_bytes() if source else make_desktop_entry(install), 0o755)
        desktop_created = True
    autostart_dir = config_home() / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    expected_exec = "steam -silent" if install.kind == "native" else f"flatpak run {FLATPAK_APP_ID} -silent"
    matching_autostart = False
    for name in ("steam-input.desktop", "retrofe-steam-input.desktop", "steam.desktop"):
        candidate = autostart_dir / name
        if candidate.is_file() and expected_exec in candidate.read_text(encoding="utf-8", errors="replace"):
            matching_autostart = True
            break
    autostart_created = False
    if not matching_autostart:
        destination = autostart_dir / "retrofe-steam-input.desktop"
        payload = (
            "[Desktop Entry]\nType=Application\nName=Steam Input\n"
            "Comment=Disponibiliza Steam Input para os controles do RetroFE\n"
            f"Exec={expected_exec}\nTerminal=false\nNoDisplay=true\n"
            "X-GNOME-Autostart-enabled=true\nX-KDE-autostart-after=panel\n"
        ).encode("utf-8")
        if not destination.exists() or ("Name=Steam Input" in destination.read_text(encoding="utf-8", errors="replace") and destination.read_bytes() != payload):
            atomic_write(destination, payload, 0o755)
            autostart_created = True
    if menu_created:
        cache_tool = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
        if cache_tool:
            run([cache_tool, "--noincremental"], timeout=30)
    return {"menu_created": menu_created, "desktop_created": desktop_created, "autostart_created": autostart_created}


def desktop_entry_usable(path: Path, install: SteamInstall) -> bool:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    raw_exec = next((line.partition("=")[2] for line in lines if line.startswith("Exec=")), "")
    try:
        command = shlex.split(raw_exec)
    except ValueError:
        return False
    if not command:
        return False
    if install.kind == "native":
        return Path(command[0]).name == "steam" and bool(shutil.which(command[0]) or Path(command[0]).is_file())
    if Path(command[0]).name != "flatpak" or len(command) < 3:
        return False
    return command[1] == "run" and FLATPAK_APP_ID in command[2:]


def setup_selector(root: Path, install: SteamInstall, steam_id64: str | None, account32: str | None) -> tuple[str, Path | None]:
    path = controller_config_path(install.root, account32)
    if path is None:
        return "needs-steam-sign-in", None
    if path.is_symlink():
        return "selector-symlink-preserved", path
    if not path.is_file():
        payload = '"controller_config"\n{\n}\n'
    else:
        payload = path.read_text(encoding="utf-8", errors="replace")
    updated, changed = update_controller_selector(payload, TEMPLATE_NAME)
    if not changed:
        return "configured", path
    backup = path.with_name(path.name + ".pre-retrofe-" + datetime.now().strftime("%Y%m%dT%H%M%S"))
    if path.is_file() and not backup.exists():
        shutil.copy2(path, backup)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    atomic_write(path, updated.encode("utf-8"), mode)
    return "configured-updated", path


def portable_selector_label(install: SteamInstall, selector_path: Path | None) -> str | None:
    if selector_path is None:
        return None
    try:
        relative = selector_path.relative_to(install.root).as_posix()
    except ValueError:
        return "external-steam-library/controller-config-selector.vdf"
    return re.sub(r"(Steam Controller Configs/)[^/]+/", r"\1<account>/", relative, count=1)


def launch_uri(install: SteamInstall, uri: str) -> None:
    subprocess.Popen([*install.command, uri], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def open_desktop_controller_config(root: Path, install: SteamInstall) -> None:
    target = window_placement.find_window(class_hint="RetroFE", title_hint="RetroFE")
    before = window_placement.list_windows()
    before_ids = {window.window_id for window in before}
    previous_controller = window_placement.find_window(
        class_hint="steam", title_hint="controller", visible_only=False
    )
    launch_uri(install, f"steam://controllerconfig/{APP_ID_DESKTOP}")
    if not target:
        print("[AVISO] A configuração do Steam abriu, mas não encontrei a janela X11/XWayland do RetroFE para alinhá-la.")
        return
    if not window_placement.backend_available():
        print("[AVISO] A configuração do Steam abriu; o host não oferece wmctrl/xdotool/xprop para alinhá-la ao RetroFE.")
        return
    before_by_id = {window.window_id: window for window in before}
    steam_window = window_placement.wait_for_controller_window(before_ids, timeout=12.0)
    if not steam_window:
        print("[AVISO] A configuração do Steam foi solicitada, mas não encontrei a janela Steam no backend X11/XWayland.")
        return
    same_window = previous_controller is not None and previous_controller.window_id == steam_window.window_id
    original = previous_controller.frame if same_window and previous_controller else None
    original_minimized = previous_controller.minimized if same_window and previous_controller else False
    original_fullscreen = previous_controller.fullscreen if same_window and previous_controller else steam_window.fullscreen
    aligned = window_placement.place_window(steam_window.window_id, target.frame, activate=True)
    if (not aligned and window_placement.matches_monitor_rectangle(target.frame)
            and window_placement.set_fullscreen(steam_window.window_id, True)):
        actual = window_placement.window_by_id(steam_window.window_id)
        aligned = bool(actual and window_placement.rectangles_match(actual.frame, target.frame))
    if not aligned:
        window_placement.set_fullscreen(steam_window.window_id, steam_window.fullscreen)
        fallback_geometry = original or steam_window.frame
        if not steam_window.fullscreen:
            window_placement.place_window(steam_window.window_id, fallback_geometry, activate=False)
        print("[AVISO] A janela de edição Steam não cabe no retângulo do RetroFE; mantive a geometria anterior.")
        return
    state_file = root / "UserData/retrofe/controller-window-restore.json"
    additional_windows: list[dict[str, object]] = []
    for candidate in window_placement.list_windows():
        if candidate.window_id == steam_window.window_id or "steam" not in (
                candidate.wm_class + " " + candidate.title).casefold():
            continue
        previous = before_by_id.get(candidate.window_id)
        additional_windows.append({
            "window_id": candidate.window_id,
            "original_geometry": previous.frame.as_dict() if previous else None,
            "original_minimized": previous.minimized if previous else True,
            "original_fullscreen": previous.fullscreen if previous else False,
        })
    try:
        watcher_pid = window_placement.start_restore_monitor(
            state_file,
            steam_window=steam_window,
            retrofe_window_id=target.window_id,
            original=original,
            original_minimized=original_minimized,
            original_fullscreen=original_fullscreen,
            additional_windows=additional_windows,
        )
    except (OSError, ValueError) as exc:
        watcher_pid = 0
        print(f"[AVISO] Steam foi alinhado, mas não consegui preparar a restauração da posição: {exc}")
    geometry = target.frame
    print(f"[OK] A janela de edição Steam ({steam_window.title}) foi alinhada a {geometry.width}x{geometry.height}+{geometry.x}+{geometry.y} sobre o RetroFE.")
    if watcher_pid:
        print("[OK] A posição anterior do Steam será restaurada quando o RetroFE voltar ao foco ou encerrar.")


def status_data(root: Path, install: SteamInstall | None) -> dict[str, object]:
    active = process_running()
    if install is None:
        return {"installed": False, "active": active, "steam_kind": None, "desktop_menu_entry": False}
    steam_id64, account32 = active_account_id64(install.root)
    selector = controller_config_path(install.root, account32)
    selector_status = "needs-apply" if steam_id64 else "needs-steam-sign-in"
    if selector and selector.is_file():
        changed = update_controller_selector(selector.read_text(encoding="utf-8", errors="replace"), TEMPLATE_NAME)[1]
        selector_status = "needs-apply" if changed else "configured"
    if steam_desktop_session.snapshot():
        selector_status = "suspended-for-retrofe"
    profile_path = install.root / "controller_base/templates" / TEMPLATE_NAME
    profile_matches = bool(
        profile_path.is_file()
        and steam_id64
        and not profile_path.is_symlink()
        and template_semantic(profile_path.read_bytes()) == template_semantic(template_payload(root, steam_id64))
    )
    matching_entry = any(path.is_file() and desktop_entry_usable(path, install) for path in menu_entry_candidates())
    return {
        "installed": True,
        "active": active,
        "steam_kind": install.kind,
        "steam_root_kind": "native" if install.kind == "native" else "flatpak",
        "desktop_menu_entry": matching_entry,
        "desktop_menu_entry_path": next((str(path) for path in menu_entry_candidates() if path.is_file() and desktop_entry_usable(path, install)), None),
        "desktop_shortcut": any(
            (current_desktop() / name).is_file() and desktop_entry_usable(current_desktop() / name, install)
            for name in ("steam.desktop", f"{FLATPAK_APP_ID}.desktop", "retrofe-steam-input.desktop", "Steam Input.desktop", "RetroFE Steam Input.desktop")
        ),
        "desktop_shortcut_path": next(
            (str(current_desktop() / name) for name in ("steam.desktop", f"{FLATPAK_APP_ID}.desktop", "retrofe-steam-input.desktop", "Steam Input.desktop", "RetroFE Steam Input.desktop")
             if (current_desktop() / name).is_file() and desktop_entry_usable(current_desktop() / name, install)),
            None,
        ),
        "autostart": any((config_home() / "autostart" / name).is_file() for name in ("steam-input.desktop", "retrofe-steam-input.desktop", "steam.desktop")),
        "steam_account_available": bool(steam_id64),
        "desktop_controller_profile": selector_status,
        "profile_template_present": profile_path.is_file(),
        "profile_template_matches": profile_matches,
    }


def record_environment_change(root: Path, details: dict[str, object]) -> str | None:
    project_root = next(
        (candidate for candidate in (root, *root.parents) if (candidate / "tools/roms/romtool.py").is_file()),
        None,
    )
    if project_root is None:
        print("[AVISO] romtool.py não acompanha esta cópia portátil; registre a mudança externa no histórico do projeto hospedeiro.")
        return None
    command = [
        sys.executable,
        str(project_root / "tools/roms/romtool.py"),
        "record-change",
        "--change-type",
        "retrofe-steam-input-bootstrap",
        "--reason",
        "Preparar Steam Input Desktop exigido pelos atalhos do RetroFE, criar apenas itens de integração ausentes e manter o teclado como alternativa.",
        "--details-json",
        json.dumps(details, ensure_ascii=False, sort_keys=True),
    ]
    result = run(command, timeout=180)
    if result.returncode != 0:
        print(f"[AVISO] Não consegui registrar a mudança externa no histórico: {result.stderr.strip() or result.stdout.strip()}")
        return None
    try:
        operation_id = str(json.loads(result.stdout).get("operation_id", ""))
    except json.JSONDecodeError:
        operation_id = ""
    print(f"[REGISTRO] Mudança externa registrada no romtool: {operation_id or 'operation id indisponível'}.")
    return operation_id or None


def ensure(root: Path) -> tuple[SteamInstall, dict[str, object]]:
    install = find_steam()
    installed_by_run = install is None
    if install is None:
        install = install_steam()
    account_id64, account32 = active_account_id64(install.root)
    if account_id64:
        _, template_state = install_template(root, install, account_id64)
    else:
        template_state = "waiting-for-steam-sign-in"
    shortcuts = ensure_shortcuts(install)
    suspended = steam_desktop_session.snapshot()
    if suspended:
        selector_state, selector_path = "suspended-for-retrofe", controller_config_path(install.root, account32)
    else:
        selector_state, selector_path = setup_selector(root, install, account_id64, account32)
    running_before = process_running()
    started_by_run = not running_before
    if running_before and selector_state == "configured-updated":
        launch_uri(install, f"steam://controllerconfig/{APP_ID_DESKTOP}")
    if not running_before:
        start_steam(install)
    current = status_data(root, install)
    state = {
        "schema_version": 1,
        "steam_kind": install.kind,
        "template_sha256": hashlib.sha256(template_semantic(template_payload(root, account_id64))).hexdigest(),
        "template_state": "matching" if template_state == "installed" else template_state,
        "selector_state": "configured" if selector_state == "configured-updated" else selector_state,
        "selector_relative": portable_selector_label(install, selector_path),
        "shortcuts": shortcuts,
        "steam_active_after_check": process_running(),
        "setup_completed_at_utc": utc_now(),
    }
    state_path = root / SETUP_STATE_REL
    existing: dict[str, object] = {}
    if state_path.is_file() and not state_path.is_symlink():
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    comparable = {key: value for key, value in state.items() if key != "setup_completed_at_utc"}
    old_comparable = {key: value for key, value in existing.items() if key != "setup_completed_at_utc"}
    if comparable != old_comparable:
        atomic_write(state_path, (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    external_actions = []
    if installed_by_run:
        external_actions.append("steam-installed")
    if template_state == "installed":
        external_actions.append("steam-controller-template-installed")
    if selector_state == "configured-updated":
        external_actions.append("steam-appid-413080-controller-selection-updated")
    external_actions.extend(name for name, created in shortcuts.items() if created)
    if started_by_run:
        external_actions.append("steam-client-started")
    if external_actions:
        record_environment_change(root, {
            "actions": external_actions,
            "steam_kind": install.kind,
            "steam_root": str(install.root),
            "steam_was_installed_before": not installed_by_run,
            "steam_was_active_before": running_before,
            "steam_active_after": process_running(),
            "steam_account_available": bool(account_id64),
            "controller_template_state": template_state,
            "controller_selector_state": selector_state,
            "controller_selector_relative": portable_selector_label(install, selector_path),
            "shortcuts": shortcuts,
        })
    if template_state == "conflict-preserved":
        print(f"[AVISO] O template Steam já existia com outro conteúdo e foi preservado: {install.root / 'controller_base/templates' / TEMPLATE_NAME}")
    if selector_state == "suspended-for-retrofe":
        print("[OK] Perfil Desktop salvo pelo guardião; Gamepad exclusivo acompanha a sessão RetroFE.")
    elif selector_state == "needs-steam-sign-in":
        print("[AVISO] Steam foi localizado/iniciado, mas ainda não há perfil Steam autenticado para vincular o layout Desktop do Steam Deck. Faça login uma vez; o próximo início concluirá essa etapa.")
    elif selector_state == "selector-symlink-preserved":
        print(f"[AVISO] Seletor Steam é symlink e foi preservado para revisão: {selector_path}")
    elif selector_state in {"configured", "configured-updated"} and not running_before and selector_path:
        print("[OK] Perfil Steam Deck Desktop associado ao AppID 413080 antes de iniciar Steam.")
    elif selector_state in {"configured", "configured-updated"} and running_before and status_data(root, install)["desktop_controller_profile"] == "configured":
        print("[OK] Steam já estava ativo e o perfil Desktop do Steam Deck estava selecionado.")
    print(f"[OK] Steam ativo: {process_running()}; menu Steam: {current['desktop_menu_entry']}; atalho Desktop: {current['desktop_shortcut']}; autostart: {current['autostart']}.")
    if not account_id64:
        print("[FALTA] Conclua o login no Steam. A associação do perfil será feita na próxima abertura do RetroFE.")
    elif selector_state not in {"configured", "configured-updated", "suspended-for-retrofe"}:
        print(f"[FALTA] O perfil Desktop do AppID 413080 não foi associado ({selector_state}); o arquivo existente foi preservado.")
    elif not current.get("profile_template_matches"):
        print("[FALTA] O template Steam existente não corresponde aos mapeamentos deste projeto; ele foi preservado para revisão.")
    if not current.get("desktop_menu_entry"):
        print("[FALTA] Não encontrei uma entrada Steam utilizável no menu de aplicativos do KDE.")
    return install, current



def recover_session_once(root: Path) -> None:
    """Recover KDE even if Steam is unavailable; preserve pending Steam state."""
    input_api = runpy.run_path(str(Path(__file__).with_name("input-session.py")))
    ok, message = input_api["recover_guard"](steam_desktop_session.state_directory())
    errors = [] if ok else [message]
    state = steam_desktop_session.snapshot()
    if state and not steam_desktop_session.has_live_owners():
        try:
            if not process_running():
                install = find_steam()
                if not install:
                    raise RuntimeError("Steam ausente; recuperação original preservada.")
                start_steam(install)
            steam_desktop_session.release(recover=True)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
    # Local keyboard/audio must recover even while Steam cannot restore its
    # profile. Keep Steam's original selection independently for later retry.
    pending = steam_desktop_session.snapshot()
    game_alive = bool(pending) and steam_desktop_session.has_live_owners()
    if ok and not game_alive:
        try:
            repaired = desktop_environment.restore(steam_desktop_session.state_directory())
            if repaired:
                print("[RECUPERAÇÃO] Desktop restaurado: " + ", ".join(repaired), flush=True)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))


def recovery_loop(root: Path) -> None:
    """Independent login/session safety net, outside the frontend process group."""
    import fcntl
    directory = steam_desktop_session.state_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / "recovery-loop.lock"
    if lock.is_symlink():
        raise RuntimeError("Lock de recuperação é symlink; preservado.")
    with lock.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[RECUPERAÇÃO] Outro guardião de login já está ativo.", flush=True)
            return
        print("[RECUPERAÇÃO] Guardião independente ativo; recupera KDE/Steam após saída, falha ou reinício.", flush=True)
        last_error, last_logged, failing_since = None, 0.0, None
        while True:
            try:
                recover_session_once(root)
                last_error, failing_since = None, None
            except (OSError, ValueError, RuntimeError) as exc:
                message = str(exc)
                failing_since = failing_since or time.monotonic()
                # A game in progress keeps the gamepad profile on purpose; only
                # a session with no live owner is a stuck desktop.
                try:
                    game_alive = steam_desktop_session.has_live_owners()
                except (OSError, ValueError, RuntimeError):
                    game_alive = True  # An uncertain process scan cannot authorize recovery.
                if not game_alive and time.monotonic() - failing_since > 20:
                    desktop_environment.notify_stuck(message)
                if message != last_error or time.monotonic() - last_logged > 30:
                    print(f"[RECUPERAÇÃO] Estado preservado; aguardando: {message}", flush=True)
                    last_error, last_logged = message, time.monotonic()
            time.sleep(2)


def ensure_recovery_guard(root: Path) -> None:
    """Generate host integration from this portable copy; never replace personal files."""
    marker = "X-RetroFE-Managed-Input-Recovery=true"
    steam_desktop_session.state_directory().mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "session-recovery-loop"]
    def quoted(args, systemd=False):
        values = []
        for arg in args:
            value = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            if systemd:
                value = value.replace("$", "$$")
            values.append('"' + value + '"')
        return " ".join(values)
    systemd = shutil.which("systemctl")
    usable = systemd and run([systemd, "--user", "is-active", "graphical-session.target"], timeout=5).returncode == 0
    destination = config_home() / ("systemd/user/retrofe-input-recovery.service" if usable else "autostart/retrofe-input-recovery.desktop")
    if destination.is_symlink() or (destination.exists() and marker not in destination.read_text()):
        raise RuntimeError(f"Integração de recuperação não gerenciada foi preservada: {destination}")
    if usable:
        environment = ["XDG_STATE_HOME=" + str(steam_desktop_session.state_directory().parents[1]),
                       "RETROFE_STEAM_CDP_PORT=" + os.environ.get("RETROFE_STEAM_CDP_PORT", "8080")]
        payload = ("# " + marker + "\n[Unit]\nDescription=RetroFE desktop input recovery\n"
                   "# Steam precisa de DISPLAY/XAUTHORITY, importados somente com a sessão gráfica.\n"
                   "After=graphical-session.target\nPartOf=graphical-session.target\n"
                   "[Service]\nType=simple\nEnvironment=" + quoted(environment, True) + "\nExecStart=" + quoted(command, True) +
                   "\nRestart=on-failure\nRestartSec=2\n[Install]\nWantedBy=graphical-session.target\n").encode()
    else:
        payload = ("[Desktop Entry]\nType=Application\nName=RetroFE Input Recovery\n" + marker +
                   "\nExec=" + quoted(command) + "\nNoDisplay=true\nTerminal=false\nX-GNOME-Autostart-enabled=true\n").encode()
    changed = not destination.exists() or destination.read_bytes() != payload
    if changed:
        atomic_write(destination, payload, 0o600)
    activated = False
    if usable:
        if changed:
            result = run([systemd, "--user", "daemon-reload"], timeout=10)
            if result.returncode: raise RuntimeError(result.stderr)
        active = run([systemd, "--user", "is-active", "retrofe-input-recovery.service"], timeout=5).returncode == 0
        # reenable remove o vínculo antigo com default.target quando a unidade muda.
        result = run([systemd, "--user", "reenable" if changed else "enable", "--now", "retrofe-input-recovery.service"], timeout=15)
        if result.returncode: raise RuntimeError(result.stderr)
        if changed and active:
            result = run([systemd, "--user", "restart", "retrofe-input-recovery.service"], timeout=15)
            if result.returncode: raise RuntimeError(result.stderr)
        if run([systemd, "--user", "is-active", "retrofe-input-recovery.service"], timeout=5).returncode:
            raise RuntimeError("Guardião independente não ficou ativo; Desktop não será suspenso.")
        activated = not active
    else:
        with (steam_desktop_session.state_directory().parent / "input-recovery.log").open("ab") as log:
            subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
    if changed or activated:
        record_environment_change(root, {"actions":["desktop-input-recovery-installed"],
                                        "path":str(destination), "sha256":hashlib.sha256(payload).hexdigest(),
                                        "backend":"systemd-user" if usable else "xdg-autostart"})
    print("[RECUPERAÇÃO] Guardião de sessão/login disponível (" + ("systemd usuário" if usable else "autostart") + ").")

def run_session(root: Path, command: list[str], system_id: str | None = None) -> int:
    """Use the existing Steam lease for ES-DE and direct emulator launches."""
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("Sessão de controle exige um comando após --")
    devices = Path("/proc/bus/input/devices").read_text(errors="replace")
    mode = runpy.run_path(str(Path(__file__).with_name("input-session.py")))["detect_session_mode"]()
    if not re.search(r"Vendor=28de Product=1205", devices, re.IGNORECASE) or mode == "big-picture":
        if system_id:
            if mode != "big-picture":
                sys.path.insert(0, str(root.parents[1] / "tools/roms"))
                import controller_session
                return controller_session.run(root.parents[1], system_id, command, os.environ.copy())
            print('[CONTROLE] Gaming Mode mantém Steam Input; roteamento dock/saída KWin exige sessão Desktop.', flush=True)
            prepare_emulator_controls(root, system_id)
        return subprocess.run(command, check=False).returncode
    helper = [sys.executable, str(Path(__file__).resolve()), "--root", str(root)]
    ensure_recovery_guard(root)
    saved = steam_desktop_session.snapshot()
    if not saved or not any(steam_desktop_session.owner_active(owner) for owner in saved["owners"].values()):
        # No installation, login automation or Steam restart on game launch.
        prepared = subprocess.run([*helper, "session-prepare", "--no-restart"], check=False)
        if prepared.returncode:
            return prepared.returncode
    pid = os.getpid()
    acquired = steam_desktop_session.acquire(root, pid)
    env = os.environ.copy()
    if acquired:
        env.update(SDL_JOYSTICK_HIDAPI_STEAMDECK="0", SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD="1")
        owner = (steam_desktop_session.snapshot() or {}).get("owners", {}).get(str(pid), {})
        if owner.get("token"):
            env[steam_desktop_session.SESSION_TOKEN_ENV] = owner["token"]
    try:
        if system_id:
            sys.path.insert(0, str(root.parents[1] / "tools/roms"))
            import controller_session
            def owned_children():
                return steam_desktop_session.live_children(pid)
            def remaining():
                return bool(owned_children())
            return controller_session.run(root.parents[1], system_id, command, env,
                attach=(lambda child_pid: steam_desktop_session.attach(pid, child_pid)) if acquired else None,
                remaining=remaining if acquired else None, owned_children=owned_children if acquired else None)
        child = subprocess.Popen(command, env=env)
        if acquired:
            steam_desktop_session.attach(pid, child.pid)
        result = child.wait()
        if acquired:
            # Flatpak's command process can finish before its remaining
            # sandbox descendants. Keep the lease until their verified exit.
            while True:
                if not steam_desktop_session.live_children(pid):
                    break
                time.sleep(.2)
        return result
    finally:
        if acquired:
            # A live child prevents restoration and remains watched independently.
            steam_desktop_session.release(pid)


def prepare_emulator_controls(root: Path, system_id: str, *, require_gamepad=False) -> None:
    tools = root.parents[1] / "tools/roms"
    sys.path.insert(0, str(tools))
    import emulator_controls
    emulator_controls.prepare_runtime(root.parents[1], system_id, require_gamepad=require_gamepad)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="diagnóstico somente leitura")
    commands.add_parser("ensure", help="instala/configura Steam Input uma vez e garante cliente ativo")
    commands.add_parser("open-desktop", help="abre no Steam a configuração Desktop/Gamepad do Steam Deck")
    prepare_parser = commands.add_parser("session-prepare", help="prepara API Steam local e template Gamepad sem alterar o perfil ativo")
    prepare_parser.add_argument("--no-restart", action="store_true", help="preserva o Steam em uso se a API estiver indisponível")
    session_run = commands.add_parser("session-run", help="mantém o Gamepad durante o frontend/emulador e restaura o Desktop")
    session_run.add_argument("--system-id")
    session_run.add_argument("child_command", nargs=argparse.REMAINDER)
    controller_prepare = commands.add_parser("controller-prepare", help=argparse.SUPPRESS)
    controller_prepare.add_argument("--system-id", required=True)
    controller_prepare.add_argument("--require-gamepad", action="store_true")
    commands.add_parser("session-status", help="consulta saídas reais do perfil Desktop na API Steam local")
    commands.add_parser("session-recover", help="restaura KDE e Steam de sessões órfãs")
    commands.add_parser("session-recovery-ensure", help="instala guardião independente por usuário")
    commands.add_parser("session-recovery-loop", help=argparse.SUPPRESS)
    for name in ("session-acquire", "session-release", "session-attach", "session-watch"):
        item = commands.add_parser(name, help=argparse.SUPPRESS)
        item.add_argument("--pid", type=int, required=True)
        if name == "session-attach":
            item.add_argument("--child-pid", type=int, required=True)
        if name == "session-watch":
            item.add_argument("--pid-start", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "controller-prepare":
            prepare_emulator_controls(root, args.system_id, require_gamepad=args.require_gamepad)
            return 0
        if args.command.startswith("session-"):
            if args.command == "session-run":
                return run_session(root, args.child_command, args.system_id)
            elif args.command == "session-status":
                print(json.dumps({"controllers": steam_desktop_session.inspect(),
                                  "recovery": steam_desktop_session.snapshot()}, ensure_ascii=False, indent=2))
            elif args.command == "session-recover":
                recover_session_once(root)
            elif args.command == "session-recovery-ensure":
                ensure_recovery_guard(root)
            elif args.command == "session-recovery-loop":
                recovery_loop(root)
            elif args.command == "session-release":
                steam_desktop_session.release(args.pid)
            elif args.command == "session-attach":
                steam_desktop_session.attach(args.pid, args.child_pid)
            elif args.command == "session-watch":
                steam_desktop_session.watch(args.pid, args.pid_start)
            else:
                devices = Path("/proc/bus/input/devices").read_text(errors="replace")
                deck_present = bool(re.search(r"Vendor=28de Product=1205", devices, re.IGNORECASE))
                if not deck_present:
                    print("[STEAM INPUT] Host sem Steam Deck; perfil Desktop do Deck não será alterado.")
                    return 0
                session = runpy.run_path(str(Path(__file__).with_name("input-session.py")))["detect_session_mode"]()
                if session == "big-picture":
                    print("[STEAM INPUT] Gaming/Big Picture já controla o gamepad; perfil Desktop preservado.")
                    return 0
                if args.command == "session-prepare":
                    install = find_steam()
                    if not install:
                        raise RuntimeError("Steam ausente; execute a preparação do launcher primeiro.")
                    account, _ = active_account_id64(install.root)
                    if not account:
                        raise RuntimeError("Faça login no Steam para preparar o perfil de sessão.")
                    template, state = install_template(root, install, account,
                        "retrofe_steamdeck_gamepad.vdf", Path("Config/steam-input/retrofe_steamdeck_gamepad.vdf.in"))
                    if state == "conflict-preserved":
                        raise RuntimeError(f"Template de sessão diferente foi preservado: {template}")
                    actions = ["steam-gamepad-session-template-installed"] if state == "installed" else []
                    if actions:
                        record_environment_change(root, {"actions": actions, "template": str(template)})
                    changes = steam_desktop_session.prepare(install, start_steam, process_running,
                                                           allow_restart=not args.no_restart)
                    if changes:
                        record_environment_change(root, {"actions": changes, "steam_root": str(install.root)})
                    print("[STEAM INPUT] API local e template Gamepad prontos.")
                elif steam_desktop_session.acquire(root, args.pid):
                    print("RETROFE_STEAM_GAMEPAD_SESSION=1")
                    owner = (steam_desktop_session.snapshot() or {}).get("owners", {}).get(str(args.pid), {})
                    if owner.get("token"):
                        print(f"{steam_desktop_session.SESSION_TOKEN_ENV}={owner['token']}")
            return 0
        if args.command == "status":
            print(json.dumps(status_data(root, find_steam()), ensure_ascii=False, indent=2))
            return 0
        install, current = ensure(root)
        if args.command == "open-desktop":
            if current.get("desktop_controller_profile") not in {"configured", "suspended-for-retrofe"}:
                raise RuntimeError("Steam está aberto, mas o layout Desktop ainda não pode ser ativado. Conclua o login no Steam e abra novamente o RetroFE.")
            open_desktop_controller_config(root, install)
            return 0
        if args.command == "ensure":
            ready = (
                current.get("active")
                and current.get("desktop_controller_profile") in {"configured", "suspended-for-retrofe"}
                and current.get("profile_template_matches")
                and current.get("desktop_menu_entry")
            )
            if not ready:
                print("[FALTA] Steam Input ainda não está pronto. Conclua o login no Steam ou corrija a instalação indicada acima antes de iniciar o RetroFE.", file=sys.stderr)
                return 3
            return 0
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
