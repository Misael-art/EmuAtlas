"""Reversible controller configuration shared by the two canonical frontends.

Only declared bindings are added. Original keyboard bindings, user data and
first-run decisions remain in their existing configuration files.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import configparser
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET

import romtool


HOST = Path("frontend/RetroFE")
PROFILE = HOST / "Config/profiles/host-linux.json"
PLAYSTATION = {
    "Up": "DPadUp", "Down": "DPadDown", "Left": "DPadLeft", "Right": "DPadRight",
    "Cross": "A", "Circle": "B", "Square": "X", "Triangle": "Y",
    "Start": "Start", "Select": "Back", "L1": "LeftShoulder", "R1": "RightShoulder",
    "L2": "+LeftTrigger", "R2": "+RightTrigger", "L3": "LeftStick", "R3": "RightStick",
    "LUp": "-LeftY", "LDown": "+LeftY", "LLeft": "-LeftX", "LRight": "+LeftX",
    "RUp": "-RightY", "RDown": "+RightY", "RLeft": "-RightX", "RRight": "+RightX",
}
RA_BUTTONS = {"b": 0, "a": 1, "y": 2, "x": 3, "select": 4, "start": 6,
              "l3": 7, "r3": 8, "l": 9, "r": 10,
              "up": 11, "down": 12, "left": 13, "right": 14}
RA_AXES = {"l2": "+4", "r2": "+5", "l_x_plus": "+0", "l_x_minus": "-0",
           "l_y_plus": "+1", "l_y_minus": "-1", "r_x_plus": "+2", "r_x_minus": "-2",
           "r_y_plus": "+3", "r_y_minus": "-3"}
DOLPHIN_BUTTONS = {
    "Buttons/A": "Button S", "Buttons/B": "Button E", "Buttons/X": "Button W",
    "Buttons/Y": "Button N", "Buttons/Z": "Shoulder R", "Buttons/Start": "Start",
    "Main Stick/Up": "Left Y+", "Main Stick/Down": "Left Y-",
    "Main Stick/Left": "Left X-", "Main Stick/Right": "Left X+",
    "C-Stick/Up": "Right Y+", "C-Stick/Down": "Right Y-",
    "C-Stick/Left": "Right X-", "C-Stick/Right": "Right X+",
    "Triggers/L": "Trigger L", "Triggers/R": "Trigger R",
    "Triggers/L-Analog": "Trigger L", "Triggers/R-Analog": "Trigger R",
    "D-Pad/Up": "Pad N", "D-Pad/Down": "Pad S", "D-Pad/Left": "Pad W", "D-Pad/Right": "Pad E",
}
WIIMOTE_BUTTONS = {
    "Buttons/A": "Button S", "Buttons/B": "Trigger R", "Buttons/1": "Button W",
    "Buttons/2": "Button N", "Buttons/-": "Back", "Buttons/+": "Start",
    "D-Pad/Up": "Pad N", "D-Pad/Down": "Pad S", "D-Pad/Left": "Pad W", "D-Pad/Right": "Pad E",
    "IR/Up": "Right Y+", "IR/Down": "Right Y-", "IR/Left": "Right X-", "IR/Right": "Right X+",
    "Nunchuk/Buttons/C": "Shoulder L", "Nunchuk/Buttons/Z": "Trigger L",
    "Nunchuk/Stick/Up": "Left Y+", "Nunchuk/Stick/Down": "Left Y-",
    "Nunchuk/Stick/Left": "Left X-", "Nunchuk/Stick/Right": "Left X+",
    "Shake/X": "Thumb L", "Shake/Y": "Thumb L", "Shake/Z": "Thumb L",
    "Classic/Buttons/A": "Button E", "Classic/Buttons/B": "Button S",
    "Classic/Buttons/X": "Button N", "Classic/Buttons/Y": "Button W",
    "Classic/Buttons/+": "Start", "Classic/Buttons/-": "Back",
    "Classic/Triggers/L": "Trigger L", "Classic/Triggers/R": "Trigger R",
    "Classic/Buttons/ZL": "Shoulder L", "Classic/Buttons/ZR": "Shoulder R",
    "Classic/Left Stick/Up": "Left Y+", "Classic/Left Stick/Down": "Left Y-",
    "Classic/Left Stick/Left": "Left X-", "Classic/Left Stick/Right": "Left X+",
    "Classic/Right Stick/Up": "Right Y+", "Classic/Right Stick/Down": "Right Y-",
    "Classic/Right Stick/Left": "Right X-", "Classic/Right Stick/Right": "Right X+",
    "Classic/D-Pad/Up": "Pad N", "Classic/D-Pad/Down": "Pad S",
    "Classic/D-Pad/Left": "Pad W", "Classic/D-Pad/Right": "Pad E",
}


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def routed_players(pads):
    expected = int(os.environ.get('EMULATION_PLAYER_CAPACITY', '4'))
    selected = [p for p in pads if p['name'].startswith('Emulation P')]
    by_name = {p['name']: p for p in selected}
    names = [f'Emulation P{n+1}' for n in range(expected)]
    if len(selected) != expected or len(by_name) != expected or any(name not in by_name for name in names):
        raise RuntimeError('Portas virtuais da sessão indisponíveis durante a preparação')
    return [by_name[name] for name in names]


def target(root, relative):
    # Host profiles express shared paths relative to frontend/RetroFE. Normalize
    # only this declared prefix; arbitrary traversal remains forbidden.
    shared_prefix = 'frontend/RetroFE/../../emulator/data'
    if str(relative) == shared_prefix or str(relative).startswith(shared_prefix + '/'):
        relative = Path('emulator/data' + str(relative)[len(shared_prefix):])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Caminho de configuração precisa ser relativo e sem traversal")
    path = root / relative
    if not path.is_relative_to(root):
        raise ValueError("Configuração fora do projeto")
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink():
            raise ValueError(f"Configuração por symlink preservada: {relative}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Configuração não é arquivo regular: {relative}")
    return path


def ini_update(data, settings, *, append=False, expression=False):
    """Keep unrelated bytes and support SimpleIni's repeated binding keys."""
    bom = data.startswith(b"\xef\xbb\xbf")
    text = data.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for section, values in settings.items():
        starts = [i for i, line in enumerate(lines) if line.strip() == f"[{section}]"]
        if len(starts) > 1:
            raise ValueError(f"Seção duplicada preservada: {section}")
        if not starts:
            lines.extend(["", f"[{section}]"])
            starts = [len(lines) - 1]
        start = starts[0] + 1
        end = next((i for i in range(start, len(lines)) if lines[i].strip().startswith("[")), len(lines))
        for key, value in values.items():
            pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=\s*(.*)$")
            matches = [(i, pattern.match(lines[i]).group(1).strip())
                       for i in range(start, end) if pattern.match(lines[i])]
            old = [v for _, v in matches]
            if str(value) in old:
                continue
            if append:
                # A different joystick binding is an explicit choice. Preserve it.
                if any("SDL-" in v for v in old):
                    continue
                lines.insert(end, f"{key} = {value}")
                end += 1
            elif expression and matches:
                if len(matches) != 1:
                    raise ValueError(f"Binding duplicado preservado: {section}/{key}")
                if str(value) not in old[0]:
                    lines[matches[0][0]] = f"{key} = {old[0]} | {value}" if old[0] else f"{key} = {value}"
            else:
                if len(matches) > 1:
                    raise ValueError(f"Chave duplicada preservada: {section}/{key}")
                if matches:
                    lines[matches[0][0]] = f"{key} = {value}"
                else:
                    lines.insert(end, f"{key} = {value}")
                    end += 1
    result = (newline.join(lines) + newline).encode("utf-8")
    return (b"\xef\xbb\xbf" if bom else b"") + result


