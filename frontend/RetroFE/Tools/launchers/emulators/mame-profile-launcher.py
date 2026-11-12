#!/usr/bin/env python3
"""Launch a configured MAME system without writing into the host MAME profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retrofe_audio import run_with_music_paused


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (
            (candidate / "settings.conf").is_file()
            and (candidate / "Config/profiles/host-linux.json").is_file()
            and (candidate / "collections").is_dir()
        ):
            return candidate
    raise RuntimeError("Não encontrei a raiz portátil do RetroFE acima do launcher.")


ROOT = find_project_root()
PROFILE_PATH = ROOT / "Config" / "profiles" / "host-linux.json"


def host_path(value: str, variable: str) -> Path:
    path = Path(os.environ.get(variable, value)).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def host_executable(value: str) -> Path:
    configured = os.environ.get("RETROFE_MAME", value)
    path = Path(configured).expanduser()
    if path.is_absolute():
        return path.resolve()
    if "/" in configured:
        return (ROOT / path).resolve()
    return Path(shutil.which(configured) or configured)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument("--seconds-to-run", type=int, help="test-only bounded run")
    args = parser.parse_args()

    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT.parent.parent / 'tools/roms'))
    from portable_emulator import native_profile
    profile = native_profile(ROOT.parent.parent, profile)
    system = profile["systems"].get(args.collection)
    if system is None:
        for parent in profile["systems"].values():
            if args.collection in parent.get("subcollections", []):
                system = parent
                break
    if system is None:
        print(f"No MAME mapping for collection: {args.collection}", file=sys.stderr)
        return 2

    item = Path(args.item).expanduser().resolve()
    if not item.is_file():
        print(f"Game file does not exist: {item}", file=sys.stderr)
        return 2
    mame = host_executable(profile["mame_executable"])
    if not mame.is_file() or not os.access(mame, os.X_OK):
        print(f"MAME executable is unavailable: {mame}", file=sys.stderr)
        return 2

    user_root = (ROOT / profile["mame_user_data"] / system["driver"]).resolve()
    dirs = {
        "cfg": user_root / profile.get('mame_config_subdir', 'cfg'),
        "nvram": user_root / "nvram",
        "input": user_root / "input",
        "state": user_root / "state",
        "snap": user_root / "snap",
        "diff": user_root / "diff",
        "samples": user_root / "samples",
    }
    for directory in dirs.values():
        directory.mkdir(parents=True, exist_ok=True)

    # The system information panel requires a keyboard acknowledgement before
    # the selected game starts. Skip that informational screen for frontend
    # launches so the configured item opens directly.
    command = [
        str(mame), system["driver"], system["media_argument"], str(item),
        "-skip_gameinfo",
        "-rompath", str(host_path(profile["bios_root"], "RETROFE_BIOS_ROOT")),
        "-cfg_directory", str(dirs["cfg"]),
        "-nvram_directory", str(dirs["nvram"]),
        "-input_directory", str(dirs["input"]),
        "-state_directory", str(dirs["state"]),
        "-snapshot_directory", str(dirs["snap"]),
        "-diff_directory", str(dirs["diff"]),
        "-samplepath", str(dirs["samples"]),
        "-noplugins",
    ]
    if args.seconds_to_run is not None:
        if args.seconds_to_run < 1:
            parser.error("--seconds-to-run must be positive")
        command.extend(["-seconds_to_run", str(args.seconds_to_run), "-video", "none", "-sound", "none"])
    print("Launching:", " ".join(command), flush=True)
    return run_with_music_paused(command, cwd=ROOT, system_id=system["rom_subdir"])


if __name__ == "__main__":
    raise SystemExit(main())
