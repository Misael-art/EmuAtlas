#!/usr/bin/env python3
"""Materialize host collection paths and isolated emulator configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (
            (candidate / "settings.conf").is_file()
            and (candidate / "Config/profiles/host-linux.json").is_file()
            and (candidate / "collections").is_dir()
        ):
            return candidate
    raise RuntimeError("Não encontrei a raiz portátil do RetroFE acima do materializador.")


def verification_root() -> Path:
    """Allow a staged copy to verify absolute paths for its deployment target."""
    configured = os.environ.get("RETROFE_TARGET_ROOT", "").strip()
    if not configured:
        return ROOT.resolve()
    target = Path(configured).expanduser()
    if not target.is_absolute():
        target = ROOT / target
    target = target.resolve()
    if not target.is_dir():
        raise RuntimeError(f"RETROFE_TARGET_ROOT não é uma pasta existente: {target}")
    return target


def installation_root() -> Path:
    """Use the future installation path when materializing a staged copy."""
    configured = os.environ.get("RETROFE_TARGET_ROOT", "").strip()
    if not configured:
        return ROOT.resolve()
    target = Path(configured).expanduser()
    if not target.is_absolute():
        target = ROOT / target
    return target.resolve()


ROOT = find_project_root()
PROFILE_PATH = ROOT / "Config/profiles/host-linux.json"
TEMPLATES = ROOT / "Config/templates"


def host_path(value: str, variable: str) -> Path:
    path = Path(os.environ.get(variable, value)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def set_conf_keys(text: str, values: dict[str, str]) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=", line)
        key = match.group(1) if match else None
        if key not in values:
            output.append(line)
        elif key not in seen:
            output.append(f"{key} = {values[key]}")
            seen.add(key)
    output.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    return "\n".join(output) + "\n"


def collapse_first(text: str, key: str) -> str:
    lines = text.splitlines()
    seen = False
    output: list[str] = []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for line in lines:
        if pattern.match(line):
            if seen:
                continue
            seen = True
        output.append(line)
    return "\n".join(output) + "\n"


def set_ini_keys(text: str, section: str, values: dict[str, str]) -> str:
    lines = text.splitlines()
    current = ""
    found_section = False
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        section_match = re.match(r"^\s*\[([^]]+)\]\s*$", line)
        if section_match:
            if current.casefold() == section.casefold():
                output.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
            current = section_match.group(1)
            if current.casefold() == section.casefold():
                found_section = True
            output.append(line)
            continue
        if current.casefold() == section.casefold():
            match = re.match(r"^\s*([^=]+?)\s*=", line)
            key = match.group(1).strip() if match else None
            if key in values:
                if key not in seen:
                    output.append(f"{key} = {values[key]}")
                    seen.add(key)
                continue
        output.append(line)
    if current.casefold() == section.casefold():
        output.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    elif not found_section:
        output.append("")
        output.append(f"[{section}]")
        output.extend(f"{key} = {value}" for key, value in values.items())
    return "\n".join(output) + "\n"


def write_launcher(name: str) -> None:
    path = ROOT / "launchers.linux" / f"{name}.conf"
    path.write_text(
        "executable = %RETROFE_PATH%/Tools/launchers/emulators/host-emulator-launcher.py\n"
        'arguments = --collection "%ITEM_COLLECTION_NAME%" --item "%ITEM_FILEPATH%"\n'
        "currentDirectory = %RETROFE_PATH%/Tools/launchers/emulators\n",
        encoding="utf-8",
    )


def materialize() -> dict[str, object]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    canonical_root = installation_root()
    rom_root = host_path(profile["rom_root"], "RETROFE_ROM_ROOT")
    bios_root = host_path(profile["bios_root"], "RETROFE_BIOS_ROOT")
    templates = TEMPLATES / "host-collections"
    templates.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []

    sega32x = ROOT / "collections/Sega 32X/settings.conf"
    sega32x_template = TEMPLATES / "sega32x-settings.conf.original"
    if sega32x.is_file() and not sega32x_template.exists():
        shutil.copy2(sega32x, sega32x_template)

    for collection, system in profile["collections"].items():
        active = ROOT / "collections" / collection / "settings.conf"
        active.parent.mkdir(parents=True, exist_ok=True)
        template = templates / f"{collection}.settings.conf.original"
        if active.exists() and not template.exists():
            shutil.copy2(active, template)
        # Keep active collection settings as the base so host routing changes do
        # not overwrite project-specific media paths or other local settings.
        if active.is_file():
            text = active.read_text(encoding="utf-8", errors="replace")
        elif template.is_file():
            text = template.read_text(encoding="utf-8", errors="replace")
        else:
            text = "# Host profile collection configuration.\n"

        values = {
            "list.path": f"%BASE_ITEM_PATH%/{system['rom_subdir']}",
            "list.includeMissingItems": "false",
            "list.extensions": system["extensions"],
            "list.menuSort": "true",
            "launcher": system["launcher"],
        }
        text = set_conf_keys(text, values)
        active.write_text(collapse_first(text, "media.logo"), encoding="utf-8")
        generated.append(collection)

    alias_templates = TEMPLATES / "host-collections"
    preserved_alias_templates = TEMPLATES / "subcollections-original"
    for collection, system in profile["collections"].items():
        for alias in system.get("subcollections", []):
            alias_system = {**system, **profile.get("subcollection_overrides", {}).get(alias, {})}
            alias_path = ROOT / "collections" / alias / "settings.conf"
            alias_template = alias_templates / f"{alias}.settings.conf.original"
            # Existing alias settings are authoritative for local media and UI
            # configuration; templates only seed collections that do not exist.
            if alias_path.is_file():
                text = alias_path.read_text(encoding="utf-8", errors="replace")
                if not alias_template.exists():
                    alias_template.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(alias_path, alias_template)
            elif alias_template.exists():
                text = alias_template.read_text(encoding="utf-8", errors="replace")
            else:
                existing_template = preserved_alias_templates / f"{alias}.settings.conf.original"
                if existing_template.is_file():
                    text = existing_template.read_text(encoding="utf-8", errors="replace")
                    shutil.copy2(existing_template, alias_template)
                else:
                    parent_template = alias_templates / f"{collection}.settings.conf.original"
                    text = parent_template.read_text(encoding="utf-8", errors="replace") if parent_template.is_file() else "# Generated host subcollection profile.\n"
                    alias_template.parent.mkdir(parents=True, exist_ok=True)
                    alias_template.write_text(text, encoding="utf-8")
            alias_values = {
                "list.path": f"%BASE_ITEM_PATH%/{alias_system['rom_subdir']}",
                "list.includeMissingItems": "false",
                "list.extensions": alias_system["extensions"],
                "list.menuSort": "true",
                "launcher": alias_system["launcher"],
            }
            alias_path.parent.mkdir(parents=True, exist_ok=True)
            text = set_conf_keys(text, alias_values)
            alias_path.write_text(collapse_first(text, "media.logo"), encoding="utf-8")

    for launcher_name in ("RetroArch Host", "Host Flatpak", "Host Emulator Unavailable", "Host MAME", "Host Command"):
        write_launcher(launcher_name)

    ra = profile["retroarch"]
    core_root = host_path(ra["core_root"], "RETROFE_RETROARCH_CORE_ROOT")
    ra_template = ROOT / ra["config_template"]
    ra_path = ROOT / ra["config_path"]
    ra_template.parent.mkdir(parents=True, exist_ok=True)
    ra_path.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "../../emulator/data/ROMCache").mkdir(parents=True, exist_ok=True)
    for subdir in ("saves", "states"):
        (ra_path.parent / subdir).mkdir(parents=True, exist_ok=True)
    if not ra_template.exists():
        ra_template.write_text(
            "# Original host profile template; paths are materialized per installation.\n"
            'system_directory = ""\n'
            'libretro_directory = ""\n'
            'savefile_directory = ""\n'
            'savestate_directory = ""\n',
            encoding="utf-8",
        )
    ra_text = ra_template.read_text(encoding="utf-8", errors="replace")
    ra_values = {
        "system_directory": f'"{bios_root}"',
        "libretro_directory": f'"{core_root}"',
        "savefile_directory": f'"{(canonical_root / ra["savefile_directory"]).resolve()}"',
        "savestate_directory": f'"{(canonical_root / ra["savestate_directory"]).resolve()}"',
        "config_save_on_exit": "false",
    }
    ra_path.write_text(set_conf_keys(ra_text, ra_values), encoding="utf-8")

    pcsx2_template = TEMPLATES / "host-apps/PCSX2.ini.original"
    # PCSX2 appends `PCSX2` to -datapath before resolving the INI directory.
    pcsx2_path = ROOT / "../../emulator/data/PCSX2/PCSX2/inis/PCSX2.ini"
    source_pcsx2 = Path.home() / ".var/app/net.pcsx2.PCSX2/config/PCSX2/inis/PCSX2.ini"
    pcsx2_template.parent.mkdir(parents=True, exist_ok=True)
    pcsx2_path.parent.mkdir(parents=True, exist_ok=True)
    if source_pcsx2.is_file() and not pcsx2_template.exists():
        shutil.copy2(source_pcsx2, pcsx2_template)
    # Preserve the active emulator configuration; templates only initialize a
    # missing project-owned configuration file.
    if pcsx2_path.is_file():
        pcsx2_text = pcsx2_path.read_text(encoding="utf-8", errors="replace")
    elif pcsx2_template.is_file():
        pcsx2_text = pcsx2_template.read_text(encoding="utf-8", errors="replace")
    else:
        pcsx2_text = ""
    if pcsx2_text:
        pcsx2_text = set_ini_keys(pcsx2_text, "Folders", {
            "Bios": str(bios_root),
        })
        pcsx2_text = set_ini_keys(pcsx2_text, "GameList", {
            "RecursivePaths": str(rom_root / "ps2"),
        })
        pcsx2_path.write_text(pcsx2_text, encoding="utf-8")

    melonds = profile["melonDS"]
    melonds_template = ROOT / melonds["config_template"]
    melonds_path = ROOT / melonds["config_path"]
    source_melonds = Path.home() / ".var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml"
    melonds_template.parent.mkdir(parents=True, exist_ok=True)
    melonds_path.parent.mkdir(parents=True, exist_ok=True)
    for subdir in ("saves", "states", "cheats"):
        (ROOT / melonds["userdata_root"] / subdir).mkdir(parents=True, exist_ok=True)
    if source_melonds.is_file() and not melonds_template.exists():
        shutil.copy2(source_melonds, melonds_template)
    # Preserve active settings from the emulator between materialization runs.
    if melonds_path.is_file():
        text = melonds_path.read_text(encoding="utf-8", errors="replace")
    elif melonds_template.is_file():
        text = melonds_template.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
    if text:
        bios_values = {
            key: json.dumps(str(bios_root / filename))
            for key, filename in melonds["bios_files"].items()
        }
        text = set_ini_keys(text, "DS", bios_values)
        text = set_ini_keys(text, "Emu", {"ExternalBIOSEnable": "true"})
        text = set_ini_keys(text, "Instance0", {
            "SaveFilePath": json.dumps(str((canonical_root / melonds["userdata_root"] / "saves/default").resolve())),
            "SavestatePath": json.dumps(str((canonical_root / melonds["userdata_root"] / "states/default").resolve())),
            "CheatFilePath": json.dumps(str((canonical_root / melonds["userdata_root"] / "cheats/default").resolve())),
        })
        melonds_path.write_text(text, encoding="utf-8")

    return {
        "collections": generated,
        "host_launchers": ["RetroArch Host", "Host Flatpak", "Host Emulator Unavailable", "Host MAME", "Host Command"],
        "retroarch_config": str(ra_path.relative_to(ROOT)),
        "pcsx2_config": str(pcsx2_path.relative_to(ROOT)) if pcsx2_path.exists() else "not-present-on-host",
        "melonds_config": str(melonds_path.relative_to(ROOT)) if melonds_path.exists() else "not-present-on-host",
    }


def check() -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    canonical_root = verification_root()
    rom_root = host_path(profile["rom_root"], "RETROFE_ROM_ROOT")
    bios_root = host_path(profile["bios_root"], "RETROFE_BIOS_ROOT")
    ra = profile["retroarch"]
    core_root = host_path(ra["core_root"], "RETROFE_RETROARCH_CORE_ROOT")
    for collection, system in profile["collections"].items():
        path = ROOT / "collections" / collection / "settings.conf"
        text = path.read_text(encoding="utf-8", errors="replace")
        values = {}
        for line in text.splitlines():
            match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", line)
            if match:
                values.setdefault(match.group(1), []).append(match.group(2))
        expected = {
            "list.path": f"%BASE_ITEM_PATH%/{system['rom_subdir']}",
            "list.includeMissingItems": "false",
            "list.extensions": system["extensions"],
            "list.menuSort": "true",
            "launcher": system["launcher"],
        }
        for key, value in expected.items():
            if values.get(key) != [value]:
                raise RuntimeError(f"{collection}: {key} expected {value!r}, found {values.get(key)!r}")
        launcher = ROOT / "launchers.linux" / f"{system['launcher']}.conf"
        if not launcher.is_file():
            raise RuntimeError(f"Missing launcher config for {collection}: {launcher}")
        for alias in system.get("subcollections", []):
            alias_system = {**system, **profile.get("subcollection_overrides", {}).get(alias, {})}
            alias_path = ROOT / "collections" / alias / "settings.conf"
            alias_text = alias_path.read_text(encoding="utf-8", errors="replace")
            alias_values = {}
            for line in alias_text.splitlines():
                match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", line)
                if match:
                    alias_values.setdefault(match.group(1), []).append(match.group(2))
            alias_expected = {
                "list.path": f"%BASE_ITEM_PATH%/{alias_system['rom_subdir']}",
                "list.includeMissingItems": "false",
                "list.extensions": alias_system["extensions"],
                "list.menuSort": "true",
                "launcher": alias_system["launcher"],
            }
            for key, value in alias_expected.items():
                if alias_values.get(key) != [value]:
                    raise RuntimeError(f"{alias}: {key} expected {value!r}, found {alias_values.get(key)!r}")

    ra_text = (ROOT / ra["config_path"]).read_text(encoding="utf-8", errors="replace")
    for path in (str(bios_root), str((canonical_root / ra["savefile_directory"]).resolve()), str((canonical_root / ra["savestate_directory"]).resolve())):
        if path not in ra_text:
            raise RuntimeError(f"RetroArch config does not contain expected path: {path}")
    # The launcher rewrites libretro_directory per launch: the host core root or a collection's project core_root.
    core_roots = {str(core_root)} | {str((canonical_root / system["core_root"]).resolve())
                                     for system in profile["collections"].values() if system.get("core_root")}
    if not any(root in ra_text for root in core_roots):
        raise RuntimeError(f"RetroArch config does not contain any declared core root: {sorted(core_roots)}")
    for collection, system in profile["collections"].items():
        system_core_root = (canonical_root / system["core_root"]).resolve() if system.get("core_root") else core_root
        if system["adapter"] == "retroarch" and not (system_core_root / system["core"]).is_file():
            raise RuntimeError(f"RetroArch core missing for {collection}: {system['core']}")

    melonds = profile["melonDS"]
    melonds_text = (ROOT / melonds["config_path"]).read_text(encoding="utf-8", errors="replace")
    if "ExternalBIOSEnable = true" not in melonds_text:
        raise RuntimeError("melonDS external BIOS is not enabled")
    for filename in melonds["bios_files"].values():
        path = bios_root / filename
        if not path.is_file() or str(path) not in melonds_text:
            raise RuntimeError(f"melonDS BIOS/firmware is missing or not configured: {path}")

    pcsx2 = ROOT / "../../emulator/data/PCSX2/PCSX2/inis/PCSX2.ini"
    if pcsx2.exists():
        text = pcsx2.read_text(encoding="utf-8", errors="replace")
        if f"Bios = {bios_root}" not in text:
            raise RuntimeError("PCSX2 BIOS path was not materialized")
        if f"RecursivePaths = {rom_root / 'ps2'}" not in text:
            raise RuntimeError("PCSX2 ROM search path was not materialized")
    print("Host profile materialization checks passed.")


parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true", help="read-only verification; set RETROFE_TARGET_ROOT when checking a staged copy")
args = parser.parse_args()
if args.check:
    check()
else:
    print(json.dumps(materialize(), ensure_ascii=False, indent=2))