def gamepads(library=None):
    """Read SDL's actual mapping. No grabs, writes or synthetic events."""
    library = library or ctypes.util.find_library("SDL2-2.0")
    if not library:
        return []
    sdl = ctypes.CDLL(library)
    sdl.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
    sdl.SDL_InitSubSystem.restype = ctypes.c_int
    sdl.SDL_GameControllerNameForIndex.argtypes = [ctypes.c_int]
    sdl.SDL_GameControllerNameForIndex.restype = ctypes.c_char_p
    sdl.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
    sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
    sdl.SDL_GameControllerMapping.argtypes = [ctypes.c_void_p]
    sdl.SDL_GameControllerMapping.restype = ctypes.c_void_p
    sdl.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
    joystick_path = getattr(sdl,'SDL_JoystickPathForIndex',None)
    if joystick_path:
        joystick_path.argtypes=[ctypes.c_int]
        joystick_path.restype=ctypes.c_char_p
    for symbol in ("SDL_JoystickGetDeviceVendor", "SDL_JoystickGetDeviceProduct"):
        function = getattr(sdl, symbol)
        function.argtypes = [ctypes.c_int]
        function.restype = ctypes.c_uint16
    sdl.SDL_free.argtypes = [ctypes.c_void_p]
    if sdl.SDL_InitSubSystem(0x2000) != 0:
        raise RuntimeError("SDL não conseguiu enumerar os controles")
    result = []
    try:
        for index in range(sdl.SDL_NumJoysticks()):
            if not sdl.SDL_IsGameController(index):
                continue
            pad = sdl.SDL_GameControllerOpen(index)
            if not pad:
                continue
            try:
                mapping = sdl.SDL_GameControllerMapping(pad)
                try:
                    text = ctypes.string_at(mapping).decode() if mapping else ""
                finally:
                    if mapping:
                        sdl.SDL_free(mapping)
                result.append({"index": index, "name": sdl.SDL_GameControllerNameForIndex(index).decode(),
                               "path": (joystick_path(index) or b'').decode() if joystick_path else '',
                               "guid": text.split(",", 1)[0],
                               "vendor": sdl.SDL_JoystickGetDeviceVendor(index),
                               "product": sdl.SDL_JoystickGetDeviceProduct(index),
                               "mapping": dict(x.split(":", 1) for x in text.split(",")[2:] if ":" in x)})
            finally:
                sdl.SDL_GameControllerClose(pad)
    finally:
        sdl.SDL_QuitSubSystem(0x2000)
    return result


def runtime_gamepads(root, system_id=None):
    """Probe the runtime's SDL2, not SDL2-compat's different host ordering."""
    if os.environ.get('EMULATION_ROUTED_CONTROLS') != '1' or not system_id:
        return gamepads()
    profile = json.loads((root / PROFILE).read_text())
    entry = next((v for v in profile['collections'].values() if v['rom_subdir']==system_id),{})
    app = entry.get('app_id')
    if entry.get('adapter') == 'retroarch':
        app = profile['retroarch'].get('app_id','org.libretro.RetroArch')
    if app:
        runtime = subprocess.run(['flatpak','info','--show-runtime',app],capture_output=True,text=True,timeout=5)
        if runtime.returncode == 0:
            location = subprocess.run(['flatpak','info','--show-location',runtime.stdout.strip()],capture_output=True,text=True,timeout=5)
            directory = Path(location.stdout.strip())
            if location.returncode == 0 and directory.is_absolute():
                libraries = sorted(directory.glob('files/lib/**/libSDL2-2.0.so.0'))
                if len(libraries)==1:
                    return gamepads(str(libraries[0]))
    return gamepads()


def dolphin_gamepad_name(fallback):
    library = ctypes.util.find_library("SDL3")
    if not library:
        raise RuntimeError("SDL3 indisponível para confirmar o nome do controle Dolphin")
    sdl = ctypes.CDLL(library)
    sdl.SDL_InitSubSystem.argtypes = [ctypes.c_uint32]
    sdl.SDL_InitSubSystem.restype = ctypes.c_bool
    sdl.SDL_GetGamepads.argtypes = [ctypes.POINTER(ctypes.c_int)]
    sdl.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
    sdl.SDL_GetGamepadNameForID.argtypes = [ctypes.c_uint32]
    sdl.SDL_GetGamepadNameForID.restype = ctypes.c_char_p
    sdl.SDL_free.argtypes = [ctypes.c_void_p]
    if not sdl.SDL_InitSubSystem(0x2000):
        raise RuntimeError("SDL3 não enumerou o controle Dolphin")
    ids = None
    try:
        count = ctypes.c_int()
        ids = sdl.SDL_GetGamepads(ctypes.byref(count))
        if count.value != 1:
            raise RuntimeError("Seleção Dolphin ambígua ou indisponível; preserve a configuração")
        name = sdl.SDL_GetGamepadNameForID(ids[0])
        return name.decode() if name else fallback
    finally:
        if ids:
            sdl.SDL_free(ids)
        sdl.SDL_QuitSubSystem(0x2000)


def melonds_button(binding):
    """melonDS 1.1 consumes raw SDL joystick codes, not GameController IDs."""
    if re.fullmatch(r"b\d+", binding):
        return int(binding[1:])
    match = re.fullmatch(r"h(\d+)\.(1|2|4|8)", binding)
    if match:
        return 0x100 | (int(match[1]) << 4) | int(match[2])
    raise ValueError(f"Binding SDL melonDS não suportado: {binding}")


