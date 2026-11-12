#!/usr/bin/env python3
"""Audit ROM platform folders, host mappings, and RetroFE .sub collections."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "Config/platform-catalog.json").is_file() and (candidate / "Manifests").is_dir():
            return candidate
    raise RuntimeError("Não encontrei a raiz do RetroFE acima do auditor.")


ROOT = find_project_root()
PROFILE_PATH = ROOT / "Config/profiles/host-linux.json"
CATALOG_PATH = ROOT / "Config/platform-catalog.json"
PLATFORM_REPORT = ROOT / "Manifests/platform-coverage.csv"
SUBCOLLECTION_REPORT = ROOT / "Manifests/subcollection-coverage.csv"
JSON_REPORT = ROOT / "Manifests/platform-coverage.json"
PLATFORM_FIELDS = [
    "system_id", "display_name", "kind", "rom_subdir", "extensions", "observed_extensions", "matching_files", "nested_directories",
    "collection", "menu_visible", "adapter", "launcher", "core", "host_mapping", "bios_status", "status",
    "esde_defined", "esde_theme", "esde_canonical_path", "retrofe_theme_logo", "retrofe_device",
]
SUBCOLLECTION_FIELDS = [
    "sub_path", "subset_name", "parent_collection", "profile_declared", "alias_settings_exists",
    "rom_subdir", "entries", "exact_matches", "expected_source_matches", "dominant_source",
    "dominant_matches", "status",
]
# roms/media is stray scraper output, not a platform directory.
NON_PLATFORM_DIRS = {"bios", ".steamzero", "media"}


def resolved_host_path(value: str, variable: str, root: Path) -> Path:
    configured = os.environ.get(variable, value)
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def available_flatpaks() -> tuple[set[str], bool]:
    executable = shutil.which("flatpak")
    if not executable:
        return set(), False
    result = subprocess.run(
        [executable, "list", "--app", "--columns=application"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        return set(), False
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}, True


def read_menu() -> set[str]:
    path = ROOT / "collections/Main/menu.txt"
    if not path.is_file():
        return set()
    return {line.strip().casefold() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()}


def esde_definitions():
    """Read packaged defaults without launching or extracting another frontend."""
    directory = ROOT.parents[1] / 'frontend/ES-DE'
    definitions = {}
    image = directory / 'ES-DE_x64_SteamDeck.AppImage'
    executable = shutil.which('unsquashfs')
    if executable and image.is_file():
        with image.open('rb') as stream:
            header = stream.read(4 * 1024 * 1024)
        offset = header.find(b'hsqs')
        while offset >= 0:
            result = subprocess.run([executable, '-cat', '-o', str(offset), str(image),
                'usr/share/es-de/resources/systems/linux/es_systems.xml'],
                capture_output=True, timeout=20)
            if result.returncode == 0:
                definitions = {s.findtext('name'): s for s in ET.fromstring(result.stdout).findall('system')}
                break
            offset = header.find(b'hsqs', offset + 1)
    custom = directory / 'UserData/custom_systems/es_systems.xml'
    if custom.is_file():
        for system in ET.parse(custom).getroot().findall('system'):
            definitions[system.findtext('name')] = system
    return definitions


def matching_files(directory: Path, extensions: set[str]) -> tuple[int, list[str], Counter[str], dict[str, set[str]]]:
    count = 0
    nested_directories: list[str] = []
    by_extension: Counter[str] = Counter()
    stem_sources: dict[str, set[str]] = {}
    if not directory.is_dir() or directory.is_symlink():
        return count, nested_directories, by_extension, stem_sources

    for base, directories, files in os.walk(directory, followlinks=False):
        current = Path(base)
        directories[:] = sorted(
            (name for name in directories if not (current / name).is_symlink()),
            key=str.casefold,
        )
        if current != directory:
            nested_directories.append(current.relative_to(directory).as_posix())
        for name in files:
            path = current / name
            if path.suffix.lstrip(".").casefold() not in extensions:
                continue
            if path.is_symlink():
                try:
                    target = path.resolve(strict=True)
                    if path.suffix.casefold() != '.exefs' or not target.is_relative_to(directory.resolve()) or not target.is_file():
                        continue
                    with target.open('rb') as stream:
                        if stream.read(4) != b'NSO0':
                            continue
                except OSError:
                    continue
            extension = path.suffix.lstrip(".").casefold()
            count += 1
            by_extension[extension] += 1
            stem_sources.setdefault(path.stem.casefold(), set()).add(directory.name)
    return count, nested_directories, by_extension, stem_sources


def profile_mapping_status(
    collection: str | None,
    system: dict | None,
    profile: dict,
    core_root: Path,
    bios_root: Path,
    mame_available: bool,
    installed_flatpaks: set[str],
    flatpak_query_ok: bool,
) -> tuple[str, str, str, str, str]:
    if not system:
        return "", "", "", "", ""

    adapter = str(system.get("adapter", ""))
    launcher = str(system.get("launcher", ""))
    core = str(system.get("core", ""))
    bios_status = "not_declared"
    if adapter == "unavailable":
        bios_status = "profile_reports_missing" if "bios" in str(system.get("reason", "")).casefold() else "not_declared"
        if str(system.get("profile_kind", "")).casefold() == "not_applicable":
            return adapter, launcher, core, "not_applicable_on_host", bios_status
        return adapter, launcher, core, "blocked_by_host_profile", bios_status

    if adapter == "retroarch":
        app_id = profile.get("retroarch", {}).get("app_id", "")
        if not core or not (core_root / core).is_file():
            return adapter, launcher, core, "missing_retroarch_core", bios_status
        if app_id and app_id not in installed_flatpaks:
            state = "retroarch_app_unknown" if not flatpak_query_ok else "missing_retroarch_app"
            return adapter, launcher, core, state, bios_status
        return adapter, launcher, core, "configured_available", bios_status

    if adapter == "mame_named":
        return adapter, launcher, core, "configured_available" if mame_available else "missing_mame", bios_status

    if adapter == "mame":
        mame_system = profile.get("systems", {}).get(collection or "")
        if not mame_available:
            return adapter, launcher, core, "missing_mame", bios_status
        if not mame_system:
            return adapter, launcher, core, "missing_mame_system_profile", bios_status
        required_bios = [bios_root / filename for filename in system.get("bios_files", [])]
        if required_bios and any(not path.is_file() for path in required_bios):
            return adapter, launcher, core, "missing_mame_bios_or_romset", "required_bios_missing"
        if required_bios:
            bios_status = "required_bios_present"
        return adapter, launcher, core, "configured_available", bios_status

    if adapter == "flatpak":
        app_id = str(system.get("app_id", ""))
        if not app_id:
            return adapter, launcher, core, "missing_flatpak_app_id", bios_status
        if app_id in installed_flatpaks:
            required_bios = [bios_root / filename for filename in system.get("bios_files", [])]
            if required_bios and any(not path.is_file() for path in required_bios):
                missing = ",".join(path.name for path in required_bios if not path.is_file())
                return adapter, launcher, core, f"missing_bios:{missing}", "required_bios_missing"
            if required_bios:
                bios_status = "required_bios_present"
            elif system.get("bios_subdir") and (bios_root / system["bios_subdir"]).is_dir():
                bios_status = "bios_directory_present"
            return adapter, launcher, core, "configured_available", bios_status
        state = "flatpak_app_unknown" if not flatpak_query_ok else "missing_flatpak_app"
        return adapter, launcher, core, state, bios_status

    if adapter == "command":
        command = str(system.get("command", ""))
        configured = os.environ.get(str(system.get("command_env", "")), command)
        executable = Path(os.path.expandvars(os.path.expanduser(configured)))
        if executable.is_absolute():
            available = executable.is_file() and os.access(executable, os.X_OK)
        elif "/" in configured:
            executable = (ROOT / executable).resolve()
            available = executable.is_file() and os.access(executable, os.X_OK)
        else:
            available = bool(shutil.which(configured))
        return adapter, launcher, core, "configured_available" if available else "missing_command", bios_status

    return adapter, launcher, core, "unknown_adapter", bios_status


def host_candidate(
    system: dict,
    core_root: Path,
    profile: dict,
    mame_available: bool,
    installed_flatpaks: set[str],
) -> tuple[str, str, str]:
    for core in system.get("preferred_retroarch_cores", []):
        if (core_root / core).is_file():
            return "retroarch", str(core), "RetroArch Host"

    hints = {re.sub(r"[^A-Z0-9]", "", str(value).upper()) for value in system.get("emulator_hints", [])}
    if "MAME" in hints and mame_available:
        return "mame", "", "Host MAME"

    for app_id, app in profile.get("flatpak_apps", {}).items():
        if app_id not in installed_flatpaks:
            continue
        tokens = {
            re.sub(r"[^A-Z0-9]", "", app_id.rsplit(".", 1)[-1].upper()),
            re.sub(r"[^A-Z0-9]", "", str(app.get("command", "")).upper()),
        }
        if hints.intersection(tokens):
            return "flatpak", "", "Host Flatpak"
    return "", "", ""


def serialize_csv(rows: list[dict[str, str]], fields: list[str]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def build_reports(rom_root: Path) -> tuple[str, str, str, dict[str, int]]:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_systems = catalog.get("systems", [])
    profile_by_root: dict[str, tuple[str, dict]] = {}
    profile_aliases: dict[tuple[str, str], bool] = {}
    for collection, system in profile.get("collections", {}).items():
        if system.get("rom_subdir"):
            profile_by_root.setdefault(str(system["rom_subdir"]).casefold(), (collection, system))
        for alias in system.get("subcollections", []):
            profile_aliases[(collection.casefold(), str(alias).casefold())] = True

    core_root = resolved_host_path(profile.get("retroarch", {}).get("core_root", ""), "RETROFE_RETROARCH_CORE_ROOT", ROOT)
    bios_root = resolved_host_path(profile.get("bios_root", ""), "RETROFE_BIOS_ROOT", ROOT)
    mame_value = os.environ.get("RETROFE_MAME", profile.get("mame_executable", "mame"))
    mame_path = Path(mame_value).expanduser()
    if not mame_path.is_absolute() and "/" in mame_value:
        mame_path = (ROOT / mame_path).resolve()
    mame_available = (mame_path.is_file() and os.access(mame_path, os.X_OK)) or bool(shutil.which(mame_value))
    installed_flatpaks, flatpak_query_ok = available_flatpaks()
    menu = read_menu()
    esde_by_id = esde_definitions()

    def artwork(collection, kind):
        if not collection:
            return 'missing'
        folder = ROOT / 'collections/Main/medium_artwork' / kind
        return 'present' if any((folder / (collection + ext)).is_file() for ext in ('.png', '.jpg', '.jpeg')) else 'missing'

    platform_rows: list[dict[str, str]] = []
    platform_json: list[dict[str, object]] = []
    stem_index: dict[str, set[str]] = {}
    known_ids: set[str] = set()
    for system in catalog_systems:
        system_id = str(system["id"])
        known_ids.add(system_id.casefold())
        rom_subdir = str(system.get("rom_subdir", system_id))
        directory = rom_root / rom_subdir
        extensions = {str(extension).casefold().lstrip(".") for extension in system.get("extensions", [])}
        observed_extensions = {
            str(extension).casefold().lstrip(".")
            for extension in system.get("observed_file_extensions", [])
        }
        mapping = profile_by_root.get(rom_subdir.casefold())
        collection, host_system = mapping if mapping else (None, None)
        scan_extensions = observed_extensions if system.get("kind") == "unclassified" else extensions
        if host_system:
            scan_extensions = scan_extensions | set(str(host_system.get('extensions', '')).casefold().split(','))
        file_count, nested, by_extension, index = matching_files(directory, scan_extensions)
        if system.get("kind") != "unclassified":
            for stem, sources in index.items():
                stem_index.setdefault(stem, set()).update(sources)

        adapter, launcher, core, host_mapping, bios_status = profile_mapping_status(
            collection, host_system, profile, core_root, bios_root, mame_available,
            installed_flatpaks, flatpak_query_ok,
        )
        if system.get("kind") == "support_tools":
            host_mapping = "not_a_game_platform"
            status = "support_tool_directory"
        elif system.get("kind") == "unclassified":
            host_mapping = "metadata_missing_manual_review"
            status = "platform_metadata_missing" if directory.is_dir() else "catalogued_rom_directory_missing"
        elif host_system:
            if host_mapping == "not_applicable_on_host":
                status = "not_applicable_on_host"
            elif host_mapping == "blocked_by_host_profile":
                status = "host_blocked_with_content" if file_count else "host_blocked_empty"
            elif host_mapping == "configured_available":
                status = "profile_ready_with_content" if file_count else "profile_ready_empty"
            else:
                status = "profile_missing_dependency"
        elif not directory.is_dir():
            status = "catalogued_rom_directory_missing"
        elif file_count:
            adapter, core, launcher = host_candidate(system, core_root, profile, mame_available, installed_flatpaks)
            if adapter == "retroarch":
                host_mapping = "host_core_available_profile_missing"
            elif adapter == "mame":
                host_mapping = "mame_available_profile_missing"
            elif adapter == "flatpak":
                host_mapping = "flatpak_available_profile_missing"
            else:
                host_mapping = "host_mapping_unavailable_or_undetected"
            status = "profile_missing_with_content"
        elif not directory.is_dir():
            status = "catalogued_rom_directory_missing"
        else:
            adapter, core, launcher = host_candidate(system, core_root, profile, mame_available, installed_flatpaks)
            status = "empty_host_mapping_available" if adapter else "empty_without_host_mapping"
            if adapter == "retroarch":
                host_mapping = "host_core_available_profile_missing"
            elif adapter == "mame":
                host_mapping = "mame_available_profile_missing"
            elif adapter == "flatpak":
                host_mapping = "flatpak_available_profile_missing"

        menu_visible = bool(collection and collection.casefold() in menu)
        esde = esde_by_id.get(system_id)
        row = {
            "system_id": system_id,
            "display_name": str(system.get("display_name", system_id)),
            "kind": str(system.get("kind", "platform")),
            "rom_subdir": rom_subdir,
            "extensions": ",".join(sorted(extensions)),
            "observed_extensions": ",".join(sorted(by_extension)),
            "matching_files": str(file_count),
            "nested_directories": str(len(nested)),
            "collection": collection or "",
            "menu_visible": "yes" if menu_visible else "no",
            "adapter": adapter,
            "launcher": launcher,
            "core": core,
            "host_mapping": host_mapping,
            "bios_status": bios_status,
            "status": status,
            "esde_defined": "yes" if esde is not None else "no",
            "esde_theme": esde.findtext('theme', '') if esde is not None else '',
            "esde_canonical_path": ('yes' if esde.findtext('path') == '%ROMPATH%/' + rom_subdir else 'no') if esde is not None else '',
            "retrofe_theme_logo": artwork(collection, 'logo_aeon_nox_light_white'),
            "retrofe_device": artwork(collection, 'device'),
        }
        platform_rows.append(row)
        platform_json.append(
            {
                **row,
                "extensions": sorted(extensions),
                "observed_extensions": sorted(by_extension),
                "nested_directories": nested,
                "matching_files_by_extension": dict(sorted(by_extension.items())),
                "host_mapping_reason": host_system.get("reason", "") if host_system else "",
            }
        )

    live_system_dirs = {
        path.name.casefold(): path
        for path in rom_root.iterdir()
        if path.is_dir() and not path.is_symlink() and (path / "systeminfo.txt").is_file()
    } if rom_root.is_dir() else {}
    for system_id, directory in sorted(live_system_dirs.items()):
        if system_id in known_ids:
            continue
        platform_rows.append({
            "system_id": directory.name,
            "display_name": "",
                "kind": "unclassified",
            "rom_subdir": directory.name,
            "extensions": "",
            "matching_files": "0",
            "nested_directories": "0",
            "collection": "",
            "menu_visible": "no",
            "adapter": "",
            "launcher": "",
            "core": "",
            "host_mapping": "refresh_platform_catalog",
            "bios_status": "not_checked",
            "status": "uncatalogued_systeminfo_directory",
        })

    unknown_directories = []
    if rom_root.is_dir():
        unknown_directories = sorted(
            path.name for path in rom_root.iterdir()
            if path.is_dir() and not path.is_symlink()
            and path.name.casefold() not in NON_PLATFORM_DIRS
            and path.name.casefold() not in known_ids
            and path.name.casefold() not in live_system_dirs
        )

    subset_rows: list[dict[str, str]] = []
    subset_json: list[dict[str, object]] = []
    declared_aliases: set[tuple[str, str]] = set()
    overrides = profile.get("subcollection_overrides", {})
    for parent, system in profile.get("collections", {}).items():
        for alias in system.get("subcollections", []):
            declared_aliases.add((parent.casefold(), str(alias).casefold()))

    for subset_path in sorted((ROOT / "collections").rglob("*.sub"), key=lambda path: path.as_posix().casefold()):
        if subset_path.is_symlink():
            continue
        parent_path = subset_path.parent.relative_to(ROOT / "collections")
        parent = parent_path.as_posix()
        parent_name = subset_path.parent.name
        alias = subset_path.stem
        declared = (parent_name.casefold(), alias.casefold()) in declared_aliases
        parent_system = profile.get("collections", {}).get(parent_name)
        source = overrides.get(alias, {})
        rom_subdir = str(source.get("rom_subdir", parent_system.get("rom_subdir", "") if parent_system else ""))
        lines = [
            line.strip() for line in subset_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        resolved = [stem_index.get(line.casefold(), set()) for line in lines]
        exact_matches = sum(bool(matches) for matches in resolved)
        expected_matches = sum(rom_subdir.casefold() in matches for matches in resolved) if rom_subdir else 0
        # Each basename may exist in several platform roots. Sort the set
        # before counting so tied dominant sources are stable across runs.
        source_counts = Counter(
            root for matches in resolved for root in sorted(matches, key=str.casefold)
        )
        dominant_source, dominant_count = source_counts.most_common(1)[0] if source_counts else ("", 0)
        if not lines:
            state = "empty_subset"
        elif parent_system and not declared:
            state = "system_subset_missing_profile_declaration"
        elif declared and not (ROOT / "collections" / alias / "settings.conf").is_file():
            state = "profile_alias_settings_missing"
        elif rom_subdir and dominant_source and dominant_source.casefold() != rom_subdir.casefold() and dominant_count > expected_matches:
            state = "subset_source_may_differ_from_parent"
        elif exact_matches == len(lines):
            state = "all_entries_match_by_filename"
        elif exact_matches:
            state = "partially_matches_by_filename"
        else:
            state = "no_exact_filename_match"

        row = {
            "sub_path": subset_path.relative_to(ROOT).as_posix(),
            "subset_name": alias,
            "parent_collection": parent,
            "profile_declared": "yes" if declared else "no",
            "alias_settings_exists": "yes" if (ROOT / "collections" / alias / "settings.conf").is_file() else "no",
            "rom_subdir": rom_subdir,
            "entries": str(len(lines)),
            "exact_matches": str(exact_matches),
            "expected_source_matches": str(expected_matches),
            "dominant_source": dominant_source,
            "dominant_matches": str(dominant_count),
            "status": state,
        }
        subset_rows.append(row)
        subset_json.append({**row, "entries": len(lines), "exact_matches": exact_matches})

    # Include profile aliases whose source .sub file is missing.
    for parent, system in profile.get("collections", {}).items():
        for alias in system.get("subcollections", []):
            expected_path = ROOT / "collections" / parent / f"{alias}.sub"
            if expected_path.is_file():
                continue
            row = {
                "sub_path": expected_path.relative_to(ROOT).as_posix(),
                "subset_name": alias,
                "parent_collection": parent,
                "profile_declared": "yes",
                "alias_settings_exists": "yes" if (ROOT / "collections" / alias / "settings.conf").is_file() else "no",
                "rom_subdir": str(overrides.get(alias, {}).get("rom_subdir", system.get("rom_subdir", ""))),
                "entries": "0",
                "exact_matches": "0",
                "expected_source_matches": "0",
                "dominant_source": "",
                "dominant_matches": "0",
                "status": "profile_alias_file_missing",
            }
            subset_rows.append(row)
            subset_json.append({**row, "entries": 0, "exact_matches": 0})

    platform_rows.sort(key=lambda row: row["system_id"].casefold())
    subset_rows.sort(key=lambda row: row["sub_path"].casefold())
    counts = Counter(row["status"] for row in platform_rows)
    counts.update({f"subset_{key}": value for key, value in Counter(row["status"] for row in subset_rows).items()})
    summary = {
        "schema_version": 1,
        "rom_root_config": profile.get("rom_root", ""),
        "catalog_file": "Config/platform-catalog.json",
        "catalog_platforms": sum(1 for system in catalog_systems if system.get("kind") == "platform"),
        "catalog_records": len(catalog_systems),
        "catalogued_systeminfo_directories": sum(1 for system in catalog_systems if system.get("kind") != "unclassified"),
        "unclassified_catalog_directories": [
            str(system["id"]) for system in catalog_systems if system.get("kind") == "unclassified"
        ],
        "live_systeminfo_directories": len(live_system_dirs),
        "profile_collections": len(profile.get("collections", {})),
        "platforms": platform_rows,
        "subcollections": subset_json,
        "unclassified_directories_without_systeminfo": sorted(
            [str(system["id"]) for system in catalog_systems if system.get("kind") == "unclassified"]
            + unknown_directories,
            key=str.casefold,
        ),
        "status_counts": dict(sorted(counts.items())),
    }
    return (
        serialize_csv(platform_rows, PLATFORM_FIELDS),
        serialize_csv(subset_rows, SUBCOLLECTION_FIELDS),
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        dict(counts),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom-root", help="substitui RETROFE_ROM_ROOT e o caminho relativo do perfil")
    parser.add_argument("--check", action="store_true", help="compara os manifestos existentes sem gravar")
    args = parser.parse_args()
    if not CATALOG_PATH.is_file():
        print("[FALTA] Catálogo ausente. Execute primeiro Tools/materializers/materialize-platform-catalog.py.", file=sys.stderr)
        return 2

    configured_root = args.rom_root or os.environ.get("RETROFE_ROM_ROOT") or json.loads(PROFILE_PATH.read_text(encoding="utf-8"))["rom_root"]
    rom_root = Path(configured_root).expanduser()
    if not rom_root.is_absolute():
        rom_root = ROOT / rom_root
    generated = build_reports(rom_root.resolve())
    outputs = {
        PLATFORM_REPORT: generated[0],
        SUBCOLLECTION_REPORT: generated[1],
        JSON_REPORT: generated[2],
    }
    if args.check:
        stale = [path for path, content in outputs.items() if not path.is_file() or path.read_text(encoding="utf-8") != content]
        if stale:
            print("[FALTA] Relatórios desatualizados: " + ", ".join(str(path.relative_to(ROOT)) for path in stale), file=sys.stderr)
            return 1
        summary = json.loads(generated[2])
        print(f"[OK] Cobertura atualizada: {summary['catalog_platforms']} plataformas ({summary['catalog_records']} registros) e {len(summary['subcollections'])} subconjuntos.")
        return 0

    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    summary = json.loads(generated[2])
    print(f"[OK] Auditoria registrada: {summary['catalog_platforms']} plataformas, {len(summary['subcollections'])} subconjuntos.")
    print("[INFO] A auditoria só lê ROMs/BIOS e arquivos .sub; nenhum arquivo de mídia é copiado ou modificado.")
    for name, value in summary["status_counts"].items():
        print(f"  {name}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
