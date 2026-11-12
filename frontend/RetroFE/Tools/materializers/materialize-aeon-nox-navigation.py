#!/usr/bin/env python3
"""Materialize RetroFE's shared, data-backed navigation tree.

The generated collections are regular RetroFE menu and merged collections.
They contain names/references only; this tool never copies ROMs or artwork.
Run without --apply to inspect a plan. The launcher refreshes favorites and
exact-match genre playlists while preserving other full-generation collections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable


SCRIPT = Path(__file__).resolve()
DEFAULT_ROOT = SCRIPT.parents[2]
STATE_REL = Path("Manifests/aeon-nox-navigation-state.json")
STATE_VERSION = 1
MENU_COLLECTIONS = "Coleções"
LEGACY_MENU_COLLECTIONS = "- Menu Collections"
OLD_MENU_COLLECTION_NAMES = {"Categorias", "Menu Collections", LEGACY_MENU_COLLECTIONS}
DISPLAY_OPTIONS_COLLECTION = "Resolucao"
DISPLAY_PROFILE_DIR = Path("Tools/launchers/retrofe/display-profiles")
OPTIONS_COLLECTION = "Opcoes"
THEME_OPTIONS_COLLECTION = "Tema"
THEME_PROFILE_DIR = Path("Tools/launchers/retrofe/theme-profiles")
CONTROLLER_OPTIONS_COLLECTION = "Controles"
CONTROLLER_PROFILE_DIR = Path("Tools/launchers/retrofe/controller-options")
FRANCHISE_COLLECTIONS = "Franquias"
BROWSE_PLATFORMS_COLLECTION = "Explorar plataformas"
RECENT_PLATFORMS_COLLECTION = "Recentes"
GENRE_PLAYLIST_PREFIX = "Gênero - "

TYPE_DISPLAY_LABELS = {
    "Arcade": "Sistemas arcade",
    "Computers": "Computadores",
    "Console add-ons": "Acessorios de console",
    "Handheld": "Portateis",
    "Home consoles": "Consoles domesticos",
    "Hybrid consoles": "Consoles hibridos",
}

# These aliases were named by the user. A game is included only when the
# metadata title contains the complete phrase and the exact item basename is
# present in the corresponding local ROM collection.
FRANCHISE_RULES = {
    "Fatal Fury": ("fatal fury", re.compile(r"\bfatal\s+fury\b", re.IGNORECASE)),
    "Mario": ("mario", re.compile(r"\bmario\b", re.IGNORECASE)),
    "Street Fighter": ("street fighter", re.compile(r"\bstreet\s+fighter\b", re.IGNORECASE)),
}

TYPE_ALIASES = {
    "home video game console": "Home consoles",
    "home video game consoles": "Home consoles",
    "home console": "Home consoles",
    "video game console": "Home consoles",
    "video game consoles": "Home consoles",
    "handheld video game console": "Handheld",
    "handheld video game consoles": "Handheld",
    "handheld console": "Handheld",
    "handheld consoles": "Handheld",
    "handheld": "Handheld",
    "handheld game console": "Handheld",
    "handheld game consoles": "Handheld",
    "arcade system": "Arcade",
    "arcade system board": "Arcade",
    "arcade machine": "Arcade",
    "arcade machines": "Arcade",
    "arcade": "Arcade",
    "home computer": "Computers",
    "personal computer": "Computers",
    "computer": "Computers",
    "computer system": "Computers",
    "computers": "Computers",
    "microcomputer": "Computers",
    "hybrid video game console": "Hybrid consoles",
    "hybrid console": "Hybrid consoles",
    "video game add-on": "Console add-ons",
    "video game console add-on": "Console add-ons",
    "video game console add-on|homebrew": "Console add-ons",
}

MANUFACTURER_ALIASES = {
    "atari corporation": "Atari",
    "commodore business machines (cbm)": "Commodore",
    "commodore international": "Commodore",
    "nec home electronics": "NEC",
    "nintendo": "Nintendo",
    "sega": "Sega",
    "snk": "SNK",
    "sony electronics": "Sony",
    "the 3do company": "3DO Company",
}
TYPE_OVERRIDES = {
    # The local info.conf mislabels this fixed home console as handheld.
    "Nintendo GameCube": "Home consoles",
}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def text_bytes(text: str) -> bytes:
    return (text.rstrip("\n") + "\n").encode("utf-8")


def read_info(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip().casefold()] = value.strip()
    return result


def load_systems(root: Path) -> list[dict[str, object]]:
    payload = json.loads((root / "Config/systems.json").read_text(encoding="utf-8"))
    rows = payload.get("systems", [])
    if isinstance(rows, dict):
        rows = list(rows.values())
    rows = [row for row in rows if isinstance(row, dict)]
    # systems.json is an older projection. Runtime/profile additions must be
    # discovered from their declared source of truth at every launcher refresh.
    profile_path = root / "Config/profiles/host-linux.json"
    if profile_path.is_file():
        live = json.loads(profile_path.read_text(encoding="utf-8"))["collections"]
        previous = {str(row.get("collection")): row for row in rows}
        rows = [{**previous.get(name, {}), **entry, "collection": name}
                for name, entry in live.items()]
        rows.extend(row for name, row in previous.items() if name not in live
                    and not row.get("rom_subdir") in {entry["rom_subdir"] for entry in live.values()})
    return rows


def has_local_content(directory: Path, profile: dict[str, object]) -> bool:
    """Discover input/packaged content without treating notes or media as games.

    A package keeps its platform discoverable; this does not certify that its
    contents are loadable. Only confined ExeFS launch aliases may be followed.
    """
    extensions = set(str(profile.get("extensions", "")).casefold().split(","))
    extensions.update({"rar", "pkg", "exfat"})
    if not directory.is_dir() or directory.is_symlink():
        return False
    for base, folders, files in os.walk(directory, followlinks=False):
        current = Path(base)
        folders[:] = [name for name in folders if not (current / name).is_symlink()]
        for name in files:
            path = current / name
            if path.suffix.lstrip(".").casefold() not in extensions:
                continue
            if path.is_symlink():
                if path.suffix.casefold() != ".exefs":
                    continue
                try:
                    target = path.resolve(strict=True)
                    if not target.is_relative_to(directory.resolve()) or not target.is_file():
                        continue
                    with target.open("rb") as stream:
                        if stream.read(4) != b"NSO0":
                            continue
                except OSError:
                    continue
            if path.is_file() and path.stat().st_size:
                return True
    return False


def safe_name(value: str) -> str:
    if not value or value in {".", ".."} or any(ch in value for ch in "/\\\0"):
        raise ValueError(f"Nome de coleção inseguro: {value!r}")
    return value


def collection_rows(root: Path) -> list[dict[str, object]]:
    rows = []
    rom_root = root.parents[1] / "roms"
    for profile in load_systems(root):
        name = str(profile.get("collection", "")).strip()
        if not name or not (root / "collections" / name).is_dir():
            continue
        rom_dir = rom_root / str(profile.get("rom_subdir", "")).strip()
        has_local_games = has_local_content(rom_dir, profile)
        if profile.get("menu_visible") is False and not has_local_games:
            continue
        info = read_info(root / "collections" / name / "info.conf")
        rows.append({"name": name, "profile": profile, "info": info, "has_local_content": has_local_games})
    return rows


def add_main_platforms(root, rows, append_actions):
    """Add newly populated platforms without removing/reordering personal menus."""
    target = root / "collections/Main/menu.txt"
    if target.is_symlink() or not target.is_file():
        raise ValueError("Main/menu.txt precisa ser arquivo local para reconciliar plataformas")
    queued = next((data for path, data in reversed(append_actions) if path == target), None)
    current = queued if queued is not None else target.read_bytes()
    lines = current.decode("utf-8").splitlines()
    existing = {line.strip().casefold() for line in lines}
    additions = sorted((row["name"] for row in rows if row["has_local_content"]
                        and row["name"].casefold() not in existing), key=str.casefold)
    if additions:
        append_actions[:] = [(path, data) for path, data in append_actions if path != target]
        append_actions.append((target, menu_bytes([*lines, *additions])))


def metadata_groups(rows: list[dict[str, object]]) -> dict[str, dict[str, list[str]]]:
    groups: dict[str, dict[str, set[str]]] = {
        "manufacturer": defaultdict(set),
        "type": defaultdict(set),
        "year": defaultdict(set),
        "bits": defaultdict(set),
    }
    year_values: dict[str, int] = {}
    for row in rows:
        name = str(row["name"])
        info = row["info"]
        assert isinstance(info, dict)

        manufacturer = info.get("manufacturer", "").strip()
        if manufacturer and manufacturer.casefold() not in {"unknown", "n/a", "none", "-"}:
            # Comma-separated credits in info.conf denote multiple makers.
            # Group each explicit company independently so Nintendo, Sega,
            # Sony, and similar brand filters include co-manufactured systems.
            makers = [part.strip() for part in manufacturer.split(",") if part.strip()]
            for maker in makers:
                if maker.casefold() == "many":
                    groups["manufacturer"]["Other (multiple)"].add(name)
                    continue
                label = MANUFACTURER_ALIASES.get(maker.casefold(), maker)
                groups["manufacturer"][label].add(name)

        # Some legacy info.conf files contain these two values under reversed
        # keys. Accept only explicit, recognizable form-factor phrases from
        # either field; generation names are not mistaken for device types.
        override_type = TYPE_OVERRIDES.get(name)
        if override_type:
            groups["type"][override_type].add(name)
        else:
            for key in ("type", "generation"):
                value = info.get(key, "").strip().casefold()
                if value in TYPE_ALIASES:
                    groups["type"][TYPE_ALIASES[value]].add(name)
                    break

        years = [int(value) for value in re.findall(r"\b(19\d{2}|20[0-3]\d)\b", info.get("year", ""))]
        if years:
            first_release = min(years)
            groups["year"][str(first_release)].add(name)
            year_values[str(first_release)] = first_release

        cpu = info.get("cpu", "")
        bit_match = re.search(r"\b(4|8|16|32|64)\s*[- ]?bit\b", cpu, re.IGNORECASE)
        if bit_match:
            groups["bits"][f"{int(bit_match.group(1))}-bit"].add(name)

    result: dict[str, dict[str, list[str]]] = {}
    for facet, values in groups.items():
        if facet == "year":
            ordered = sorted(values, key=lambda year: (-year_values[year], year))
        elif facet == "bits":
            ordered = sorted(values, key=lambda label: int(label.split("-", 1)[0]))
        else:
            ordered = sorted(values, key=str.casefold)
        result[facet] = {label: sorted(values[label], key=str.casefold) for label in ordered}
    return result


def recent_platform_names(root: Path, rows: list[dict[str, object]]) -> list[str]:
    """Resolve RetroFE's recent collection history to currently visible platforms."""
    source = root / "collections/Main/playlists/lastplayed.txt"
    if not source.is_file():
        return []
    visible = {str(row["name"]).casefold(): str(row["name"]) for row in rows}
    seen: set[str] = set()
    result: list[str] = []
    for raw in source.read_text(encoding="utf-8", errors="replace").splitlines():
        key = raw.strip().casefold()
        if key in visible and key not in seen:
            seen.add(key)
            result.append(visible[key])
    return result