def azahar_profile(data, pad):
    config = configparser.ConfigParser(interpolation=None, strict=True)
    config.optionxform = str
    config.read_string(data.decode("utf-8-sig"))
    values = dict(config["Controls"])
    size = int(values.get("profiles\\size", "0"))
    selected = int(values.get("profile", "0")) + 1
    name = "Projeto - Gamepad SDL"
    managed = [i for i in range(1, size + 1) if values.get(f"profiles\\{i}\\name", "").strip('"') == name]
    if len(managed) > 1:
        raise ValueError("Perfil de controle Azahar duplicado")
    if not managed and values.get(f"profiles\\{selected}\\name", "Default") != "Default":
        return data
    index = managed[0] if managed else size + 1
    prefix = f"profiles\\{index}\\"
    settings = {prefix + key[len(f"profiles\\{selected}\\"):]: value for key, value in values.items()
                if key.startswith(f"profiles\\{selected}\\")}
    settings.update({"profiles\\size": str(max(size, index)), "profile": str(index - 1),
                     "profile\\default": "false", prefix + "name": name, prefix + "name\\default": "false"})
    base = f"engine:sdl,api:controller,guid:{pad['guid']},port:0"
    buttons = {"a": 1, "b": 0, "x": 3, "y": 2, "l": 9, "r": 10,
               "start": 6, "select": 4, "up": 11, "down": 12, "left": 13, "right": 14}
    for key, button in buttons.items():
        settings[prefix + "button_" + key] = json.dumps(f"{base},button:{button}")
        settings[prefix + "button_" + key + "\\default"] = "false"
    for key, axis in {"zl": 4, "zr": 5}.items():
        settings[prefix + "button_" + key] = json.dumps(f"{base},axis:{axis},direction:+,threshold:0.5")
        settings[prefix + "button_" + key + "\\default"] = "false"
    for key, axes in {"circle_pad": (0, 1), "c_stick": (2, 3)}.items():
        settings[prefix + key] = json.dumps(f"{base},axis_x:{axes[0]},axis_y:{axes[1]}")
        settings[prefix + key + "\\default"] = "false"
    return ini_update(data, {"Controls": settings})


def cemu_profile(pad):
    node = ET.Element("emulated_controller")
    ET.SubElement(node, "type").text = "Wii U GamePad"
    controller = ET.SubElement(node, "controller")
    for key, value in {"api": "SDLController", "uuid": "0_" + pad["guid"], "display_name": pad["name"]}.items():
        ET.SubElement(controller, key).text = value
    mappings = ET.SubElement(controller, "mappings")
    # Cemu v2.6 VPADController::ButtonId -> ControllerBase::Buttons2.
    buttons = {1: 1, 2: 0, 3: 3, 4: 2, 5: 9, 6: 10, 7: 42, 8: 43,
               9: 6, 10: 4, 11: 11, 12: 12, 13: 13, 14: 14, 15: 7, 16: 8,
               17: 45, 18: 39, 19: 44, 20: 38, 21: 47, 22: 41, 23: 46, 24: 40}
    for mapping, button in buttons.items():
        entry = ET.SubElement(mappings, "entry")
        ET.SubElement(entry, "mapping").text = str(mapping)
        ET.SubElement(entry, "button").text = str(button)
    ET.indent(node, space="    ")
    return ET.tostring(node, encoding="utf-8", xml_declaration=True) + b"\n"


def rpcs3_selection(data):
    # Only read the two scalar fields that this tool owns, preserving every
    # other setting. Unknown or ambiguous YAML is never reserialized.
    text = data.decode("utf-8-sig")
    block = re.search(r"(?m)^Player 1 Input:\s*\n((?:[ \t]+[^\n]*\n|\n)*)", text)
    if not block:
        return None
    result = {}
    for key in ("Handler", "Device"):
        values = re.findall(r"(?m)^  " + key + r": ([^\n]*)$", block[1])
        if len(values) != 1:
            return None
        value = values[0].strip()
        result[key] = json.loads(value) if value.startswith('"') else value
    return result


@contextmanager
def configuration_lock():
    path = romtool.OPS_ROOT / "emulator-controls.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def prior_bytes(root, path):
    for stored in sorted(romtool.OPS_ROOT.glob("emulator-controls-*/manifest.json"), reverse=True):
        manifest = json.loads(stored.read_text())
        if manifest.get("status") != "completed_verified":
            continue
        for row in manifest.get("items", []):
            if row.get("path") != str(path.relative_to(root)):
                continue
            snapshot = target(root, Path(row["after_snapshot"]))
            data = snapshot.read_bytes()
            if digest(data) != row["sha256_after"]:
                raise ValueError("Snapshot de controles DS divergente")
            return data
    return None


def owned_melonds_bindings(root, path, current):
    data = prior_bytes(root, path)
    if data is None:
        return False
    previous = tomllib.loads(data.decode()).get("Instance0", {}).get("Joystick", {})
    return all(current.get(k) == v for k, v in previous.items() if not k.startswith("HK_"))


def esde_projection(root, profile):
    try:
        from .frontend_projection import esde_projection as project
    except ImportError:
        from frontend_projection import esde_projection as project
    return project(root, profile)


