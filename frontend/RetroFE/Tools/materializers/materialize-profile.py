#!/usr/bin/env python3
"""Rebuild the three Linux pilot collection configs from preserved templates."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
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


ROOT = find_project_root()
FE = ROOT
PROFILE = ROOT / "Config" / "profiles" / "host-linux.json"
TEMPLATES = ROOT / "Config" / "templates"
PILOT_TEMPLATE_NAMES = {
    "Nintendo Entertainment System": "nes-settings.conf.original",
    "Sega Genesis": "genesis-settings.conf.original",
    "Sega 32X": "sega32x-settings.conf.original",
}


def host_rom_root(profile: dict) -> Path:
    path = Path(os.environ.get("RETROFE_ROM_ROOT", profile["rom_root"])).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def display_settings() -> dict[str, str]:
    values = {"RETROFE_WINDOW_WIDTH": "1920", "RETROFE_WINDOW_HEIGHT": "1080", "RETROFE_FULLSCREEN": "no"}
    config = ROOT / "Config/display.env"
    if config.is_file():
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*(RETROFE_WINDOW_WIDTH|RETROFE_WINDOW_HEIGHT|RETROFE_FULLSCREEN)\s*=\s*([^#\s]+)", line)
            if match:
                values[match.group(1)] = match.group(2)
    # Same precedence as launch-retrofe.sh: display.env, host.env, then the saved display profile.
    host = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "retrofe/host.env"
    if host.is_file():
        for line in host.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^\s*(RETROFE_WINDOW_WIDTH|RETROFE_WINDOW_HEIGHT|RETROFE_FULLSCREEN)\s*=\s*([^#\s]+)", line)
            if match:
                values[match.group(1)] = match.group(2).strip("'\"")
    tool = ROOT / "Tools/launchers/retrofe/display-profile.py"
    if (ROOT / "UserData/retrofe/display-profile.json").is_file() and tool.is_file():
        result = subprocess.run([sys.executable, str(tool), "--root", str(ROOT), "env"],
                                capture_output=True, text=True, check=False)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, _, value = line.partition("=")
                if key in ("RETROFE_WINDOW_WIDTH", "RETROFE_WINDOW_HEIGHT"):
                    values[key] = value.strip()
    for key in ("RETROFE_WINDOW_WIDTH", "RETROFE_WINDOW_HEIGHT"):
        if not re.fullmatch(r"[1-9][0-9]*", values[key]):
            raise ValueError(f"{key} em Config/display.env deve ser um inteiro positivo")
    values["RETROFE_FULLSCREEN"] = {"yes": "yes", "true": "yes", "1": "yes", "no": "no", "false": "no", "0": "no"}.get(values["RETROFE_FULLSCREEN"].casefold(), "")
    if not values["RETROFE_FULLSCREEN"]:
        raise ValueError("RETROFE_FULLSCREEN em Config/display.env deve ser yes/no, true/false ou 1/0")
    return {
        "fullscreen": values["RETROFE_FULLSCREEN"],
        "fullscreen0": values["RETROFE_FULLSCREEN"],
        "fullscreen1": values["RETROFE_FULLSCREEN"],
        "horizontal": values["RETROFE_WINDOW_WIDTH"],
        "horizontal0": values["RETROFE_WINDOW_WIDTH"],
        "horizontal1": values["RETROFE_WINDOW_WIDTH"],
        "vertical": values["RETROFE_WINDOW_HEIGHT"],
        "vertical0": values["RETROFE_WINDOW_HEIGHT"],
        "vertical1": values["RETROFE_WINDOW_HEIGHT"],
    }


def set_keys(text: str, values: dict[str, str], *, dedupe: set[str] | None = None) -> str:
    dedupe = dedupe or set(values)
    lines = text.splitlines()
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=", line)
        key = match.group(1) if match else None
        if key in values:
            if key in seen:
                continue
            result.append(f"{key} = {values[key]}".rstrip())
            seen.add(key)
        elif key in dedupe:
            if key in seen:
                continue
            result.append(line)
            seen.add(key)
        elif key and key.startswith("media.") and "\\" in line:
            result.append(line.replace("\\", "/"))
        else:
            result.append(line)
    for key, value in values.items():
        if key not in seen:
            result.append(f"{key} = {value}".rstrip())
    return "\n".join(result) + "\n"


def collapse_first(text: str, key: str) -> str:
    lines = text.splitlines()
    seen = False
    result: list[str] = []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for line in lines:
        if pattern.match(line):
            if seen:
                continue
            seen = True
        result.append(line)
    return "\n".join(result) + "\n"


def active_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(2).split("#", 1)[0].strip()
            values.setdefault(match.group(1), []).append(value)
    return values


def materialize() -> None:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    rom_root = host_rom_root(profile)
    settings_template = TEMPLATES / "settings.conf.original"
    settings_path = FE / "settings.conf"
    settings = settings_template.read_text(encoding="utf-8", errors="replace")
    global_values = {
        "baseItemPath": str(rom_root),
        "exitOnFirstPageBack": "no",
        "rememberMenu": "yes",
        "collectionInputClear": "yes",
        "LEDBlinkyDirectory": "",
    }
    global_values.update(display_settings())
    settings = set_keys(settings, global_values)
    settings_path.write_text(settings, encoding="utf-8")

    for collection, system in profile["systems"].items():
        if collection not in PILOT_TEMPLATE_NAMES:
            continue
        template = TEMPLATES / PILOT_TEMPLATE_NAMES[collection]
        if not template.is_file():
            raise FileNotFoundError(f"Missing preserved settings template for {collection}: {template}")
        active = FE / "collections" / collection / "settings.conf"
        text = template.read_text(encoding="utf-8", errors="replace")
        values = {
            "list.path": f"%BASE_ITEM_PATH%/{system['rom_subdir']}",
            "list.includeMissingItems": "false",
            "list.extensions": system["extensions"],
            "launcher": system["launcher"],
        }
        text = set_keys(text, values)
        # The parser stores the first duplicate key; these old regional logo paths
        # were not a fallback list. Preserve the first as the explicit unresolved
        # LaunchBox dependency and remove only the ineffective repeated entries.
        text = collapse_first(text, "media.logo")
        active.write_text(text, encoding="utf-8")

        original_dir = TEMPLATES / "subcollections-original"
        for alias in system.get("subcollections", []):
            alias_system = {**system, **profile.get("subcollection_overrides", {}).get(alias, {})}
            alias_path = FE / "collections" / alias / "settings.conf"
            template = original_dir / f"{alias}.settings.conf.original"
            if template.is_file():
                alias_text = template.read_text(encoding="utf-8", errors="replace")
            elif alias_path.is_file():
                alias_text = alias_path.read_text(encoding="utf-8", errors="replace")
            else:
                alias_text = "# Generated Linux profile for this preserved .sub collection.\n"
            alias_values = {
                "list.path": f"%BASE_ITEM_PATH%/{alias_system['rom_subdir']}",
                "list.includeMissingItems": "false",
                "list.extensions": alias_system["extensions"],
                "launcher": alias_system["launcher"],
            }
            alias_text = set_keys(alias_text, alias_values)
            alias_text = collapse_first(alias_text, "media.logo")
            alias_path.parent.mkdir(parents=True, exist_ok=True)
            alias_path.write_text(alias_text, encoding="utf-8")

        launcher_path = FE / "launchers.linux" / f"{system['launcher']}.conf"
        launcher_path.write_text(
            "executable = %RETROFE_PATH%/Tools/launchers/emulators/mame-profile-launcher.py\n"
            'arguments = --collection "%ITEM_COLLECTION_NAME%" --item "%ITEM_FILEPATH%"\n'
            "currentDirectory = %RETROFE_PATH%/Tools/launchers/emulators\n",
            encoding="utf-8",
        )

    print(json.dumps({"profile": profile["profile"], "settings": str(settings_path), "collections": list(profile["systems"])}, ensure_ascii=False))


def check() -> None:
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    rom_root = host_rom_root(profile)
    global_cfg = active_values((FE / "settings.conf").read_text(encoding="utf-8", errors="replace"))
    assert global_cfg.get("baseItemPath") == [str(rom_root)], global_cfg.get("baseItemPath")
    expected_global = {
        "exitOnFirstPageBack": "no",
        "rememberMenu": "yes",
        "collectionInputClear": "yes",
        "LEDBlinkyDirectory": "",
        **display_settings(),
    }
    for key, value in expected_global.items():
        assert global_cfg.get(key) == [value], (key, global_cfg.get(key))
    for collection, system in profile["systems"].items():
        if collection not in PILOT_TEMPLATE_NAMES:
            continue
        cfg = active_values((FE / "collections" / collection / "settings.conf").read_text(encoding="utf-8", errors="replace"))
        expected = {
            "list.path": f"%BASE_ITEM_PATH%/{system['rom_subdir']}",
            "list.includeMissingItems": "false",
            "list.extensions": system["extensions"],
            "launcher": system["launcher"],
        }
        for key, value in expected.items():
            assert cfg.get(key) == [value], (collection, key, cfg.get(key))
        assert len(cfg.get("media.logo", [])) <= 1, (collection, cfg.get("media.logo"))
        launcher_file = FE / "launchers.linux" / f"{system['launcher']}.conf"
        launcher_values = active_values(launcher_file.read_text(encoding="utf-8", errors="replace"))
        assert launcher_values.get("executable") == ["%RETROFE_PATH%/Tools/launchers/emulators/mame-profile-launcher.py"]
        assert launcher_values.get("currentDirectory") == ["%RETROFE_PATH%/Tools/launchers/emulators"]
        for alias in system.get("subcollections", []):
            alias_system = {**system, **profile.get("subcollection_overrides", {}).get(alias, {})}
            alias_cfg = active_values((FE / "collections" / alias / "settings.conf").read_text(encoding="utf-8", errors="replace"))
            alias_expected = {
                "list.path": f"%BASE_ITEM_PATH%/{alias_system['rom_subdir']}",
                "list.includeMissingItems": "false",
                "list.extensions": alias_system["extensions"],
                "launcher": alias_system["launcher"],
            }
            for key, value in alias_expected.items():
                assert alias_cfg.get(key) == [value], (alias, key, alias_cfg.get(key))
            assert len(alias_cfg.get("media.logo", [])) <= 1, (alias, alias_cfg.get("media.logo"))
    print("Profile materialization checks passed.")


parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true", help="read-only verification of materialized configs")
args = parser.parse_args()
if args.check:
    check()
else:
    materialize()