def genre_playlist_outputs(root: Path, rows: list[dict[str, object]]) -> tuple[dict[Path, bytes], dict[str, object]]:
    """Generate per-platform genre playlists only for exact local ROM stems."""
    db = root / "meta.db"
    if not db.is_file():
        return {}, {"playlists": 0, "platforms": 0, "exact_game_genre_matches": 0, "metadata_rows_without_local_rom": 0}

    rom_root = root.parents[1] / "roms"
    system_rows = {str(row["name"]).casefold(): row for row in rows}
    stems_by_system: dict[str, set[str]] = {}
    for folded, row in system_rows.items():
        profile = row["profile"]
        assert isinstance(profile, dict)
        rom_dir = rom_root / str(profile.get("rom_subdir", ""))
        stems_by_system[folded] = {
            item.stem for item in rom_dir.iterdir() if item.is_file()
        } if rom_dir.is_dir() else set()

    matches: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    matched_rows: set[tuple[str, str]] = set()
    metadata_rows_without_local_rom = 0
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for collection, name, genre in connection.execute(
            "SELECT collectionName, name, genre FROM Meta WHERE genre IS NOT NULL AND trim(genre) <> ''"
        ):
            system_key = str(collection or "").casefold()
            row = system_rows.get(system_key)
            if row is None:
                continue
            item_name = str(name or "")
            if item_name not in stems_by_system.get(system_key, set()):
                metadata_rows_without_local_rom += 1
                continue
            # Meta.genre uses semicolons for multiple genres. A slash is
            # retained because labels such as Sports/Soccer are one genre.
            genres = [part.strip() for part in str(genre).split(";") if part.strip()]
            if not genres:
                continue
            system = str(row["name"])
            matched_rows.add((system, item_name))
            for label in genres:
                matches[system][label].add(item_name)
    finally:
        connection.close()

    outputs: dict[Path, bytes] = {}
    platform_counts: dict[str, int] = {}
    for system, genre_groups in sorted(matches.items(), key=lambda pair: pair[0].casefold()):
        path_to_label: dict[str, str] = {}
        for genre, names in sorted(genre_groups.items(), key=lambda pair: pair[0].casefold()):
            component = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", genre)
            component = re.sub(r"\s+", " ", component).strip(" .-") or "Sem nome"
            filename = f"{GENRE_PLAYLIST_PREFIX}{component}.txt"
            folded_filename = filename.casefold()
            previous = path_to_label.setdefault(folded_filename, genre)
            if previous != genre:
                raise ValueError(f"Colisão de nomes em playlists de gênero: {system}: {previous!r} / {genre!r}")
            rel = Path("collections") / system / "playlists" / filename
            outputs[rel] = menu_bytes(sorted(names, key=str.casefold))
        platform_counts[system] = len(genre_groups)

    return outputs, {
        "playlists": len(outputs),
        "platforms": len(platform_counts),
        "playlists_by_platform": platform_counts,
        "exact_game_genre_matches": len(matched_rows),
        "metadata_rows_without_local_rom": metadata_rows_without_local_rom,
        "matching_policy": "Meta.collectionName matches the installed collection and Meta.name exactly matches a local ROM basename; semicolon-delimited genres are split, slash labels are preserved.",
    }