def proposals(root, *, pad=None, system_id=None, frontends=True, pads=None, routed=False):
    """Return bytes and reasons. A missing runtime is reported, never guessed."""
    profile = json.loads((root / PROFILE).read_text())
    pads = pads or ([pad] if pad else [])
    results = []

    def add(relative, data, reason):
        path = target(root, relative)
        relative = path.relative_to(root)
        before = path.read_bytes() if path.exists() else None
        if before != data:
            results.append({"path": str(relative), "before": before, "after": data, "reason": reason})

    def ini(relative, settings, reason, **options):
        p = target(root, relative)
        if p.is_file():
            add(relative, ini_update(p.read_bytes(), settings, **options), reason)

    if system_id in (None, "psx", "ps2"):
        for sid, relative in [("psx", "../../emulator/data/DuckStation/config/duckstation/settings.ini"),
                              ("ps2", "../../emulator/data/PCSX2/PCSX2/inis/PCSX2.ini")]:
            if system_id and system_id != sid:
                continue
            bindings = dict(PLAYSTATION)
            if sid == "ps2":
                bindings.update(Cross="FaceSouth", Circle="FaceEast", Square="FaceWest", Triangle="FaceNorth")
            ini(HOST / relative, {"Pad1": {key: f"SDL-0/{value}" for key, value in bindings.items()}},
                "Adicionar gamepad SDL ao jogador 1 preservando as alternativas de teclado", append=True)
            if routed:
                # These consoles have two native ports. Multitaps require a
                # separate per-game accessory decision; never force one.
                path = target(root, HOST / relative)
                original = path.read_bytes()
                data = ini_update(original, {'Pad1': {key:f'SDL-0/{value}' for key,value in bindings.items()},
                    'Pad2': {key:f'SDL-1/{value}' for key,value in bindings.items()}},append=True)
                section = re.search(r'(?ms)^\[Pad2\]\s*\n(.*?)(?=^\[|\Z)',original.decode('utf-8-sig'))
                chosen = re.findall(r'(?m)^Type\s*=\s*(.*?)\s*$',section[1]) if section else []
                if not chosen or chosen == ['None']:
                    data = ini_update(data,{'Pad2':{'Type':'DualShock2' if sid=='ps2' else 'AnalogController'}})
                results[:] = [r for r in results if r['path'] != str(HOST / relative)]
                add(HOST / relative,data,"Preparar duas portas nativas, preservando teclado e sem ativar multitap")
    uses_ra = system_id is None or any(x.get("rom_subdir") == system_id and x["adapter"] == "retroarch"
                                       for x in profile["collections"].values())
    if uses_ra:
        values = {"input_joypad_driver": '"sdl2"', "input_autodetect_enable": '"true"',
                  "input_player1_joypad_index": f'"{pad["index"] if pad else 0}"',
                  "input_libretro_device_p1": '"1"', "input_menu_toggle_gamepad_combo": '"3"',
                  "gamemode_enable": '"false"'}
        values.update({f"input_player1_{key}_btn": f'"{value}"' for key, value in RA_BUTTONS.items()})
        values.update({f"input_player1_{key}_axis": f'"{value}"' for key, value in RA_AXES.items()})
        if routed:
            values["input_menu_toggle_gamepad_combo"] = '"0"'
            values['input_max_users'] = json.dumps(str(len(pads)))
            for n, player in enumerate(pads, 1):
                values[f"input_player{n}_joypad_index"] = json.dumps(str(player["index"]))
                values[f"input_player{n}_reserved_device"] = json.dumps(player['name'])
                values[f"input_player{n}_device_reservation_type"] = '"2"'
                values.update({f"input_player{n}_{key}_btn": json.dumps(str(value)) for key,value in RA_BUTTONS.items()})
                values.update({f"input_player{n}_{key}_axis": json.dumps(value) for key,value in RA_AXES.items()})
        userdata = HOST / profile["retroarch"]["userdata_root"]
        values["joypad_autoconfig_dir"] = json.dumps(str(root / userdata / "autoconfig"))
        path = Path(profile["retroarch"]["config_path"])
        before = target(root, HOST / path).read_bytes()
        def cfg_values(data):
            return dict(re.findall(r"(?m)^\s*(\w+)\s*=\s*(.*?)\s*$", data.decode()))
        current = cfg_values(before)
        owned = cfg_values(prior_bytes(root, root / HOST / path) or b"")
        values = {key: value for key, value in values.items()
                  if key not in current or current[key] == owned.get(key) or current[key] == value}
        # This cfg has no sections. Use the same line-preserving writer with a temporary section.
        data = ini_update(b"[RetroArch]\n" + before, {"RetroArch": values}).split(b"\n", 1)[1]
        add(HOST / path, data, "Usar SDL GameController normalizado e configurar o RetroPad do jogador 1")
        if pad and (routed or pad["name"] in {"Steam Virtual Gamepad", "Steam Deck Controller"}):
            name = "emulation-player1.cfg" if routed else ("steam-virtual-gamepad.cfg" if pad["name"] == "Steam Virtual Gamepad" else "steam-deck.cfg")
            relative = userdata / "autoconfig/sdl2" / name
            settings = {"input_driver": "sdl2", "input_device": pad["name"],
                        "input_vendor_id": str(pad["vendor"]), "input_product_id": str(pad["product"])}
            settings.update({f"input_{key}_btn": str(value) for key, value in RA_BUTTONS.items()})
            settings.update({f"input_{key}_axis": value for key, value in RA_AXES.items()})
            data = "".join(f"{key} = {json.dumps(value)}\n" for key, value in settings.items()).encode()
            p = target(root, relative)
            if not p.exists() or p.read_bytes() in {data, prior_bytes(root, p)}:
                add(relative, data, "Autoconfig SDL2 pelo nome e VID/PID observados para reconhecer o controle no RetroArch")
            if routed:
                for n,player in enumerate(pads[1:],2):
                    settings.update(input_device=player['name'], input_vendor_id=str(player['vendor']),input_product_id=str(player['product']))
                    data = ''.join(f'{key} = {json.dumps(value)}\n' for key,value in settings.items()).encode()
                    relative = userdata / f'autoconfig/sdl2/emulation-player{n}.cfg'
                    p = target(root,relative)
                    if not p.exists() or p.read_bytes() in {data,prior_bytes(root,p)}:
                        add(relative,data,'Autoconfig para porta nomeada e reservada do jogador '+str(n))
    if system_id in (None, "gc", "wii", "wiiware") and pad:
        relative = HOST / "../../emulator/data/Dolphin/shared/Config/GCPadNew.ini"
        p = target(root, relative)
        legacy = Path.home() / ".var/app/org.DolphinEmu.dolphin-emu/config/dolphin-emu"
        original = p.read_bytes() if p.exists() else ((legacy / "GCPadNew.ini").read_bytes()
                                                      if (legacy / "GCPadNew.ini").is_file() else b"[GCPad1]\n")
        name = pad["name"] if routed else dolphin_gamepad_name(pad["name"])
        if any(char in name for char in "`:\n\r"):
            raise ValueError("Nome de dispositivo exige revisão de escape no Dolphin")
        device = f"SDL/0/{name}"
        bindings = {key: f"`{device}:{value}`" for key, value in DOLPHIN_BUTTONS.items()}
        add(relative, ini_update(original, {"GCPad1": bindings}, expression=True),
            "Adicionar entradas SDL GameCube pelo nome enumerado, preservando teclado e expressões")
        if routed:
            values = {f"GCPad{n}": {key: f"`SDL/0/{player['name']}:{value}`" for key,value in DOLPHIN_BUTTONS.items()}
                      for n,player in enumerate(pads[:4],1)}
            # One proposal per path; retain original preimage for the transaction.
            results[:] = [r for r in results if r['path'] != str(relative)]
            add(relative, ini_update(original, values, expression=True), "Preparar quatro portas GameCube estáveis da sessão")
        # Preserve existing Dolphin preferences when moving its configuration owner.
        wiimote_path = relative.with_name("WiimoteNew.ini")
        original_wii = target(root, wiimote_path)
        wii = (original_wii.read_bytes() if original_wii.exists() else
               ((legacy / "WiimoteNew.ini").read_bytes() if (legacy / "WiimoteNew.ini").is_file() else b"[Wiimote1]\n"))
        values = {key: f"`{device}:{value}`" for key, value in WIIMOTE_BUTTONS.items()}
        add(wiimote_path, ini_update(wii, {"Wiimote1": values}, expression=True),
            "Mapear Wiimote/Nunchuk e Classic preservando a extensão escolhida, mouse e teclado; movimento exige teste por jogo")
        if routed:
            values = {f"Wiimote{n}": {key: f"`SDL/0/{player['name']}:{value}`" for key,value in WIIMOTE_BUTTONS.items()}
                      for n,player in enumerate(pads[:4],1)}
            config = configparser.ConfigParser(interpolation=None,strict=True)
            config.optionxform = str
            config.read_string(wii.decode('utf-8-sig'))
            sources = {section:{'Source':'1'} for section in values
                       if config.get(section,'Source',fallback='0') in {'0','1'}}
            wii = ini_update(wii,sources)
            results[:] = [r for r in results if r['path'] != str(wiimote_path)]
            add(wiimote_path, ini_update(wii, values, expression=True), "Preparar bindings das quatro portas Wii; preservar fonte/extensão por jogo")
        for name in ("Dolphin.ini", "GFX.ini", "Hotkeys.ini"):
            destination = relative.with_name(name)
            source = legacy / name
            if not target(root, destination).exists() and source.is_file() and not source.is_symlink():
                add(destination, source.read_bytes(), "Preservar preferências Dolphin na configuração compartilhada")
        if routed:
            relative = relative.with_name('Dolphin.ini')
            original = target(root,relative).read_bytes() if target(root,relative).exists() else b''
            config = configparser.ConfigParser(interpolation=None,strict=True)
            config.optionxform = str
            config.read_string(original.decode('utf-8-sig'))
            values = {f'SIDevice{n}':'6' for n in range(4) if config.get('Core',f'SIDevice{n}',fallback='0') in {'0','6'}}
            results[:] = [r for r in results if r['path'] != str(relative)]
            add(relative,ini_update(original,{'Core':values}),'Ativar as quatro portas GameCube padrão; preservar acessórios específicos')
    if system_id in (None, "nds") and pad:
        relative = HOST / "../../emulator/data/MelonDS/config/melonDS/melonDS.toml"
        p = target(root, relative)
        if p.exists():
            before = p.read_bytes()
            config = tomllib.loads(before.decode())
            previous = config.get("Instance0", {}).get("Joystick", {})
            mapping = {"A": "b", "B": "a", "X": "y", "Y": "x", "L": "leftshoulder",
                       "R": "rightshoulder", "Start": "start", "Select": "back",
                       "Up": "dpup", "Down": "dpdown", "Left": "dpleft", "Right": "dpright"}
            # Existing nonempty user mappings are preserved. Runtime preparation
            # updates only a configuration previously produced by this tool.
            owned = owned_melonds_bindings(root, p, previous)
            if all(previous.get(key, -1) == -1 for key in mapping) or owned:
                values = {key: melonds_button(pad["mapping"][value]) for key, value in mapping.items()}
                data = ini_update(before, {"Instance0": {"JoystickID": pad["index"]}, "Instance0.Joystick": values})
                tomllib.loads(data.decode())
                add(relative, data, "Associar botões DS à representação raw real do controle SDL, mantendo teclado e toque")
    if system_id in (None, "n3ds") and pad:
        relative = HOST / "../../emulator/data/Azahar/config/azahar-emu/qt-config.ini"
        p = target(root, relative)
        if p.exists():
            add(relative, azahar_profile(p.read_bytes(), pad),
                "Selecionar perfil SDL normalizado para 3DS, preservando o perfil de teclado e toque")
    if system_id in (None, "wiiu") and pad:
        relative = HOST / "../../emulator/data/Cemu/config/Cemu/controllerProfiles/controller0.xml"
        p = target(root, relative)
        before = p.read_bytes() if p.exists() else None
        if before is None:
            add(relative, cemu_profile(pad), "Criar jogador 1 Wii U GamePad na API SDL com botões, sticks e gatilhos")
        else:
            prior = prior_bytes(root, p)
            if prior is not None:
                current_xml, previous_xml = ET.fromstring(before), ET.fromstring(prior)
                current_uuid = current_xml.find("controller/uuid")
                if (current_uuid is not None and current_uuid.text == previous_xml.findtext("controller/uuid")
                        and current_xml.findtext("controller/api") == "SDLController"):
                    current_uuid.text = "0_" + pad["guid"]
                    ET.indent(current_xml, space="    ")
                    add(relative, ET.tostring(current_xml, encoding="utf-8", xml_declaration=True) + b"\n",
                        "Atualizar apenas a identidade do controle Cemu gerenciado após a troca Desktop/Gamepad")
    if system_id in (None, "ps3") and pad:
        relative = HOST / "../../emulator/data/RPCS3/config/rpcs3/input_configs/global/Default.yml"
        p = target(root, relative)
        before = p.read_bytes() if p.exists() else None
        device = (pad["name"] if routed else dolphin_gamepad_name(pad["name"])) + " 1"
        if before is None:
            data = f"Player 1 Input:\n  Handler: SDL\n  Device: {json.dumps(device)}\n"
            add(relative, data.encode(), "Selecionar handler SDL no RPCS3; usar o automapeamento do runtime para o jogador 1")
        else:
            prior = prior_bytes(root, p)
            selection = rpcs3_selection(before)
            if prior is not None and selection == rpcs3_selection(prior) and selection and selection["Handler"] == "SDL":
                text = before.decode("utf-8-sig")
                block = re.search(r"(?m)^Player 1 Input:\s*\n((?:[ \t]+[^\n]*\n|\n)*)", text)
                changed = re.sub(r"(?m)^  Device: [^\n]*$", lambda _: "  Device: " + json.dumps(device), block[1])
                add(relative, (text[:block.start(1)] + changed + text[block.end(1):]).encode(),
                    "Atualizar identidade do gamepad RPCS3 preservando mapeamentos, hotkeys e outros jogadores")
        if routed:
            data = before or b""
            settings = data.decode('utf-8-sig')
            for n, player in enumerate(pads[:7], 1):
                heading = f"Player {n} Input:"
                block = re.search(r"(?m)^"+re.escape(heading)+r"\s*\n((?:[ \t]+[^\n]*\n|\n)*)", settings)
                if block:
                    content = block[1]
                    for key,value in {'Handler':'SDL','Device':json.dumps(player['name']+' 1')}.items():
                        pattern = r"(?m)^  "+key+r": [^\n]*$"
                        content = re.sub(pattern, lambda _: '  '+key+': '+value, content) if re.search(pattern, content) else content+'  '+key+': '+value+'\n'
                    settings = settings[:block.start(1)]+content+settings[block.end(1):]
                else:
                    settings += '\n'+heading+'\n  Handler: SDL\n  Device: '+json.dumps(player['name']+' 1')+'\n'
            results[:] = [r for r in results if r['path'] != str(relative)]
            add(relative, settings.encode(), "Associar portas SDL RPCS3 estáveis, preservando os demais campos")
    if system_id in (None, 'xbox') and pad:
        relative = HOST / '../../emulator/data/xemu/xemu.toml'
        p = target(root, relative)
        if p.exists():
            data = p.read_bytes()
            ports = {f'port{n}':json.dumps(player['guid']) for n,player in enumerate(pads[:4],1)}
            ports.update({f'port{n}_driver':json.dumps('usb-xbox-gamepad') for n in range(1,len(pads[:4])+1)})
            data = ini_update(data, {'input.bindings':ports})
            tomllib.loads(data.decode())
            add(relative, data, "Substituir GUID antigo do Deck pelas portas SDL atuais usando o schema nativo xemu")
    if system_id in (None, 'wiiu') and routed:
        for n,player in enumerate(pads[1:4],1):
            relative = HOST / f'../../emulator/data/Cemu/config/Cemu/controllerProfiles/controller{n}.xml'
            p = target(root, relative)
            if not p.exists() or prior_bytes(root,p) == p.read_bytes():
                node = ET.fromstring(cemu_profile(player))
                node.find('type').text = 'Wii U Pro Controller'
                add(relative, ET.tostring(node,encoding='utf-8',xml_declaration=True)+b'\n',
                    "Preparar controlador adicional Wii U Pro na porta estável da sessão")
    if system_id in (None,'switch2') and routed:
        for relative in [HOST/base/filename for base in ('../../emulator/data/Eden-NX2/config/eden','../../emulator/data/Eglantine/config/eglantine')
                         for filename in ('qt-config.ini','sdl2-config.ini')]:
            p = target(root,relative)
            original = p.read_bytes() if p.exists() else b'[Controls]\n'
            config = configparser.ConfigParser(interpolation=None,strict=True)
            config.optionxform=str
            config.read_string(original.decode('utf-8-sig'))
            values={}
            for n,player in enumerate(pads[:8]):
                prefix=f'player_{n}_'
                # Eden/Eglantine GetGUID explicitly clears SDL's controller
                # name CRC (bytes 2..3). A full SDL GUID silently never matches.
                native_guid=player['guid'][:4]+'0000'+player['guid'][8:]
                common='engine:sdl,port:0,guid:'+native_guid
                for key,value in config.items('Controls'):
                    if (key.startswith(prefix+'button_') and 'engine:sdl' in value
                            and not any(guid in value for guid in (player['guid'],native_guid))):
                        raise RuntimeError('Perfil Eden personalizado preservado; revisar jogador '+str(n+1))
                bindings={'a':'b','b':'a','x':'y','y':'x','lstick':'leftstick','rstick':'rightstick',
                    'l':'leftshoulder','r':'rightshoulder','zl':'lefttrigger','zr':'righttrigger',
                    'plus':'start','minus':'back','dup':'dpup','ddown':'dpdown','dleft':'dpleft','dright':'dpright'}
                for key,button in bindings.items():
                    raw=player['mapping'][button]
                    if raw.startswith('b'):parameter=',button:'+raw[1:]
                    elif raw.startswith('a'):parameter=',axis:'+raw[1:]+',threshold:0.5,invert:+'
                    else:
                        hat,mask=raw[1:].split('.')
                        parameter=',hat:'+hat+',direction:'+{'1':'up','2':'right','4':'down','8':'left'}[mask]
                    values[prefix+'button_'+key]=json.dumps(common+parameter)
                    values[prefix+'button_'+key+'\\default']='false'
                for key,x,y in [('lstick',0,1),('rstick',3,4)]:
                    values[prefix+key]=json.dumps(common+f',axis_x:{x},axis_y:{y},invert_x:+,invert_y:+,offset_x:0,offset_y:0')
                    values[prefix+key+'\\default']='false'
                for key,value in [('connected','true'),('type','0')]:
                    values[prefix+key]=value
                    values[prefix+key+'\\default']='false'
            sections={'Controls':values}
            if 'Eglantine' in str(relative):
                paths={}
                for key,folder in {'nand_directory':'nand','sdmc_directory':'sdmc','load_directory':'load','dump_directory':'dump','tas_directory':'tas'}.items():
                    if config.get('Paths',key,fallback='').strip('"')=='':
                        paths[key]=json.dumps(str(root/HOST/'../../emulator/data/Eglantine/data/eglantine'/folder))
                if paths:sections['Paths']=paths
            add(relative,ini_update(original,sections),
                'Associar quatro Pro Controllers SDL ao schema confirmado no código Eden; teclado roteado da sessão')
    if frontends:
        frontend_entries = dict(profile['collections'])
        for entry in profile['collections'].values():
            for alias in entry.get('subcollections', []):
                frontend_entries.setdefault(alias, {**entry, **profile.get('subcollection_overrides', {}).get(alias, {})})
        for name,entry in frontend_entries.items():
            if entry.get('rom_subdir')!='xbox360' or entry.get('adapter')!='command':continue
            relative=HOST/'collections'/name/'settings.conf'
            p=target(root,relative)
            if not p.exists():continue
            before=p.read_bytes();text=before.decode('utf-8-sig')
            launchers=re.findall(r'(?m)^launcher\s*=\s*(.*?)\s*$',text)
            if launchers and launchers!=['Host Emulator Unavailable'] and launchers!=['Host Command']:
                raise ValueError('Launcher Xbox 360 personalizado preservado')
            data=ini_update(b'[Collection]\n'+before,{'Collection':{'launcher':'Host Command','list.extensions':entry['extensions'],'list.romHierarchy':'true'}}).split(b'\n',1)[1]
            add(relative,data,'Ativar Xenia no RetroFE e descoberta de dumps em subpastas, sem mudar mídia/ROMs')
        projection, _ = esde_projection(root, profile)
        for relative, data in projection:
            add(relative, data, "Ambos os frontends usam o mesmo launcher e os mesmos dados de emulador")
    return results


def assert_closed():
    active = romtool.frontend_processes_running()
    if any(active.values()):
        raise RuntimeError("Encerre os frontends pelo menu antes de aplicar configurações")
    names = {"retroarch", "pcsx2-qt", "duckstation-qt", "duckstation", "dolphin-emu", "melonDS", "rpcs3", "azahar",
             "xemu", "Cemu", "Cemu-bin", "shadPS4", "shadps4", "kyty_emulator", "game-run", "PPSSPPSDL", "flycast", "eden", "eden-cli", "eglantine", "SharpEmu", "xenia_canary"}
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                if (p / "comm").read_text().strip() in names or (p/'exe').resolve().name in names:
                    raise RuntimeError("Há um emulador ativo; configuração preservada")
            except OSError:
                pass


def coverage(root):
    profile = json.loads((root / PROFILE).read_text())
    managed = {"psx": "duckstation_sdl", "ps2": "pcsx2_sdl", "ps3": "rpcs3_sdl_automap",
               "gc": "dolphin_sdl3", "wii": "dolphin_sdl3_motion_requires_game_review",
               "wiiware": "dolphin_sdl3_motion_requires_game_review", "wiiu": "cemu_sdl",
               "nds": "melonds_raw_sdl", "n3ds": "azahar_sdl_profile",
               "xbox": "xemu_native_sdl_guid_four_ports",
               "xbox360": "xenia_linux_sdl_four_ports_requires_native_acceptance",
               "switch2": "eden_native_sdl_raw_four_ports_requires_native_acceptance",
               "psp": "ppsspp_existing_sdl_port_one_requires_native_acceptance",
               "dreamcast": "flycast_sdl_defaults_four_ports_requires_native_acceptance",
               "atomiswave": "flycast_sdl_defaults_requires_native_acceptance",
               "naomi": "flycast_sdl_defaults_requires_native_acceptance",
               "naomi2": "flycast_sdl_defaults_requires_native_acceptance",
               "naomigd": "flycast_sdl_defaults_requires_native_acceptance",
               "ps4": "shadps4_sdl_defaults_requires_native_acceptance",
               "ps5": "kytyps5_input_requires_native_acceptance_ps5pcem_linux_keyboard_only"}
    rows = []
    for collection, entry in profile["collections"].items():
        sid, adapter = entry["rom_subdir"], entry["adapter"]
        strategy = managed.get(sid)
        if adapter == "unavailable":
            strategy = "no_runtime_profile"
        elif adapter == "retroarch":
            strategy = "retroarch_sdl2_retro_pad"
        elif adapter in {"mame", "mame_named"}:
            strategy = "mame_joystick_defaults_not_game_tested"
        rows.append({"system_id": sid, "collection": collection, "adapter": adapter,
                     "controller_strategy": strategy or "existing_runtime_defaults_not_game_tested",
                     "session_exit": "Back/Select + Start 2s; Ctrl+Shift+F10 2s (owned focused window)",
                     "runtime_profile_configured": adapter != 'unavailable',
                     "session_backend": "kde_desktop_only",
                     "physical_gameplay_verified": False})
    return rows