def existing_franchises(root: Path, rows: list[dict[str, object]]) -> dict[str, list[str]]:
    system_names = {str(row["name"]).casefold(): str(row["name"]) for row in rows}
    rom_root = root.parents[1] / "roms"
    names: dict[str, list[str]] = {}
    for folder in sorted((root / "collections").glob("- Collection *"), key=lambda p: p.name.casefold()):
        if not folder.is_dir():
            continue
        valid = False
        for sub in folder.glob("*.sub"):
            platform = system_names.get(sub.stem.casefold())
            lines = [line.strip() for line in sub.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
            if not lines:
                continue
            if platform is None:
                # Unknown subset names cannot be verified against this host's
                # ROM inventory, so keep them out of the generated menu.
                continue
            profile = next((row["profile"] for row in rows if str(row["name"]).casefold() == platform.casefold()), {})
            assert isinstance(profile, dict)
            subdir = str(profile.get("rom_subdir", "")).strip()
            rom_dir = rom_root / subdir
            if not rom_dir.is_dir():
                continue
            stems = {item.stem for item in rom_dir.iterdir() if item.is_file()}
            if any(line in stems for line in lines):
                valid = True
                break
        if valid:
            names[folder.name] = []
    return names


def franchise_display_name(name: str) -> str:
    """Return the player-facing title for a legacy franchise collection."""
    prefix = "- Collection "
    return name[len(prefix):].strip() if name.startswith(prefix) else name


def merged_franchise_rows(
    root: Path,
    rows: list[dict[str, object]],
    curated: dict[str, dict[str, list[str]]],
    legacy: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    """Build friendly .sub collections while leaving legacy game media in place."""
    system_names = {str(row["name"]).casefold(): str(row["name"]) for row in rows}
    rom_root = root.parents[1] / "roms"
    stems_by_system: dict[str, set[str]] = {}
    for row in rows:
        profile = row["profile"]
        assert isinstance(profile, dict)
        rom_dir = rom_root / str(profile.get("rom_subdir", ""))
        if rom_dir.is_dir():
            stems_by_system[str(row["name"]).casefold()] = {
                item.stem for item in rom_dir.iterdir() if item.is_file()
            }
    merged: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for legacy_name in legacy:
        folder = root / "collections" / legacy_name
        display_name = franchise_display_name(legacy_name)
        for sub in sorted(folder.glob("*.sub"), key=lambda path: path.name.casefold()):
            system = system_names.get(sub.stem.casefold())
            if system is None:
                continue
            lines = [
                line.strip()
                for line in sub.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
                and not line.lstrip().startswith("#")
                and line.strip() in stems_by_system.get(system.casefold(), set())
            ]
            merged[display_name][system].update(lines)
    for franchise, by_system in curated.items():
        for system, names in by_system.items():
            merged[franchise][system].update(names)
    return {
        franchise: {
            system: sorted(names, key=str.casefold)
            for system, names in sorted(by_system.items(), key=lambda pair: pair[0].casefold())
            if names
        }
        for franchise, by_system in sorted(merged.items(), key=lambda pair: pair[0].casefold())
        if any(by_system.values())
    }


def curated_franchise_rows(root: Path, rows: list[dict[str, object]]) -> dict[str, dict[str, list[str]]]:
    rom_root = root.parents[1] / "roms"
    system_by_fold = {str(row["name"]).casefold(): row for row in rows}
    stems_by_system: dict[str, set[str]] = {}
    for folded, row in system_by_fold.items():
        profile = row["profile"]
        assert isinstance(profile, dict)
        rom_dir = rom_root / str(profile.get("rom_subdir", ""))
        if rom_dir.is_dir():
            stems_by_system[folded] = {item.stem for item in rom_dir.iterdir() if item.is_file()}

    db = root / "meta.db"
    if not db.is_file():
        return {}
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        result: dict[str, dict[str, list[str]]] = {}
        for franchise, (query, matcher) in FRANCHISE_RULES.items():
            found: dict[str, set[str]] = defaultdict(set)
            for collection, name, title in connection.execute(
                "SELECT collectionName, name, title FROM Meta WHERE lower(title) LIKE ?",
                (f"%{query}%",),
            ):
                if not matcher.search(str(title or "")):
                    continue
                key = str(collection or "").casefold()
                row = system_by_fold.get(key)
                if row is None or str(name or "") not in stems_by_system.get(key, set()):
                    continue
                found[str(row["name"])].add(str(name))
            result[franchise] = {system: sorted(items, key=str.casefold) for system, items in sorted(found.items(), key=lambda p: p[0].casefold())}
        return result
    finally:
        connection.close()


def collection_settings(
    *,
    menu_sort: bool = False,
    include_missing: bool = False,
    launcher: str | None = "Main",
    list_path: str | None = None,
    extensions: str | None = None,
    extra_lines: Iterable[str] = (),
) -> bytes:
    lines = [
        "# Generated by Tools/materializers/materialize-aeon-nox-navigation.py.",
        f"list.menuSort = {'yes' if menu_sort else 'no'}",
        f"list.includeMissingItems = {'yes' if include_missing else 'no'}",
    ]
    if list_path:
        lines.append(f"list.path = {list_path}")
    if extensions:
        lines.append(f"list.extensions = {extensions}")
    if launcher:
        lines.append(f"launcher = {launcher}")
    lines.extend(extra_lines)
    return text_bytes("\n".join(lines))


# Keys written into the same settings.conf by other official tools (runtime media projection and ROM
# hierarchy). Regenerating a favorite keeps them instead of treating the file as foreign content.
FOREIGN_OWNED_KEYS = ("media.", "list.romHierarchy")


def foreign_owned_lines(path: Path) -> list[str]:
    if not path.is_file() or path.is_symlink():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key = line.split("=", 1)[0].strip()
        if "=" in line and key.startswith(FOREIGN_OWNED_KEYS):
            lines.append(line.rstrip())
    return lines


def favorite_collection_settings(root: Path, source_name: str, leaf: str | None = None) -> bytes:
    source_info = read_info(root / "collections" / source_name / "settings.conf")
    list_path = source_info.get("list.path", "").strip()
    extensions = source_info.get("list.extensions", "").strip()
    if not list_path or list_path.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", list_path):
        raise ValueError(f"A origem de favoritos não tem list.path portátil: {source_name}")
    if ".." in Path(list_path.replace("\\", "/")).parts:
        raise ValueError(f"list.path contém travessia de diretório: {source_name} -> {list_path}")
    return collection_settings(
        menu_sort=False,
        include_missing=False,
        # A favorite leaf may merge games from a collection that RetroFE
        # identifies by a different source system. The host dispatcher uses
        # the selected item's canonical ROM path to choose that system.
        launcher="Host Command",
        list_path=list_path,
        extensions=extensions,
        extra_lines=foreign_owned_lines(root / "collections" / leaf / "settings.conf") if leaf else (),
    )


def menu_bytes(entries: Iterable[str]) -> bytes:
    return text_bytes("\n".join(entries))


def parse_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"version": STATE_VERSION, "managed_hashes": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != STATE_VERSION or not isinstance(value.get("managed_hashes"), dict):
            raise ValueError("versão ou formato desconhecido")
        return value
    except Exception as exc:
        raise ValueError(f"Manifesto de estado inválido ({path}): {exc}") from exc


def add_managed(outputs: dict[Path, bytes], root: Path, rel: str, data: bytes) -> None:
    rel_path = Path(rel)
    if rel_path.is_absolute() or any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError(f"Caminho relativo inseguro: {rel}")
    for part in rel_path.parts:
        if part not in {"collections", "Manifests", "launchers.linux", "Tools"}:
            safe_name(part)
    target = root / rel_path
    if target.is_symlink():
        raise ValueError(f"Destino gerado é symlink e exige revisão: {rel}")
    outputs[rel_path] = data


def add_collection_files(outputs: dict[Path, bytes], root: Path, collection: str, entries: list[str] | None) -> None:
    collection = safe_name(collection)
    folder = f"collections/{collection}"
    settings = root / folder / "settings.conf"
    if settings.is_symlink():
        raise ValueError(f"settings.conf é symlink e exige revisão: {settings}")
    if not settings.exists():
        add_managed(outputs, root, f"{folder}/settings.conf", collection_settings(menu_sort=False))
    if entries is not None:
        add_managed(outputs, root, f"{folder}/menu.txt", menu_bytes(entries))


def build_outputs(root: Path, favorites_only: bool) -> tuple[dict[Path, bytes], dict[str, object], list[tuple[Path, bytes]]]:
    rows = collection_rows(root)
    if not rows:
        raise ValueError("Config/systems.json não possui coleções visíveis instaladas")
    groups = metadata_groups(rows)
    genre_outputs, genre_summary = genre_playlist_outputs(root, rows)
    curated = curated_franchise_rows(root, rows)
    legacy_franchises = existing_franchises(root, rows)
    franchise_sources = merged_franchise_rows(root, rows, curated, legacy_franchises)
    outputs: dict[Path, bytes] = {}
    append_actions: list[tuple[Path, bytes | None]] = []
    for rel, data in genre_outputs.items():
        add_managed(outputs, root, rel.as_posix(), data)
    for franchise, by_system in franchise_sources.items():
        collection = franchise
        if not by_system:
            continue
        add_managed(outputs, root, f"collections/{collection}/settings.conf", collection_settings(launcher="Host Command"))
        for system, names in by_system.items():
            if names:
                sub_rel = Path(f"collections/{collection}/{system}.sub")
                add_managed(outputs, root, str(sub_rel), menu_bytes(names))

    # Legacy folders retain game artwork and videos. The player-facing
    # collections contain generated settings/.sub files that import games
    # from their source platforms; no ROM or game-media payload is copied,
    # moved, or renamed.
    franchise_names = sorted(franchise_sources, key=str.casefold)

    facets = {
        "manufacturer": "Marcas",
        "type": "Tipo de plataforma",
        "year": "Ano",
        "bits": "Bits",
    }
    category_root_entries: list[str] = []
    platform_browse_entries: list[str] = []
    for facet, parent in facets.items():
        values = groups[facet]
        if not values:
            continue
        platform_browse_entries.append(parent)
        child_names = []
        for label, systems in values.items():
            if facet == "manufacturer" and label == "Other (multiple)":
                child = "Outras marcas"
            elif facet == "type":
                child = TYPE_DISPLAY_LABELS.get(label, label)
            elif facet == "bits":
                child = label.replace("-bit", " bits")
            else:
                child = label
            child_names.append(child)
            add_collection_files(outputs, root, child, systems)
        add_collection_files(outputs, root, parent, child_names)

    if platform_browse_entries:
        category_root_entries.append(BROWSE_PLATFORMS_COLLECTION)
        add_collection_files(outputs, root, BROWSE_PLATFORMS_COLLECTION, platform_browse_entries)

    recent_entries = recent_platform_names(root, rows)
    if recent_entries:
        category_root_entries.append(RECENT_PLATFORMS_COLLECTION)
        add_collection_files(outputs, root, RECENT_PLATFORMS_COLLECTION, recent_entries)

    # Favorites opens platform-specific favorite game collections directly.
    # Skipping the old "Por plataforma" wrapper removes one redundant stop
    # while preserving the same game carousel and normal Back behavior.
    favorites_collection = "Favoritos"
    favorites_by_platform = "Por plataforma"
    favorite_entries: list[str] = []
    favorite_source_names: set[str] = set()
    favorite_source_count = 0
    for source_folder in sorted((root / "collections").iterdir(), key=lambda p: p.name.casefold()):
        if not source_folder.is_dir() or source_folder.name in {"Main", MENU_COLLECTIONS, *OLD_MENU_COLLECTION_NAMES}:
            continue
        if source_folder.name.startswith(("- Browse ", "- Manufacturer ", "- Platform ", "- Favorite Games ", "Favoritos ")) or source_folder.name in {
            "- Franchise Collections", FRANCHISE_COLLECTIONS,
            "- Favorites", favorites_collection, "- Favorites by Platform", favorites_by_platform,
            "- Browse by Manufacturer", "- Browse by Platform Type", "- Browse by Year", "- Browse by Bits",
            BROWSE_PLATFORMS_COLLECTION, RECENT_PLATFORMS_COLLECTION,
            "Marcas", "Tipo de plataforma", "Ano", "Bits",
            "- Options", OPTIONS_COLLECTION,
            "- Display Options", DISPLAY_OPTIONS_COLLECTION,
            "- Theme Options", THEME_OPTIONS_COLLECTION,
            "- Controller Options", CONTROLLER_OPTIONS_COLLECTION,
        }:
            continue
        name = source_folder.name
        source = source_folder / "playlists/favorites.txt"
        if not source.is_file():
            continue
        lines = [line.strip() for line in source.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines:
            leaf = safe_name(f"Favoritos {name}")
            favorite_entries.append(leaf)
            favorite_source_names.add(name)
            favorite_source_count += len(lines)
            add_managed(outputs, root, f"collections/{leaf}/settings.conf", favorite_collection_settings(root, name, leaf))
            add_managed(outputs, root, f"collections/{leaf}/{name}.sub", menu_bytes(dict.fromkeys(lines)))
    if favorite_entries:
        add_collection_files(outputs, root, favorites_collection, sorted(favorite_entries, key=str.casefold))

    # The Options collection launches a small preference writer. The chosen
    # profile is applied by the portable frontend launcher on the next start.
    option_collection = DISPLAY_OPTIONS_COLLECTION
    profile_module = root / "Tools/launchers/retrofe/display-profile.py"
    display_profiles = runpy.run_path(str(profile_module)).get("PROFILES")
    if not isinstance(display_profiles, dict) or not display_profiles:
        raise ValueError(f"Perfis de tela ausentes ou inválidos: {profile_module}")
    option_labels = list(display_profiles)
    # include.txt by itself creates visible virtual entries, but RetroFE cannot
    # invoke a launcher for a missing item. Tiny .profile descriptors under
    # Tools provide real, launchable actions without putting files in roms/.
    profile_list_path = f"%BASE_ITEM_PATH%/../frontend/RetroFE/{DISPLAY_PROFILE_DIR.as_posix()}"
    add_managed(outputs, root, f"collections/{option_collection}/settings.conf", collection_settings(
        include_missing=False,
        launcher="Aeon Nox Display Profile",
        list_path=profile_list_path,
        extensions="profile",
    ))
    add_managed(outputs, root, f"collections/{option_collection}/include.txt", menu_bytes(option_labels))
    for label, profile in display_profiles.items():
        profile_id = str(profile.get("id", "")).strip()
        if not profile_id:
            raise ValueError(f"Perfil de tela sem id: {label}")
        marker = {
            "action": "set-display-profile",
            "profile_id": profile_id,
            "profile_label": label,
        }
        add_managed(outputs, root, f"{DISPLAY_PROFILE_DIR}/{safe_name(label)}.profile", text_bytes(json.dumps(marker, ensure_ascii=False, sort_keys=True)))
    add_managed(outputs, root, "launchers.linux/Aeon Nox Display Profile.conf", text_bytes(
        "executable = python3\n"
        "arguments = \"%RETROFE_PATH%/Tools/launchers/retrofe/display-profile.py\" --root \"%RETROFE_PATH%\" set \"%ITEM_NAME%\"\n"
        "currentDirectory = %RETROFE_PATH%\n"
    ))

    theme_module = root / "Tools/launchers/retrofe/theme-profile.py"
    theme_functions = runpy.run_path(str(theme_module))
    _, themes = theme_functions["load_registry"](root)
    theme_labels: list[str] = []
    for theme_id, theme in themes.items():
        theme_functions["safe_theme"](root, theme)
        label = str(theme["label"])
        theme_labels.append(label)
        marker = {"action": "queue-theme", "theme_id": theme_id, "theme_label": label}
        add_managed(outputs, root, f"{THEME_PROFILE_DIR}/{safe_name(label)}.theme", text_bytes(json.dumps(marker, ensure_ascii=False, sort_keys=True)))
    add_managed(outputs, root, f"collections/{THEME_OPTIONS_COLLECTION}/settings.conf", collection_settings(
        include_missing=False,
        launcher="RetroFE Theme Profile",
        list_path=f"%BASE_ITEM_PATH%/../frontend/RetroFE/{THEME_PROFILE_DIR.as_posix()}",
        extensions="theme",
    ))
    add_managed(outputs, root, f"collections/{THEME_OPTIONS_COLLECTION}/include.txt", menu_bytes(theme_labels))
    add_managed(outputs, root, "launchers.linux/RetroFE Theme Profile.conf", text_bytes(
        "executable = python3\n"
        "arguments = \"%RETROFE_PATH%/Tools/launchers/retrofe/theme-profile.py\" --root \"%RETROFE_PATH%\" request \"%ITEM_NAME%\"\n"
        "currentDirectory = %RETROFE_PATH%\n"
    ))

    controller_labels = ["Steam Deck - Desktop e Gamepad"]
    controller_action = {
        "action": "open-steam-deck-controller-config",
        "app_id": "413080",
        "label": controller_labels[0],
    }
    add_managed(outputs, root, f"{CONTROLLER_PROFILE_DIR}/Steam Deck - Desktop e Gamepad.controller", text_bytes(json.dumps(controller_action, ensure_ascii=False, sort_keys=True)))
    add_managed(outputs, root, f"collections/{CONTROLLER_OPTIONS_COLLECTION}/settings.conf", collection_settings(
        include_missing=False,
        launcher="RetroFE Steam Deck Controls",
        list_path=f"%BASE_ITEM_PATH%/../frontend/RetroFE/{CONTROLLER_PROFILE_DIR.as_posix()}",
        extensions="controller",
    ))
    add_managed(outputs, root, f"collections/{CONTROLLER_OPTIONS_COLLECTION}/include.txt", menu_bytes(controller_labels))
    add_managed(outputs, root, "launchers.linux/RetroFE Steam Deck Controls.conf", text_bytes(
        "executable = python3\n"
        "arguments = \"%RETROFE_PATH%/Tools/launchers/retrofe/steam-input.py\" --root \"%RETROFE_PATH%\" open-desktop\n"
        "currentDirectory = %RETROFE_PATH%\n"
    ))

    # Keep display, theme, and controller actions behind one B1 choice.
    add_collection_files(outputs, root, OPTIONS_COLLECTION, [
        DISPLAY_OPTIONS_COLLECTION,
        THEME_OPTIONS_COLLECTION,
        CONTROLLER_OPTIONS_COLLECTION,
    ])

    if franchise_names:
        category_root_entries.append(FRANCHISE_COLLECTIONS)
    if favorite_entries:
        category_root_entries.append(favorites_collection)
    category_root_entries.append(OPTIONS_COLLECTION)
    # B1 is a short discovery hub. Metadata facets sit behind one explicit
    # Explore entry; Recentes contains platforms from RetroFE's own
    # lastplayed collection history, not a fabricated list of recent games.
    desired_order = [
        favorites_collection,
        RECENT_PLATFORMS_COLLECTION,
        BROWSE_PLATFORMS_COLLECTION,
        FRANCHISE_COLLECTIONS,
        OPTIONS_COLLECTION,
    ]
    root_menu = [item for item in desired_order if item in category_root_entries]
    add_managed(outputs, root, f"collections/{MENU_COLLECTIONS}/menu.txt", menu_bytes(root_menu))
    # Keep B1 self-contained: it is a menu-only collection and must not rely
    # on global ROM-list defaults when the project is relocated.
    add_managed(outputs, root, f"collections/{MENU_COLLECTIONS}/settings.conf", collection_settings())
    if franchise_names:
        if not (root / f"collections/{FRANCHISE_COLLECTIONS}/settings.conf").exists():
            add_managed(outputs, root, f"collections/{FRANCHISE_COLLECTIONS}/settings.conf", collection_settings())
        # The franchise browser itself is B2. Link directly to each verified
        # game collection so B3 is the actual game list, with no empty wrapper.
        add_managed(outputs, root, f"collections/{FRANCHISE_COLLECTIONS}/menu.txt", menu_bytes(franchise_names))

    # Keep B1 in Main, immediately after the first platform so it is visible
    # without burying it among dozens of systems. Do not inject it into game
    # collections: RetroFE places menu.txt entries before ROMs, which would
    # make B1 the initial selection instead of the first game. Child menus
    # use RetroFE's Back control to return to their caller.
    candidates = {"Main"}
    add_menu_link(root, "Main", append_actions, outputs)
    add_main_platforms(root, rows, append_actions)
    for folder in (root / "collections").iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.name in {"Main", MENU_COLLECTIONS, LEGACY_MENU_COLLECTIONS}:
            continue
        remove_menu_link(root, folder.name, append_actions, outputs)

    if favorites_only:
        outputs = {
            rel: data for rel, data in outputs.items()
            if rel in {
                Path(f"collections/{MENU_COLLECTIONS}/menu.txt"),
                Path(f"collections/{MENU_COLLECTIONS}/settings.conf"),
                Path(f"collections/{RECENT_PLATFORMS_COLLECTION}/menu.txt"),
                Path(f"collections/{RECENT_PLATFORMS_COLLECTION}/settings.conf"),
                Path(f"collections/{OPTIONS_COLLECTION}/settings.conf"),
                Path(f"collections/{OPTIONS_COLLECTION}/menu.txt"),
                Path(f"collections/{DISPLAY_OPTIONS_COLLECTION}/settings.conf"),
                Path(f"collections/{DISPLAY_OPTIONS_COLLECTION}/include.txt"),
                Path(f"collections/{THEME_OPTIONS_COLLECTION}/settings.conf"),
                Path(f"collections/{THEME_OPTIONS_COLLECTION}/include.txt"),
                Path(f"collections/{CONTROLLER_OPTIONS_COLLECTION}/settings.conf"),
                Path(f"collections/{CONTROLLER_OPTIONS_COLLECTION}/include.txt"),
                Path("launchers.linux/RetroFE Theme Profile.conf"),
                Path("launchers.linux/RetroFE Steam Deck Controls.conf"),
            }
            or rel.parts[:len(DISPLAY_PROFILE_DIR.parts)] == DISPLAY_PROFILE_DIR.parts
            or rel.parts[:len(THEME_PROFILE_DIR.parts)] == THEME_PROFILE_DIR.parts
            or rel.parts[:len(CONTROLLER_PROFILE_DIR.parts)] == CONTROLLER_PROFILE_DIR.parts
            or rel.parts[:2] in {
                ("collections", "- Favorites"),
                ("collections", "- Favorites by Platform"),
                ("collections", "Favoritos"),
                ("collections", "Por plataforma"),
                ("collections", RECENT_PLATFORMS_COLLECTION),
            }
            or (len(rel.parts) > 1 and rel.parts[0] == "collections" and rel.parts[1].startswith("- Favorite Games "))
            or (len(rel.parts) > 1 and rel.parts[0] == "collections" and rel.parts[1].startswith("Favoritos "))
            or (
                len(rel.parts) == 4
                and rel.parts[0] == "collections"
                and rel.parts[2] == "playlists"
                and rel.parts[3].casefold().startswith(GENRE_PLAYLIST_PREFIX.casefold())
            )
        }
        # The launcher's partial refresh updates favorite/recent entries and
        # the B1 menu; unrelated category generation remains reserved for a
        # full materialization.
        main_menu = root / "collections/Main/menu.txt"
        append_actions = [(path, data) for path, data in append_actions if path == main_menu]

    visible_legacy_aliases = {franchise_display_name(name) for name in legacy_franchises}
    summary = {
        "visible_platforms": len(rows),
        "manufacturer_groups": len(groups["manufacturer"]),
        "platform_type_groups": len(groups["type"]),
        "release_year_groups": len(groups["year"]),
        "bit_groups": len(groups["bits"]),
        "platforms_by_facet": {facet: sum(len(items) for items in values.values()) for facet, values in groups.items()},
        "manufacturer_aliases": MANUFACTURER_ALIASES,
        "platform_type_overrides": TYPE_OVERRIDES,
        "favorite_platforms": len(favorite_entries),
        "favorite_items": favorite_source_count,
        "recent_platforms": len(recent_entries),
        "recent_platform_source": "collections/Main/playlists/lastplayed.txt",
        "platform_browse_facets": platform_browse_entries,
        "genre_playlists": genre_summary,
        "display_profiles": len(option_labels),
        "display_profile_markers": len(option_labels),
        "theme_profiles": len(theme_labels),
        "theme_profile_markers": len(theme_labels),
        "controller_actions": len(controller_labels),
        "b1_link_targets": len(candidates),
        "b1_entry": {"collection": MENU_COLLECTIONS, "main_menu_position": 2, "main_menu_only": True},
        "franchise_collections_visible": len(franchise_names),
        "franchise_display_aliases": {
            name: franchise_display_name(name)
            for name in sorted(legacy_franchises, key=str.casefold)
        },
        "franchise_sources_by_platform": {
            name: {system: len(items) for system, items in by_system.items()}
            for name, by_system in franchise_sources.items()
        },
        "prompt_franchise_matches": {name: {system: len(items) for system, items in by_system.items()} for name, by_system in curated.items()},
        "franchise_collections_omitted_empty_or_unverified": sorted(
            p.name for p in (root / "collections").glob("- Collection *")
            if p.is_dir() and franchise_display_name(p.name) not in visible_legacy_aliases
        ),
    }
    return outputs, summary, append_actions


def add_menu_link(
    root: Path,
    collection: str,
    append_actions: list[tuple[Path, bytes | None]],
    outputs: dict[Path, bytes] | None = None,
) -> None:
    folder = root / "collections" / safe_name(collection)
    menu_rel = Path("collections") / collection / "menu.txt"
    if outputs is not None and menu_rel in outputs:
        current = outputs[menu_rel].decode("utf-8")
        lines = [MENU_COLLECTIONS if line.strip() in OLD_MENU_COLLECTION_NAMES else line for line in current.splitlines()]
        if collection == "Main":
            lines = [line for line in lines if line.strip() != MENU_COLLECTIONS]
            first_item = next((index for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), None)
            lines.insert(first_item + 1 if first_item is not None else 0, MENU_COLLECTIONS)
        elif MENU_COLLECTIONS not in {line.strip() for line in lines}:
            lines.append(MENU_COLLECTIONS)
        updated = "\n".join(lines) + ("\n" if current.endswith("\n") or lines else "")
        if updated != current:
            outputs[menu_rel] = updated.encode("utf-8")
        return
    menu_file = folder / "menu.txt"
    if menu_file.is_file():
        try:
            current = menu_file.read_bytes()
            text = current.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"menu.txt não é UTF-8 e exige revisão: {menu_file}") from exc
        lines = [MENU_COLLECTIONS if line.strip() in OLD_MENU_COLLECTION_NAMES else line for line in text.splitlines()]
        if collection == "Main":
            lines = [line for line in lines if line.strip() != MENU_COLLECTIONS]
            first_item = next((index for index, line in enumerate(lines) if line.strip() and not line.lstrip().startswith("#")), None)
            lines.insert(first_item + 1 if first_item is not None else 0, MENU_COLLECTIONS)
        elif MENU_COLLECTIONS not in {line.strip() for line in lines}:
            lines.append(MENU_COLLECTIONS)
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") or lines else "")
        if updated != text:
            append_actions.append((menu_file, updated.encode("utf-8")))
        return
    for label in sorted(OLD_MENU_COLLECTION_NAMES):
        legacy_marker = folder / "menu" / f"{label}.txt"
        if legacy_marker.exists() or legacy_marker.is_symlink():
            if legacy_marker.is_symlink() or not legacy_marker.is_file() or legacy_marker.stat().st_size != 0:
                raise ValueError(f"Marcador antigo do menu não vazio ou não regular: {legacy_marker}")
            append_actions.append((legacy_marker, None))
    marker = folder / "menu" / f"{MENU_COLLECTIONS}.txt"
    if marker.exists():
        if marker.is_file() and marker.stat().st_size == 0:
            return
        raise ValueError(f"Marcador de menu existente não vazio: {marker}")
    append_actions.append((marker, b""))


def remove_menu_link(
    root: Path,
    collection: str,
    append_actions: list[tuple[Path, bytes | None]],
    outputs: dict[Path, bytes] | None = None,
) -> None:
    folder = root / "collections" / safe_name(collection)
    menu_rel = Path("collections") / collection / "menu.txt"
    if outputs is not None and menu_rel in outputs:
        current = outputs[menu_rel].decode("utf-8")
        lines = [
            line for line in current.splitlines()
            if line.strip() not in {MENU_COLLECTIONS, *OLD_MENU_COLLECTION_NAMES}
        ]
        updated = "\n".join(lines) + ("\n" if current.endswith("\n") or lines else "")
        outputs[menu_rel] = updated.encode("utf-8")
        return
    menu_file = folder / "menu.txt"
    if menu_file.is_file():
        try:
            current = menu_file.read_bytes()
            text = current.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"menu.txt não é UTF-8 e exige revisão: {menu_file}") from exc
        lines = [
            line for line in text.splitlines()
            if line.strip() not in {MENU_COLLECTIONS, *OLD_MENU_COLLECTION_NAMES}
        ]
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") or lines else "")
        if updated != text:
            append_actions.append((menu_file, updated.encode("utf-8")))
    for label in (MENU_COLLECTIONS, *sorted(OLD_MENU_COLLECTION_NAMES)):
        marker = folder / "menu" / f"{label}.txt"
        if marker.exists() or marker.is_symlink():
            append_actions.append((marker, None))


def is_retirable_generated_collection(name: str) -> bool:
    # The retirement loop considers only individual files in managed_hashes.
    # This also covers new unprefixed labels without touching untracked files
    # or media that happen to share a collection directory.
    return name != "Main"


def is_favorite_generated_collection(name: str) -> bool:
    return (
        name in {"- Favorites", "- Favorites by Platform", "Favoritos", "Por plataforma"}
        or name.startswith(("- Favorite Games ", "Favoritos "))
    )


def is_runtime_refreshed_collection(name: str) -> bool:
    return is_favorite_generated_collection(name) or name == RECENT_PLATFORMS_COLLECTION


def is_generated_genre_playlist(rel: Path) -> bool:
    return (
        len(rel.parts) == 4
        and rel.parts[0] == "collections"
        and rel.parts[2] == "playlists"
        and rel.parts[3].casefold().startswith(GENRE_PLAYLIST_PREFIX.casefold())
    )


def plan(
    root: Path,
    outputs: dict[Path, bytes],
    summary: dict[str, object],
    append_actions: list[tuple[Path, bytes | None]],
    favorites_only: bool = False,
) -> dict[str, object]:
    state_path = root / STATE_REL
    state = parse_state(state_path)
    old_hashes = dict(state.get("managed_hashes", {}))
    changes = []
    conflicts = []
    next_hashes = dict(old_hashes)
    active_generated_collections = {
        rel.parts[1]
        for rel in outputs
        if len(rel.parts) > 2 and rel.parts[0] == "collections"
    }
    # Retire generated category files no longer represented by current source
    # metadata (including wrappers from older revisions), but only when each
    # file still matches the exact hash recorded by our own state.
    for rel_text, managed_hash in sorted(old_hashes.items()):
        rel = Path(rel_text)
        if len(rel.parts) < 3 or rel.parts[0] != "collections" or not is_retirable_generated_collection(rel.parts[1]):
            continue
        # A launcher refresh may update favorite/recent lists and the B1 root menu,
        # but must retain category collections created by the full materializer.
        if favorites_only and not (is_runtime_refreshed_collection(rel.parts[1]) or is_generated_genre_playlist(rel)):
            continue
        if rel.parts[1] in active_generated_collections and not is_generated_genre_playlist(rel):
            continue
        if rel in outputs:
            continue
        target = root / rel
        next_hashes.pop(rel_text, None)
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_file():
            conflicts.append({"path": rel_text, "reason": "retired generated path is not a regular file; preserved"})
            continue
        before = target.read_bytes()
        before_hash = sha256(before)
        if before_hash != managed_hash:
            conflicts.append({"path": rel_text, "reason": "retired generated file changed since materialization; preserved"})
            continue
        changes.append({
            "path": rel_text,
            "action": "remove-managed",
            "sha256_before": before_hash,
            "sha256_after": None,
            "size_before": len(before),
        })
    for rel, data in sorted(outputs.items(), key=lambda item: str(item[0]).casefold()):
        target = root / rel
        if target.exists() and not target.is_file() and not target.is_symlink():
            conflicts.append({"path": str(rel), "reason": "destination exists and is not a regular file"})
            continue
        before = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        before_hash = sha256(before) if before is not None else None
        after_hash = sha256(data)
        managed_hash = old_hashes.get(str(rel))
        if target.is_symlink():
            conflicts.append({"path": str(rel), "reason": "symlink destination"})
            continue
        if before is not None and before_hash != after_hash and managed_hash != before_hash:
            conflicts.append({"path": str(rel), "reason": "existing unmanaged content"})
            continue
        action = "create" if before is None else ("unchanged" if before_hash == after_hash else "update-managed")
        changes.append({"path": str(rel), "action": action, "sha256_before": before_hash, "sha256_after": after_hash, "size_after": len(data)})
        next_hashes[str(rel)] = after_hash

    append_changes = []
    for target, data in append_actions:
        rel = target.relative_to(root)
        if data is None:
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_file() or target.stat().st_size != 0:
                conflicts.append({"path": str(rel), "reason": "legacy menu marker is not an empty regular file"})
                continue
            before = target.read_bytes()
            append_changes.append({"path": str(rel), "action": "remove-menu-marker", "sha256_before": sha256(before), "sha256_after": None, "size_before": len(before)})
            continue
        before = target.read_bytes() if target.is_file() and not target.is_symlink() else None
        if target.is_symlink() or (before is not None and target.stat().st_size != 0 and target.name == f"{MENU_COLLECTIONS}.txt" and target.suffix == ".txt" and target.parent.name == "menu"):
            conflicts.append({"path": str(rel), "reason": "existing menu marker is not an empty regular file"})
            continue
        if before == data:
            action = "unchanged"
        else:
            action = "update-menu-file" if target.name == "menu.txt" else "create-menu-marker"
        append_changes.append({"path": str(rel), "action": action, "sha256_before": sha256(before) if before is not None else None, "sha256_after": sha256(data), "size_after": len(data)})

    state_payload = {
        "version": STATE_VERSION,
        "description": "Hashes of generated RetroFE navigation files; source menus remain additive.",
        "managed_hashes": next_hashes,
    }
    state_data = (json.dumps(state_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    state_before = state_path.read_bytes() if state_path.is_file() else None
    changes.append({
        "path": str(STATE_REL),
        "action": "create" if state_before is None else ("unchanged" if state_before == state_data else "update-managed-state"),
        "sha256_before": sha256(state_before) if state_before is not None else None,
        "sha256_after": sha256(state_data),
        "size_after": len(state_data),
    })
    return {
        "status": "conflict" if conflicts else "plan",
        "summary": summary,
        "changes": changes,
        "menu_links": append_changes,
        "conflicts": conflicts,
        "generated_state": state_payload,
        "generated_state_bytes": state_data,
    }


def apply_plan(root: Path, result: dict[str, object], outputs: dict[Path, bytes], append_actions: list[tuple[Path, bytes | None]]) -> None:
    if result["conflicts"]:
        raise ValueError("Há conflitos de destino; nenhum arquivo foi alterado")
    state_path = root / STATE_REL
    state_bytes = result["generated_state_bytes"]
    assert isinstance(state_bytes, bytes)
    for change in result["changes"]:
        if change.get("action") != "remove-managed":
            continue
        target = root / str(change["path"])
        if target.is_symlink() or not target.is_file() or sha256(target.read_bytes()) != change["sha256_before"]:
            raise ValueError(f"Arquivo gerenciado mudou antes da remoção; preservado: {target}")
        target.unlink()
        try:
            target.parent.rmdir()
            target.parent.parent.rmdir()
        except OSError:
            pass
    for target, data in append_actions:
        if data is None:
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.stat().st_size != 0:
                    raise ValueError(f"Marcador antigo mudou antes da remoção; preservado: {target}")
                target.unlink()
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.name == "menu.txt" and target.is_file():
            if target.read_bytes() != data:
                target.write_bytes(data)
        elif not target.exists():
            target.write_bytes(data)
    for rel, data in sorted(outputs.items(), key=lambda item: str(item[0]).casefold()):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_bytes() != data:
            target.write_bytes(data)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    if not state_path.is_file() or state_path.read_bytes() != state_bytes:
        state_path.write_bytes(state_bytes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="raiz RetroFE; padrão relativo à ferramenta")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="aplica o plano sem sobrescrever conteúdo desconhecido")
    mode.add_argument("--check", action="store_true", help="retorna 1 se a materialização estiver desatualizada")
    parser.add_argument("--favorites-only", action="store_true", help="atualiza somente o agregado de favoritos")
    parser.add_argument("--quiet", action="store_true", help="suprime o JSON de plano e a confirmação")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        outputs, summary, append_actions = build_outputs(root, args.favorites_only)
        result = plan(root, outputs, summary, append_actions, args.favorites_only)
        public = {key: value for key, value in result.items() if key not in {"generated_state", "generated_state_bytes"}}
        if not args.quiet:
            print(json.dumps(public, ensure_ascii=False, indent=2, sort_keys=True))
        if result["conflicts"]:
            return 2
        pending = any(change["action"] != "unchanged" for change in result["changes"])
        pending = pending or any(change["action"] != "unchanged" for change in result["menu_links"])
        if args.check:
            return 1 if pending else 0
        if args.apply:
            apply_plan(root, result, outputs, append_actions)
            if not args.quiet:
                print("APPLY_RESULT=written")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