def retrofe_controls(data, pads, *, capacity=8):
    """Project logical SDL controls while preserving keyboard and custom actions."""
    actions = {'up': [('dpup', 0), ('lefty', -1)], 'down': [('dpdown', 0), ('lefty', 1)],
               'left': [('dpleft', 0), ('leftx', -1)], 'right': [('dpright', 0), ('leftx', 1)],
               'select': [('a', 0)], 'back': [('b', 0)], 'collectionDown': [('y', 0)],
               'togglePlaylist': [('x', 0)], 'favPlaylist': [('back', 0)],
               'pageUp': [('leftstick', 0)], 'pageDown': [('rightstick', 0)],
               'letterDown': [('leftshoulder', 0)], 'letterUp': [('rightshoulder', 0)]}
    def raw_token(index, raw, direction):
        prefix = f'joy{index}'
        if re.fullmatch(r'b\d+', raw):
            return prefix + 'Button' + raw[1:]
        match = re.fullmatch(r'h(\d+)\.([1248])', raw)
        if match:
            return prefix + 'Hat' + match[1] + {'1':'Up','2':'Right','4':'Down','8':'Left'}[match[2]]
        match = re.fullmatch(r'([+-]?)a(\d+)(~?)', raw)
        if match:
            sign = direction or (-1 if match[1] == '-' else 1)
            if match[3]: sign = -sign
            return prefix + 'Axis' + match[2] + ('-' if sign < 0 else '+')
        return None
    projected = {key: [] for key in actions}
    for pad in pads:
        mapping = pad.get('mapping', {})
        if not mapping:
            continue
        for action, bindings in actions.items():
            for key, direction in bindings:
                token = raw_token(pad['index'], mapping.get(key, ''), direction)
                if token and token not in projected[action]: projected[action].append(token)
    lines = []
    seen = set()
    text = data.decode('utf-8-sig').replace(
        '# Start (joyButton7) e F10 são reservados ao navigation-panel.py: Options global.',
        '# Start lógico SDL e F10 são reservados ao navigation-panel.py: Options global.')
    for line in text.splitlines(keepends=True):
        match = re.match(r'^(\s*)(\w+)(\s*=\s*)([^#\r\n]*)(.*)$', line.rstrip('\r\n'))
        if not match or match[2] not in actions:
            lines.append(line)
            continue
        action = match[2]
        if action in seen:
            raise ValueError('Ação duplicada em controls.conf: ' + action)
        seen.add(action)
        tokens = [v.strip() for v in match[4].split(',') if v.strip()]
        preserved = [v for v in tokens if not v.startswith('joy')]
        # Unknown controllers retain their existing raw bindings.
        # New mobile sources follow devices already enumerated by SDL.
        existing_slots = max((p['index'] + 1 for p in pads), default=0)
        unmapped = set(range(existing_slots + capacity)) - {p['index'] for p in pads if p.get('mapping')}
        for token in tokens:
            index = re.match(r'joy(\d+)', token)
            if index and int(index[1]) in unmapped and not token.endswith('Button7'):
                preserved.append(token)
            elif token.startswith('joy') and not index:
                if not token.endswith('Button7'):
                    preserved.extend(f'joy{n}' + token[3:] for n in sorted(unmapped))
        values = list(dict.fromkeys([*preserved, *projected[action]]))
        newline = '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''
        lines.append(match[1] + action + match[3] + ','.join(values) + match[5] + newline)
    for action in projected:
        if action not in seen and projected[action]:
            if lines and not lines[-1].endswith('\n'): lines[-1] += '\n'
            lines.append(action + ' = ' + ','.join(projected[action]) + '\n')
    reserved = set()
    for pad in pads:
        token = raw_token(pad['index'], pad.get('mapping', {}).get('start', ''), 0)
        if token:
            reserved.update((token, re.sub(r'^joy\d+', 'joy', token)))
    for line in lines:
        key, separator, values = line.split('#', 1)[0].partition('=')
        if separator and reserved.intersection(v.strip() for v in values.split(',')):
            raise ValueError('Start SDL conflita com a ação preservada ' + key.strip())
    return (b'\xef\xbb\xbf' if data.startswith(b'\xef\xbb\xbf') else b'') + ''.join(lines).encode('utf-8')


def prepare_retrofe(root):
    """Prepare only the closed frontend's native controls through its plan log."""
    from controller_session import player_capacity
    pads = gamepads()
    path = target(root, HOST / 'controls.conf')
    if retrofe_controls(path.read_bytes(), pads, capacity=player_capacity(root)) == path.read_bytes():
        return {'status':'configuration_verified', 'items':0}
    manifest = plan(root, retrofe_only=True)
    return execute(root, manifest['operation_id'], 'apply', runtime=True, frontend_startup=True)


def plan(root, *, system_id=None, frontends=True, retrofe_only=False):
    pads = runtime_gamepads(root,system_id)
    routed = os.environ.get('EMULATION_ROUTED_CONTROLS') == '1'
    if routed:
        pads = routed_players(pads)
    pad = pads[0] if pads and (routed or len(pads) == 1) else None
    if retrofe_only:
        from controller_session import player_capacity
        path = target(root, HOST / 'controls.conf')
        before = path.read_bytes()
        after = retrofe_controls(before, pads, capacity=player_capacity(root))
        items = [] if before == after else [{'path':str(HOST / 'controls.conf'), 'before':before,
            'after':after, 'reason':'Start reservado e ações lógicas pelo mapping SDL observado'}]
    else:
        items = proposals(root, pad=pad, pads=pads, routed=routed, system_id=system_id, frontends=frontends)
    operation_id = f"emulator-controls-{romtool.dt.datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    directory = romtool.operation_dir(operation_id)
    directory.mkdir(mode=0o700)
    manifest = {"schema_version": "1.0", "kind": "emulator_controls", "operation_id": operation_id,
                "created_at": romtool.now(), "status": "prepared", "physical_gameplay_verified": False,
                "system_id": system_id, "frontends": frontends,
                "retrofe_only": retrofe_only,
                "coverage": coverage(root) if frontends and not retrofe_only else [],
                "controllers": [{"index": p["index"], "name": p["name"]} for p in pads], "items": []}
    if retrofe_only:
        manifest['frontend_sdl_mappings'] = [
            {'index': p['index'], 'name': p['name'], 'mapping': p['mapping']} for p in pads]
    for index, item in enumerate(items):
        row = {k: v for k, v in item.items() if k not in {"before", "after"}}
        row.update(sha256_before=digest(item["before"]), sha256_after=digest(item["after"]),
                   existed_before=item["before"] is not None, mode_before=stat.S_IMODE((root / item["path"]).stat().st_mode) if item["before"] is not None else 0o600)
        for field, folder in [("before", "rollback/files"), ("after", "planned/files")]:
            if item[field] is not None:
                path = directory / folder / str(index)
                path.parent.mkdir(parents=True, exist_ok=True)
                romtool.atomic_write_bytes(path, item[field], mode=0o600)
                row[field + "_snapshot"] = str(path.relative_to(root))
        manifest["items"].append(row)
    romtool.write_manifest(manifest)
    romtool.append_event(operation_id, "controller_plan_prepared", status="prepared", details={"items": len(items)})
    if not retrofe_only: romtool.rebuild_history_index()
    return manifest


def execute(root, operation_id, action, *, runtime=False, frontend_startup=False):
    manifest_path = romtool.operation_dir(operation_id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("kind") != "emulator_controls":
        raise ValueError("A operação não é um plano de controles")
    if action == "verify":
        mismatches = []
        for row in manifest["items"]:
            path = target(root, Path(row["path"]))
            if not path.exists() or digest(path.read_bytes()) != row["sha256_after"]:
                mismatches.append(row["path"])
        return {"operation_id": operation_id, "status": "diverged" if mismatches else "configuration_verified",
                "diverged": mismatches, "physical_gameplay_verified": False}
    with (nullcontext() if runtime else romtool.media_session.write_lock(vars(romtool))), configuration_lock():
        if runtime:
            if frontend_startup:
                if not manifest.get('retrofe_only') or any(row['path'] != str(HOST / 'controls.conf') for row in manifest['items']):
                    raise ValueError('Preparação RetroFE restrita ao arquivo de controles')
                assert_closed()
            elif manifest.get("frontends") or not manifest.get("system_id"):
                raise ValueError("Configuração de frontend exige aplicação fora da sessão")
        else:
            assert_closed()
        rollback = action == "rollback"
        rows = list(reversed(manifest["items"])) if rollback else manifest["items"]
        for row in rows:
            path = target(root, Path(row["path"]))
            current = path.read_bytes() if path.exists() else None
            expected = row["sha256_after"] if rollback else row["sha256_before"]
            desired = row["sha256_before"] if rollback else row["sha256_after"]
            if digest(current) not in {expected, desired}:
                raise RuntimeError(f"Mudança posterior preservada: {row['path']}")
            field = "before" if rollback else "after"
            if row.get(field + "_snapshot"):
                snapshot = target(root, Path(row[field + "_snapshot"]))
                if digest(snapshot.read_bytes()) != desired:
                    raise RuntimeError("Snapshot de controle divergente")
        manifest["status"] = "rolling_back" if rollback else "applying"
        romtool.write_manifest(manifest)
        for row in rows:
            path = target(root, Path(row["path"]))
            current = path.read_bytes() if path.exists() else None
            desired = row["sha256_before"] if rollback else row["sha256_after"]
            if digest(current) == desired:
                continue
            # Revalidate immediately before each atomic change, including resume.
            if digest(current) != (row["sha256_after"] if rollback else row["sha256_before"]):
                raise RuntimeError(f"Configuração mudou durante a aplicação: {row['path']}")
            if rollback and not row["existed_before"]:
                destination = manifest_path.parent / "rollback/generated" / row["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(path, destination)
            else:
                from emutool import _atomic_write
                snapshot = root / row[("before" if rollback else "after") + "_snapshot"]
                _atomic_write(path, snapshot.read_bytes(), row["mode_before"])
            actual = path.read_bytes() if path.exists() else None
            if digest(actual) != desired:
                raise RuntimeError(f"Verificação posterior falhou: {row['path']}")
            romtool.append_event(operation_id, "controller_config_restored" if rollback else "controller_config_applied",
                                 status="completed", source=row["path"], destination=row["path"],
                                 sha256_before=digest(current), sha256_after=desired)
        manifest["status"] = "rolled_back" if rollback else "completed_verified"
        romtool.write_manifest(manifest)
        if not manifest.get('retrofe_only'): romtool.rebuild_history_index()
    return {"operation_id": operation_id, "status": manifest["status"], "items": len(rows), "physical_gameplay_verified": False}


def prepare_runtime(root, system_id, *, require_gamepad=False):
    pads = runtime_gamepads(root,system_id)
    deadline = time.monotonic() + 5
    while require_gamepad and not pads and time.monotonic() < deadline:
        time.sleep(.2)
        pads = runtime_gamepads(root,system_id)
    routed = os.environ.get('EMULATION_ROUTED_CONTROLS') == '1'
    if len(pads) != 1 and not routed:
        if not pads:
            if require_gamepad:
                raise RuntimeError("Steam selecionou Gamepad, mas nenhum controle SDL ficou disponível para o emulador")
            print("[CONTROLE] Sem gamepad SDL; configurações de teclado existentes preservadas.")
            return
        print("[CONTROLE] Vários controles disponíveis; seleção existente de jogador preservada.")
        return
    if routed:
        pads = routed_players(pads)
    changes = proposals(root, pad=pads[0], pads=pads, routed=routed, system_id=system_id, frontends=False)
    if changes:
        profile = json.loads((root / PROFILE).read_text())
        entry = next((v for v in profile["collections"].values() if v["rom_subdir"] == system_id), {})
        names = {"psx": {"duckstation-qt", "duckstation"}, "ps2": {"pcsx2-qt"},
                 "ps3": {"rpcs3"}, "gc": {"dolphin-emu"}, "wii": {"dolphin-emu"},
                 "wiiware": {"dolphin-emu"}, "wiiu": {"Cemu", "Cemu-bin"},
                  "n3ds": {"azahar", "azahar-qt"}, "nds": {"melonDS"},
                  "xbox": {"xemu"}, "xbox360": {"xenia_canary"}, "ps4": {"shadPS4", "shadps4"}, "ps5": {"kyty_emulator", "game-run", "SharpEmu"},
                  "switch2": {"eden", "eden-cli", "eglantine"}, "psp": {"PPSSPPSDL"},
                  "dreamcast": {"flycast"}, "naomi": {"flycast"}, "naomi2": {"flycast"},
                  "naomigd": {"flycast"}, "atomiswave": {"flycast"}}.get(system_id, set())
        if entry.get("adapter") == "retroarch":
            names = {"retroarch"}
        for process in Path("/proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                if (process / "comm").read_text().strip() in names or (process/'exe').resolve().name in names:
                    raise RuntimeError("O emulador já está em uso; suas configurações foram preservadas. Encerre-o pelo menu antes de preparar outra configuração.")
            except OSError:
                pass
        manifest = plan(root, system_id=system_id, frontends=False)
        result = execute(root, manifest["operation_id"], "apply", runtime=True)
        print(f"[CONTROLE] {system_id}: {result['items']} configurações verificadas ({result['operation_id']}).", flush=True)
