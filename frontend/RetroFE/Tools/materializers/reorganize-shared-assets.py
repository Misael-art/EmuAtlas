#!/usr/bin/env python3
"""Legacy hash migration helpers and rollback for historical operations.

The materializer keeps one regular file per SHA-256 under Assets/shared,
records relative symlinks for runtime artwork paths, and centralizes exact
duplicates of static assets used by multiple layouts. Its old plan/apply
commands create hash-oriented paths and are disabled for new work. The
semantic organizer is Tools/materializers/organize-asset-library.py.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sys
import unicodedata
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def find_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "layouts").is_dir() and (candidate / "Manifests").is_dir():
            return candidate
    raise RuntimeError("Não encontrei a raiz do RetroFE acima do materializador.")


ROOT = find_root()
LAYOUTS = ROOT / "layouts"
SHARED = ROOT / "Assets" / "shared"
HISTORICAL_MANIFEST = ROOT / "Manifests" / "asset-sharing.json"
CATALOG_PATH = ROOT / "Manifests" / "asset-catalog.json"
STATIC_ROOT = SHARED / "images"
MAIN_ARTWORK_ROOT = ROOT / "collections" / "Main" / "medium_artwork"
SOURCE_ARTWORK_ROOT = ROOT / "Assets" / "source-artwork"
SOURCE_ARTWORK_INDEX = ROOT / "Manifests" / "source-artwork-sharing.csv"
COMMENT_RE = re.compile(r"(<!--.*?-->)", re.DOTALL)
ATTRIBUTE_RE = re.compile(r"\b(src|font)\s*=(\"|')([^\"']*)\2", re.IGNORECASE)
CHUNK_BYTES = 1024 * 1024
MAIN_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp",
    ".mp4", ".mp3", ".ogg", ".wav", ".flac", ".m4a",
}

# `Main` contains system/collection artwork. Its runtime lookup names stay in
# collections/Main/medium_artwork; only exact duplicate payloads are aliased.
# Templates under `_Template` and hidden design masters are not runtime media.
MAIN_TYPE_SEMANTICS = {
    "artwork_front": "platform-artwork/front",
    "background": "backgrounds",
    "background_abstract": "backgrounds/abstract",
    "bezel_frame": "bezel-frames",
    "character": "characters",
    "character_digital cgi": "characters/digital-cgi",
    "character_lowpoly": "characters/low-poly",
    "color": "colors",
    "controller": "controller/platforms",
    "controller_art": "controller/artwork",
    "controller_clean": "controller/clean",
    "device": "devices",
    "device_clean": "devices/clean",
    "device_cleanfullsetup": "devices/full-setups/clean",
    "device_fullsetup": "devices/full-setups",
    "device_icon": "devices/icons",
    "fanart_aeon Nox": "fanart/aeon-nox",
    "fanart_alekfull": "fanart/alekfull",
    "fanart_alekfullblack": "fanart/alekfull/black",
    "fanart_coverart": "fanart/fan-art",
    "fanart_device": "fanart/devices",
    "fanart_gameart": "fanart/game-art",
    "fanart_gaussiano": "fanart/gaussian",
    "fanart_picturemovie": "fanart/picture-movie",
    "fanart_tecnical": "fanart/technical",
    "fanart_wallpaper": "fanart/wallpaper",
    "fanartleft": "fanart/left",
    "fanartsys": "fanart/systems",
    "infobar": "ui/infobars",
    "logo": "branding/logos",
    "logo_clean": "branding/logos/clean",
    "logo_fullset": "branding/logos/full-set",
    # Aeon Nox uses registered Main aliases to flatten the nested full-set
    # style/class folders into one imageType lookup without copying payloads.
    "logo_aeon_nox_light_white": "branding/logos/full-set",
    "logo_aeon_nox_white_outline": "branding/logos/full-set",
    "medium_clean": "platform-artwork/medium/clean",
    "medium_cleangrey": "platform-artwork/medium/clean-grey",
    "medium_cleaninverse": "platform-artwork/medium/clean-inverse",
    "medium_front": "platform-artwork/medium/front",
    "medium_modern": "platform-artwork/medium/modern",
    "medium_moderncolor": "platform-artwork/medium/modern-color",
    "medium_modernident": "platform-artwork/medium/modern-ident",
    "medium_pixel": "platform-artwork/medium/pixel",
    "music": "music",
    "overlay_system": "overlays/system",
    "pointer": "ui/pointers",
    "screenbios": "system-screens/bios",
    "screenshot": "screenshots",
    "select": "ui/selection",
    "video": "video",
}


# Each static image is classified by the meaning visible in its file name and
# the supplied contact sheets. Unknown names stop planning for explicit review.
STATIC_CATEGORIES: dict[str, str] = {}


def add_static(category: str, names: str) -> None:
    for name in names.split():
        STATIC_CATEGORIES[name] = category


add_static("controller/prompts/buttons", "a ar b bb button-back button-enter modern-slct modern-slct-black modern-slct-grey modern-unslct modern-unslct-black modern-unslct-grey")
add_static("controller/prompts/directions", "buttonanim d-pad-h d-pad-h-b d-pad-v")
add_static("controller/prompts/guides", "help")
add_static("images/backgrounds", "arcade attract background background-2 basic dark1 dark2 dark3 intro intro-orig main-vidbg mode-color mode-color-blue mode-color-p-b mode-dark mode-soft promo promo3 splash-alt vidbg videobacka")
add_static("images/branding/logos", "intrologo pinlogo promofront promofrontgem")
add_static("images/branding/promotions", "promohide")
add_static("images/effects", "beamhalf beampurple blur cabshadow cabshadow-full colorful-platform-1-dropshadow colorful-platform-vignetting fire floor1 game-shadow-degrade haze initial-shade pulse ray reflection scanline scanline-a scanline-b scanline-colorful scanline-realistic-marquee-fx-subtle-only-plexi scanlines-d scanlines-f shadow-bottom shadowoverlay vignette vignette2 vignette3 vignette-vidro white-side-effect")
add_static("images/frames", "bezel bezel-botton bezel-horizon bezel-retrato frame videoframe videoframe-green")
add_static("images/glass", "glass-highlight glass-highlight-2")
add_static("images/overlays", "boxover fanartoverlay hide main-vidover menu-overlay overlay overlay-horizontal overlaydiamonds overlaydiamonds2 overlayvert overlaywall overlaywallcascade overlaywallleft overlaywallright overlaywallryu selected-overlay vidover")
add_static("images/objects", "pinball")
add_static("images/panels", "form-line form-tech painel panelbtg paneltg")
add_static("images/textures", "from-bit from-tech2")
add_static("images/masks", "wheel-opacity-mask wheel-opacity-mask-horizontal")
add_static("images/ui", "colorful-platformwheel-separator-dark covers-grad covers-grad1 default-grad menu menubg playlist playlist-v separation side shutdown shutdown2 top topbar topunderbg")
add_static("images/branding/logos", "packs-r-us-logo")
add_static("images/effects", "reflection-2 reflection-3")
add_static("images/ui", "time")

LAYOUT_COLLECTION_ART_KEYS = {
    "raph",
    "raph-copy",
    "tmnt-2",
    "tmnt-collection",
    "turtles",
}
LAYOUT_MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg",
    ".mp4", ".mkv", ".avi", ".mov", ".mp3", ".ogg", ".wav", ".flac", ".m4a",
    ".ttf", ".otf", ".woff", ".woff2",
}

# Special cases use the actual `imageType` directory at runtime as the source
# label, while storage uses a normalized semantic type.
TYPE_NORMALIZATION = {
    "manufacturer": "manufacturer",
    "logo": "logo",
    "genre": "genre",
    "game genre": "genre",
    "menu genre": "genre",
    "device": "device",
    "score": "score",
    "game score": "score",
    "menu score": "score",
    "color": "color",
    "year": "year",
    "game year": "year",
    "format": "format",
    "game format": "format",
    "game platform": "platform",
    "game developer": "developer",
    "game players": "players",
    "menu players": "players",
    "numberplayers": "players",
    "number players": "players",
    "isfavorite": "favorite",
    "game ratio": "aspect-ratio",
    "title": "title",
    "cristal": "crystal",
    "cristal2": "crystal",
    "sectional": "section",
    "_hidden": "hidden-source",
}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def inside_root(path: Path) -> bool:
    try:
        path.absolute().relative_to(ROOT.absolute())
        return True
    except ValueError:
        return False


def ensure_no_symlink_components(path: Path, *, allow_missing: bool = False) -> None:
    if not inside_root(path):
        raise ValueError(f"Caminho fora do projeto: {path}")
    current = ROOT
    rel = path.absolute().relative_to(ROOT.absolute())
    for index, component in enumerate(rel.parts):
        current = current / component
        if not os.path.lexists(current):
            if allow_missing:
                continue
            raise FileNotFoundError(current)
        if current.is_symlink():
            raise ValueError(f"Componente symlink não permitido nesta operação: {current}")
        if index < len(rel.parts) - 1 and not current.is_dir():
            raise NotADirectoryError(current)


def snapshot(path: Path) -> dict[str, Any]:
    ensure_no_symlink_components(path)
    st = path.stat()
    if not path.is_file():
        raise ValueError(f"Esperava arquivo regular: {path}")
    return {
        "size_bytes": st.st_size,
        "sha256": sha256_file(path),
        "mode": st.st_mode & 0o7777,
        "mtime_ns": st.st_mtime_ns,
        "device": st.st_dev,
        "inode": st.st_ino,
    }


def normalized_key(value: str) -> str:
    stem = Path(value).stem
    folded = unicodedata.normalize("NFKD", stem.casefold())
    ascii_stem = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_stem).strip("-")


def slug(value: str, limit: int = 84) -> str:
    result = normalized_key(value)
    result = result[:limit].strip("-")
    return result or "asset"


def static_destination(entry: dict[str, Any]) -> tuple[str, str]:
    old_path = entry["canonical_path"]
    source = ROOT / old_path
    if entry.get("category") != "images":
        return old_path, entry.get("category", "other")
    key = normalized_key(entry.get("asset", source.name))
    category = STATIC_CATEGORIES.get(key)
    if category is None:
        raise ValueError(f"Imagem estática sem classificação semântica explícita: {entry.get('asset')}")
    new_path = f"Assets/shared/{category}/{source.name}"
    return new_path, category


def dynamic_semantic_type(source_type: str) -> tuple[str, str | None]:
    folded = source_type.strip().casefold()
    if folded == "controller":
        return "controller-platforms", None
    if folded.startswith("controller "):
        control = folded.removeprefix("controller ").strip()
        if control in {"a", "b", "x", "y", "lb", "rb", "lt", "rt", "select", "start"}:
            return "controller-prompts/buttons", control
        if control == "d-pad":
            return "controller-prompts/directions", control
        if control in {"l-stick", "r-stick"}:
            return "controller-prompts/sticks", control
        if control == "xbox":
            return "controller-prompts/devices", control
        raise ValueError(f"Tipo de arte de controle não classificado: {source_type}")
    if folded.startswith("firstletter") or folded.startswith("-firstletter"):
        return "first-letter", None
    if re.fullmatch(r"playlist\d*", folded):
        return "playlist", None
    if folded in TYPE_NORMALIZATION:
        return TYPE_NORMALIZATION[folded], None
    raise ValueError(f"Tipo de artwork comum sem classificação semântica explícita: {source_type}")


def common_files() -> list[Path]:
    files: list[Path] = []
    for theme in sorted((p for p in LAYOUTS.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        base = theme / "collections" / "_common" / "medium_artwork"
        if not base.exists():
            continue
        for path in sorted(base.rglob("*"), key=lambda p: p.as_posix().casefold()):
            if path.is_symlink():
                raise ValueError(f"Symlink inesperado no artwork comum antes da operação: {relative(path)}")
            if path.is_file():
                files.append(path)
    return files


def project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def rewrite_xml(data: bytes, path_map: dict[str, str]) -> tuple[bytes, list[dict[str, Any]]]:
    text = data.decode("utf-8", errors="surrogateescape")
    edits: Counter[tuple[str, str]] = Counter()

    def rewrite_segment(segment: str) -> str:
        def replace_attribute(match: re.Match[str]) -> str:
            attribute, quote, value = match.group(1), match.group(2), match.group(3)
            updated = value
            for old, new in path_map.items():
                if old in updated:
                    updated = updated.replace(old, new)
                    edits[(old, new)] += 1
            if updated == value:
                return match.group(0)
            return f"{attribute}={quote}{updated}{quote}"
        return ATTRIBUTE_RE.sub(replace_attribute, segment)

    pieces = COMMENT_RE.split(text)
    for index in range(0, len(pieces), 2):
        pieces[index] = rewrite_segment(pieces[index])
    changed = "".join(pieces).encode("utf-8", errors="surrogateescape")
    changes = [
        {"from": old, "to": new, "occurrences": count}
        for (old, new), count in sorted(edits.items())
    ]
    return changed, changes


def plan_operation(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    historical = json.loads(HISTORICAL_MANIFEST.read_text(encoding="utf-8"))
    historical_assets = historical.get("shared_assets", [])
    static_assets: list[dict[str, Any]] = []
    static_by_hash: dict[str, dict[str, Any]] = {}
    static_path_map: dict[str, str] = {}
    all_targets: dict[str, str] = {}

    for entry in historical_assets:
        source_rel = entry["canonical_path"]
        source = ROOT / source_rel
        destination_rel, category = static_destination(entry)
        before = snapshot(source)
        if before["sha256"] != entry.get("sha256") or before["size_bytes"] != entry.get("size"):
            raise ValueError(f"Baseline histórico diverge do arquivo atual: {source_rel}")
        target_owner = all_targets.get(destination_rel)
        if target_owner and target_owner != before["sha256"]:
            raise FileExistsError(f"Colisão de destino entre hashes: {destination_rel}")
        all_targets[destination_rel] = before["sha256"]
        item = {
            "source": source_rel,
            "target": destination_rel,
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "mode": before["mode"],
            "mtime_ns": before["mtime_ns"],
            "category": category,
            "asset_name": entry.get("asset", source.name),
            "legacy_category": entry.get("category"),
            "move": source_rel != destination_rel,
            "historical_sources": entry.get("source_layout_copies", entry.get("previous_layout_copies", [])),
        }
        static_assets.append(item)
        static_by_hash[item["sha256"]] = item
        if source_rel != destination_rel:
            static_path_map[source_rel] = destination_rel

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in common_files():
        theme = path.relative_to(LAYOUTS).parts[0]
        common_root = LAYOUTS / theme / "collections" / "_common" / "medium_artwork"
        common_relative = path.relative_to(common_root)
        source_type = common_relative.parts[0]
        source_type, control_variant = dynamic_semantic_type(source_type)
        before = snapshot(path)
        target_item = {
            "source": relative(path),
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "mode": before["mode"],
            "mtime_ns": before["mtime_ns"],
            "device": before["device"],
            "theme": theme,
            "source_type": common_relative.parts[0],
            "semantic_type": source_type,
            "control_variant": control_variant,
            "logical_name": path.name,
            "source_variant_path": common_relative.parent.as_posix(),
        }
        grouped[before["sha256"]].append(target_item)

    dynamic_assets: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    for digest, sources in sorted(grouped.items()):
        sources.sort(key=lambda x: x["source"].casefold())
        names = Counter(x["logical_name"] for x in sources)
        canonical_name = sorted(names, key=lambda x: (-names[x], x.casefold(), x))[0]
        semantic_types = sorted({x["semantic_type"] for x in sources})
        controls = {x["control_variant"] for x in sources if x["control_variant"]}
        if digest in static_by_hash:
            target_rel = static_by_hash[digest]["target"]
            canonical_category = static_by_hash[digest]["category"]
            reused = True
        else:
            reused = False
            if semantic_types == ["controller-platforms"]:
                category = "Assets/shared/controller/platforms"
            elif all(value.startswith("controller-prompts/") for value in semantic_types):
                category = f"Assets/shared/controller/prompts/{semantic_types[0].split('/', 1)[1]}"
            else:
                category = (
                    "Assets/shared/collection-artwork/shared"
                    if len(semantic_types) > 1
                    else f"Assets/shared/collection-artwork/{semantic_types[0]}"
                )
            extension = Path(canonical_name).suffix.casefold() or ".png"
            stem = slug(Path(canonical_name).stem)
            prefix_len = 12
            target_rel = f"{category}/{digest[:2]}/{stem}--{digest[:prefix_len]}{extension}"
            while target_rel in all_targets and all_targets[target_rel] != digest:
                prefix_len += 4
                if prefix_len > len(digest):
                    raise FileExistsError(f"Não foi possível resolver colisão de caminho para SHA-256 {digest}")
                target_rel = f"{category}/{digest[:2]}/{stem}--{digest[:prefix_len]}{extension}"
            existing_hash = all_targets.get(target_rel)
            if existing_hash and existing_hash != digest:
                raise FileExistsError(f"Colisão de destino: {target_rel}")
            all_targets[target_rel] = digest
            canonical_category = category.removeprefix("Assets/shared/")

        sizes = {x["size_bytes"] for x in sources}
        if len(sizes) != 1:
            raise ValueError(f"Tamanho inconsistente no grupo SHA-256 {digest}")
        dynamic_assets.append({
            "sha256": digest,
            "size_bytes": sources[0]["size_bytes"],
            "target": target_rel,
            "category": canonical_category,
            "logical_name": canonical_name,
            "source_types": sorted({x["source_type"] for x in sources}, key=str.casefold),
            "semantic_types": semantic_types,
            "source_count": len(sources),
            "reuses_existing_asset": reused,
            "representative_source": None if reused else sources[0]["source"],
            "sources": sources,
        })
        for source in sources:
            source_path = ROOT / source["source"]
            target_path = ROOT / target_rel
            if not target_path.exists() and not reused:
                ensure_no_symlink_components(target_path, allow_missing=True)
                parent = target_path.parent
                while not parent.exists():
                    parent = parent.parent
                if parent.stat().st_dev != source_path.stat().st_dev:
                    raise OSError(f"Movimento exigiria cópia entre dispositivos: {source_path} -> {target_path}")
            alias = {
                "path": source["source"],
                "target": target_rel,
                "sha256": digest,
                "size_bytes": source["size_bytes"],
                "source_type": source["source_type"],
                "semantic_type": source["semantic_type"],
                "theme": source["theme"],
                "logical_name": source["logical_name"],
                "source_variant_path": source["source_variant_path"],
                "mode": source["mode"],
                "mtime_ns": source["mtime_ns"],
            }
            aliases.append(alias)

    for asset in dynamic_assets:
        owner = all_targets.get(asset["target"])
        if owner and owner != asset["sha256"]:
            raise FileExistsError(f"Destino canônico colidente: {asset['target']}")
        all_targets[asset["target"]] = asset["sha256"]

    xml_edits: list[dict[str, Any]] = []
    for path in sorted(ROOT.rglob("*.xml"), key=lambda p: p.as_posix().casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        original = path.read_bytes()
        updated, edits = rewrite_xml(original, static_path_map)
        if updated == original:
            continue
        st = path.stat()
        xml_edits.append({
            "path": relative(path),
            "sha256_before": sha256_bytes(original),
            "sha256_after": sha256_bytes(updated),
            "size_before": len(original),
            "size_after": len(updated),
            "mode": st.st_mode & 0o7777,
            "mtime_ns": st.st_mtime_ns,
            "edits": edits,
        })

    all_move_targets = [x["target"] for x in static_assets if x["move"]]
    all_move_targets.extend(x["target"] for x in dynamic_assets if not x["reuses_existing_asset"])
    if len(all_move_targets) != len(set(all_move_targets)):
        raise FileExistsError("Dois movimentos apontam para o mesmo destino")
    for item in static_assets:
        if item["move"]:
            if os.path.lexists(ROOT / item["target"]):
                raise FileExistsError(f"Destino já existe: {item['target']}")
            if (ROOT / item["source"]).stat().st_dev != SHARED.stat().st_dev:
                raise OSError(f"Movimento de asset compartilhado cruzaria dispositivos: {item['source']}")
    for item in dynamic_assets:
        if not item["reuses_existing_asset"] and os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino já existe: {item['target']}")

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": f"retrofe-assets-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "kind": "retrofe_shared_asset_reorganization",
        "status": "planned",
        "created_at": now(),
        "project_root": ".",
        "historical_manifest_preserved": "Manifests/asset-sharing.json",
        "current_catalog": "Manifests/asset-catalog.json",
        "method": "SHA-256 exact equality; one regular canonical payload per digest; relative symlinks preserve commonlayout lookup names.",
        "scope": {
            "static_images": "Assets/shared/images/*",
            "layout_common_artwork": "layouts/*/collections/_common/medium_artwork/**/*.png",
            "excluded": ["collections/*/medium_artwork/ game-specific artwork", "ROMs", "BIOS", "saves", "UserData"]
        },
        "static_assets": static_assets,
        "dynamic_assets": dynamic_assets,
        "layout_aliases": aliases,
        "xml_edits": xml_edits,
        "summary": {
            "static_shared_assets": len(static_assets),
            "static_images_reclassified": sum(1 for x in static_assets if x["legacy_category"] == "images"),
            "static_xml_files_to_update": len(xml_edits),
            "static_xml_reference_occurrences": sum(edit["occurrences"] for row in xml_edits for edit in row["edits"]),
            "layout_common_artwork_paths": len(aliases),
            "layout_common_sha256_groups": len(dynamic_assets),
            "new_canonical_collection_assets": sum(1 for x in dynamic_assets if not x["reuses_existing_asset"]),
            "collection_assets_reused_from_existing_shared": sum(1 for x in dynamic_assets if x["reuses_existing_asset"]),
            "layout_common_paths_to_become_relative_symlinks": len(aliases),
            "duplicate_payload_copies_replaced_by_aliases": len(aliases) - sum(1 for x in dynamic_assets if not x["reuses_existing_asset"]),
            "layout_common_source_bytes": sum(x["size_bytes"] for x in aliases),
            "new_canonical_collection_payload_bytes": sum(x["size_bytes"] for x in dynamic_assets if not x["reuses_existing_asset"]),
            "duplicate_payload_bytes_reclaimed": sum(x["size_bytes"] for x in aliases) - sum(x["size_bytes"] for x in dynamic_assets if not x["reuses_existing_asset"]),
            "all_asset_hashes_unique_after_plan": True,
            "source_files_on_same_device": True,
        },
        "rollback": {
            "command": "python3 Tools/materializers/reorganize-shared-assets.py rollback --manifest Manifests/<this-file>",
            "method": "Restore every original common-artwork path as a regular file from its verified canonical SHA-256 target; reverse exact XML substitutions; return static images to their prior paths; reconstruct a legacy baseline asset-catalog.json.",
            "permanent_deletion": False,
        },
    }
    if manifest["summary"]["static_shared_assets"] != 191:
        raise ValueError(f"Esperava baseline histórico de 191 assets, encontrei {manifest['summary']['static_shared_assets']}")
    write_json(manifest_path, manifest)
    return manifest


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)


def rel_link_target(alias: Path, target: Path) -> str:
    return os.path.relpath(target, start=alias.parent)


def atomic_write_bytes(path: Path, data: bytes, mode: int, mtime_ns: int) -> None:
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    temp.write_bytes(data)
    os.chmod(temp, mode)
    os.utime(temp, ns=(mtime_ns, mtime_ns))
    os.replace(temp, path)


def check_source(path: Path, expected_hash: str, expected_size: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Fonte não é um arquivo regular: {relative(path)}")
    if path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
        raise ValueError(f"Hash/tamanho divergiu antes da alteração: {relative(path)}")


def check_target(path: Path, expected_hash: str, expected_size: int) -> None:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(f"Destino canônico ausente ou não regular: {relative(path)}")
    if path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
        raise ValueError(f"Hash/tamanho divergiu no destino: {relative(path)}")


def build_current_catalog(manifest: dict[str, Any], mode: str = "deduplicated") -> dict[str, Any]:
    assets: dict[str, dict[str, Any]] = {}
    dynamic_by_hash = {item["sha256"]: item for item in manifest.get("dynamic_assets", [])}
    alias_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for alias in manifest.get("layout_aliases", []):
        alias_by_target[alias["target"]].append(alias)

    for item in manifest.get("static_assets", []):
        target = item["source"] if mode == "legacy-restored" else item["target"]
        entry = {
            "path": target,
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "asset_class": "static",
            "category": item["legacy_category"] if mode == "legacy-restored" else item["category"],
            "logical_name": item["asset_name"],
            "source_paths": item.get("historical_sources", []),
        }
        group = dynamic_by_hash.get(item["sha256"])
        if group:
            entry["layout_alias_count"] = group["source_count"]
            entry["layout_source_types"] = group["source_types"]
        assets[target] = entry

    if mode == "deduplicated":
        for item in manifest.get("dynamic_assets", []):
            if item["reuses_existing_asset"]:
                target = item["target"]
                if target in assets:
                    assets[target]["layout_alias_count"] = item["source_count"]
                    assets[target]["layout_source_types"] = item["source_types"]
                continue
            assets[item["target"]] = {
                "path": item["target"],
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "asset_class": "collection-artwork",
                "category": item["category"],
                "logical_name": item["logical_name"],
                "source_types": item["source_types"],
                "layout_alias_count": item["source_count"],
            }

    aliases = [] if mode == "legacy-restored" else [
        {
            "path": item["path"],
            "target": item["target"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "source_type": item["source_type"],
            "semantic_type": item["semantic_type"],
            "theme": item["theme"],
            "logical_name": item["logical_name"],
            "source_variant_path": item["source_variant_path"],
        }
        for item in manifest.get("layout_aliases", [])
    ]
    canonical = sorted(assets.values(), key=lambda x: x["path"].casefold())
    return {
        "schema_version": 1,
        "mode": mode,
        "updated_at": now(),
        "source_of_historical_provenance": "Manifests/asset-sharing.json (preserved; original pre-reorganization paths)",
        "reorganization_manifest": str(manifest.get("manifest_path", "Manifests/shared-asset-reorganization-20261024.json")),
        "storage_rules": {
            "static_images": "Assets/shared/images/<semantic-category>/<logical-name>--<sha256-prefix>.<ext>",
            "controller_art": "Assets/shared/controller/<art-type>/...",
            "collection_artwork": "Assets/shared/collection-artwork/<semantic-type>/<sha256-prefix>/<logical-name>--<sha256-prefix>.png",
            "layout_common": "relative symlink aliases preserve layouts/<theme>/collections/_common/medium_artwork/<imageType>/<item-name>.png",
            "per_game_artwork": "Unchanged; remains scoped to its collection until a separate migration to media/<system_id>/<media_type>/.",
        },
        "summary": {
            "canonical_assets": len(canonical),
            "unique_sha256": len({item["sha256"] for item in canonical}),
            "relative_layout_aliases": len(aliases),
            "category_counts": dict(sorted(Counter(item["category"] for item in canonical).items())),
        },
        "canonical_assets": canonical,
        "layout_aliases": aliases,
    }


def main_media_files(*, known_aliases: set[str] | None = None) -> list[Path]:
    if not MAIN_ARTWORK_ROOT.is_dir():
        return []
    known_aliases = known_aliases or set()
    files: list[Path] = []
    for path in sorted(MAIN_ARTWORK_ROOT.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if path.is_symlink():
            if relative(path) in known_aliases:
                continue
            raise ValueError(f"Symlink não catalogado em Main/medium_artwork: {relative(path)}")
        if not path.is_file() or path.suffix.casefold() not in MAIN_MEDIA_EXTENSIONS:
            continue
        rel = path.relative_to(MAIN_ARTWORK_ROOT)
        if "_template" in {part.casefold() for part in rel.parts}:
            continue
        if not rel.parts or rel.parts[0] not in MAIN_TYPE_SEMANTICS:
            raise ValueError(f"Tipo de artwork Main sem classificação explícita: {relative(path)}")
        files.append(path)
    return files


def main_source_artwork_files(*, known_aliases: set[str] | None = None) -> list[Path]:
    if not MAIN_ARTWORK_ROOT.is_dir():
        return []
    known_aliases = known_aliases or set()
    files: list[Path] = []
    for path in sorted(MAIN_ARTWORK_ROOT.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if path.is_symlink():
            if relative(path) in known_aliases:
                continue
            raise ValueError(f"Symlink não catalogado em Main/medium_artwork: {relative(path)}")
        if not path.is_file():
            continue
        rel = path.relative_to(MAIN_ARTWORK_ROOT)
        components = {part.casefold() for part in rel.parts}
        in_template = "_template" in components
        is_psd = path.suffix.casefold() == ".psd"
        if not (in_template or is_psd):
            continue
        if path.suffix.casefold() not in MAIN_MEDIA_EXTENSIONS | {".psd"}:
            continue
        files.append(path)
    return files


def main_support_file_moves() -> list[dict[str, Any]]:
    locations = {
        "logo_fullset": "logo-full-set",
        "device_icon": "device-icons",
        "fanart_picturemovie": "fanart-picture-movie",
        "device_cleanfullsetup": "device-clean-full-setup",
    }
    moves: list[dict[str, Any]] = []
    for source_type, docs_category in locations.items():
        base = MAIN_ARTWORK_ROOT / source_type
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
            if not path.is_file() or path.is_symlink():
                continue
            include = (
                source_type == "logo_fullset" and path.suffix.casefold() == ".docx"
                or source_type == "device_icon" and path.name.casefold() in {"readme.txt", "terms.txt"}
                or source_type == "fanart_picturemovie" and path.name.casefold() == "readme.txt"
                or source_type == "device_cleanfullsetup" and path.name.casefold() in {"_list.txt", "_make_list.bat"}
            )
            if not include:
                continue
            target = ROOT / "Docs" / "asset-sources" / docs_category / path.name
            ensure_no_symlink_components(target, allow_missing=True)
            if os.path.lexists(target):
                raise FileExistsError(f"Destino de documentação já existe: {relative(target)}")
            existing_parent = target.parent
            while not existing_parent.exists():
                existing_parent = existing_parent.parent
            if existing_parent.stat().st_dev != path.stat().st_dev:
                raise OSError(f"Movimento de documentação cruzaria dispositivos: {path} -> {target}")
            before = snapshot(path)
            moves.append({
                "source": relative(path),
                "target": relative(target),
                "sha256": before["sha256"],
                "size_bytes": before["size_bytes"],
                "mode": before["mode"],
                "mtime_ns": before["mtime_ns"],
                "device": before["device"],
                "inode": before["inode"],
                "semantic_category": docs_category,
            })
    return moves


def main_source_semantic_type(path: Path) -> str:
    rel = path.relative_to(MAIN_ARTWORK_ROOT)
    components = {part.casefold() for part in rel.parts}
    if "_hidden" in components:
        return "logo-templates"
    if "_template" in components:
        return "device-fullsetup-templates"
    return f"{rel.parts[0].replace('_', '-')}-source"


def main_canonical_category(semantic_type: str) -> str:
    if semantic_type.startswith("controller/"):
        return semantic_type
    return f"collection-artwork/{semantic_type}"


def main_logical_name(sources: list[dict[str, Any]], semantic_type: str) -> str:
    names = Counter(Path(source["logical_name"]).stem for source in sources)
    most_common = sorted(names, key=lambda name: (-names[name], name.casefold(), name))[0]
    if names[most_common] >= 2:
        return most_common
    if len(sources) >= 4:
        return "generic"
    return f"{semantic_type.rsplit('/', 1)[-1]}-shared"


def source_artwork_index_append_rows(groups: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for group in groups:
        for source in group["sources"]:
            writer.writerow([
                Path(source["logical_name"]).name,
                group["target"],
                group["sha256"],
                group["size_bytes"],
                source["source"],
            ])
    return output.getvalue().encode("utf-8")


def existing_source_artwork_assets() -> list[dict[str, Any]]:
    if not SOURCE_ARTWORK_ROOT.is_dir():
        return []
    assets: list[dict[str, Any]] = []
    for path in sorted(SOURCE_ARTWORK_ROOT.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if path.is_symlink():
            raise ValueError(f"Symlink inesperado dentro de Assets/source-artwork: {relative(path)}")
        if not path.is_file():
            continue
        before = snapshot(path)
        assets.append({
            "path": relative(path),
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "category": "historical-source-artwork",
            "logical_name": path.name,
        })
    return assets


def plan_main_artwork_operation(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    catalog_bytes = CATALOG_PATH.read_bytes()
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    audit_catalog(catalog)
    known_aliases = {item["path"] for item in catalog.get("main_artwork_aliases", [])}
    known_source_aliases = {item["path"] for item in catalog.get("source_artwork_aliases", [])}
    current_assets = {item["path"]: item for item in catalog.get("canonical_assets", [])}
    current_by_hash = {item["sha256"]: item for item in current_assets.values()}
    if len(current_by_hash) != len(current_assets):
        raise ValueError("O catálogo atual possui hashes canônicos repetidos")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    files = main_media_files(known_aliases=known_aliases | known_source_aliases)
    for path in files:
        rel = path.relative_to(MAIN_ARTWORK_ROOT)
        snapshot_before = snapshot(path)
        grouped[snapshot_before["sha256"]].append({
            "source": relative(path),
            "sha256": snapshot_before["sha256"],
            "size_bytes": snapshot_before["size_bytes"],
            "mode": snapshot_before["mode"],
            "mtime_ns": snapshot_before["mtime_ns"],
            "device": snapshot_before["device"],
            "inode": snapshot_before["inode"],
            "link_count": path.stat().st_nlink,
            "source_type": rel.parts[0],
            "semantic_type": MAIN_TYPE_SEMANTICS[rel.parts[0]],
            "logical_name": path.name,
            "source_variant_path": rel.parent.as_posix(),
        })

    source_grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_files = main_source_artwork_files(known_aliases=known_aliases | known_source_aliases)
    for path in source_files:
        rel = path.relative_to(MAIN_ARTWORK_ROOT)
        before = snapshot(path)
        semantic_type = main_source_semantic_type(path)
        source_grouped[before["sha256"]].append({
            "source": relative(path),
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "mode": before["mode"],
            "mtime_ns": before["mtime_ns"],
            "device": before["device"],
            "inode": before["inode"],
            "link_count": path.stat().st_nlink,
            "source_type": rel.parts[0],
            "semantic_type": semantic_type,
            "logical_name": path.name,
            "source_variant_path": rel.parent.as_posix(),
        })

    source_existing_assets = existing_source_artwork_assets()
    source_existing_by_hash = {item["sha256"]: item for item in source_existing_assets}
    if len(source_existing_by_hash) != len(source_existing_assets):
        raise ValueError("Assets/source-artwork possui hashes canônicos duplicados")
    shared_source_collisions = set(current_by_hash).intersection(source_existing_by_hash)
    if shared_source_collisions:
        raise ValueError(f"Assets/shared e Assets/source-artwork repetem hashes: {sorted(shared_source_collisions)[:5]}")
    source_new_assets: list[dict[str, Any]] = []
    source_groups: list[dict[str, Any]] = []
    source_aliases: list[dict[str, Any]] = []
    source_targets = {item["path"] for item in source_existing_assets}
    for digest, sources in sorted(source_grouped.items()):
        sources.sort(key=lambda item: item["source"].casefold())
        if len({item["size_bytes"] for item in sources}) != 1:
            raise ValueError(f"Tamanho inconsistente no grupo de arte-fonte SHA-256 {digest}")
        semantic_counts = Counter(item["semantic_type"] for item in sources)
        chosen_semantic = sorted(semantic_counts, key=lambda key: (-semantic_counts[key], key.casefold()))[0]
        logical_name = main_logical_name(sources, chosen_semantic)
        existing_shared = current_by_hash.get(digest)
        existing_source = source_existing_by_hash.get(digest)
        if existing_shared and existing_source:
            raise ValueError(f"Hash de arte-fonte existe em Assets/shared e Assets/source-artwork: {digest}")
        if existing_shared:
            target_rel = existing_shared["path"]
            category = existing_shared.get("category", "shared-existing")
            reused = True
        elif existing_source:
            target_rel = existing_source["path"]
            category = existing_source.get("category", "historical-source-artwork")
            reused = True
        else:
            extensions = Counter(Path(item["logical_name"]).suffix.casefold() for item in sources)
            extension = sorted(extensions, key=lambda value: (-extensions[value], value))[0] or ".png"
            category = chosen_semantic
            prefix_len = 12
            target_rel = (
                f"Assets/source-artwork/{category}/{digest[:2]}/"
                f"{slug(logical_name)}--{digest[:prefix_len]}{extension}"
            )
            while target_rel in source_targets:
                prefix_len += 4
                if prefix_len > len(digest):
                    raise FileExistsError(f"Não foi possível resolver caminho da arte-fonte {digest}")
                target_rel = (
                    f"Assets/source-artwork/{category}/{digest[:2]}/"
                    f"{slug(logical_name)}--{digest[:prefix_len]}{extension}"
                )
            if os.path.lexists(ROOT / target_rel):
                raise FileExistsError(f"Destino de arte-fonte já existe fora do inventário: {target_rel}")
            source_targets.add(target_rel)
            representative = sources[0]
            source_new_assets.append({
                "sha256": digest,
                "size_bytes": representative["size_bytes"],
                "target": target_rel,
                "category": category,
                "logical_name": f"{logical_name}{extension}",
                "source_types": sorted({item["source_type"] for item in sources}, key=str.casefold),
                "semantic_types": sorted(semantic_counts, key=str.casefold),
                "source_count": len(sources),
                "representative_source": representative["source"],
                "sources": sources,
            })
            reused = False
        group = {
            "sha256": digest,
            "size_bytes": sources[0]["size_bytes"],
            "target": target_rel,
            "category": category,
            "logical_name": logical_name,
            "semantic_types": sorted(semantic_counts, key=str.casefold),
            "reuses_existing_asset": reused,
            "representative_source": None if reused else sources[0]["source"],
            "sources": sources,
        }
        source_groups.append(group)
        for source in sources:
            source_aliases.append({
                "path": source["source"],
                "target": target_rel,
                "sha256": digest,
                "size_bytes": source["size_bytes"],
                "source_type": source["source_type"],
                "semantic_type": source["semantic_type"],
                "logical_name": source["logical_name"],
                "source_variant_path": source["source_variant_path"],
                "mode": source["mode"],
                "mtime_ns": source["mtime_ns"],
                "device": source["device"],
                "inode": source["inode"],
                "link_count": source["link_count"],
            })

    all_targets = {item["path"]: item["sha256"] for item in current_assets.values()}
    new_assets: list[dict[str, Any]] = []
    aliases: list[dict[str, Any]] = []
    selected_groups: list[dict[str, Any]] = []
    for digest, sources in sorted(grouped.items()):
        existing = current_by_hash.get(digest)
        if len(sources) < 2 and existing is None:
            continue
        sources.sort(key=lambda item: item["source"].casefold())
        semantic_counts = Counter(item["semantic_type"] for item in sources)
        # Prefer the controller namespace when identical bytes are explicitly
        # used for both controller and device lookups; otherwise use the most
        # common semantic source type, with deterministic lexical tie-breaking.
        if any(value.startswith("controller/") for value in semantic_counts):
            top = max(semantic_counts.values())
            controller_options = [key for key, count in semantic_counts.items()
                                  if count == top and key.startswith("controller/")]
            chosen_semantic = sorted(controller_options or [key for key, count in semantic_counts.items()
                                                              if count == top], key=str.casefold)[0]
        else:
            chosen_semantic = sorted(semantic_counts, key=lambda key: (-semantic_counts[key], key.casefold()))[0]
        logical_name = main_logical_name(sources, chosen_semantic)
        if existing:
            target_rel = existing["path"]
            canonical_category = existing.get("category", "shared-existing")
            reused = True
        else:
            canonical_category = main_canonical_category(chosen_semantic)
            extensions = Counter(Path(item["logical_name"]).suffix.casefold() for item in sources)
            extension = sorted(extensions, key=lambda value: (-extensions[value], value))[0] or ".png"
            prefix_len = 12
            target_rel = f"Assets/shared/{canonical_category}/{digest[:2]}/{slug(logical_name)}--{digest[:prefix_len]}{extension}"
            while target_rel in all_targets and all_targets[target_rel] != digest:
                prefix_len += 4
                if prefix_len > len(digest):
                    raise FileExistsError(f"Não foi possível resolver colisão de caminho para SHA-256 {digest}")
                target_rel = f"Assets/shared/{canonical_category}/{digest[:2]}/{slug(logical_name)}--{digest[:prefix_len]}{extension}"
            if os.path.lexists(ROOT / target_rel):
                raise FileExistsError(f"Destino canônico já existe fora do catálogo: {target_rel}")
            all_targets[target_rel] = digest
            representative = sources[0]
            new_assets.append({
                "sha256": digest,
                "size_bytes": representative["size_bytes"],
                "target": target_rel,
                "category": canonical_category,
                "logical_name": f"{logical_name}{extension}",
                "source_types": sorted({item["source_type"] for item in sources}, key=str.casefold),
                "semantic_types": sorted(semantic_counts, key=str.casefold),
                "source_count": len(sources),
                "representative_source": representative["source"],
                "sources": sources,
            })
            reused = False
        if len({item["size_bytes"] for item in sources}) != 1:
            raise ValueError(f"Tamanho inconsistente no grupo SHA-256 {digest}")
        group = {
            "sha256": digest,
            "size_bytes": sources[0]["size_bytes"],
            "target": target_rel,
            "category": canonical_category,
            "logical_name": logical_name,
            "semantic_types": sorted(semantic_counts, key=str.casefold),
            "reuses_existing_asset": reused,
            "representative_source": None if reused else sources[0]["source"],
            "sources": sources,
        }
        selected_groups.append(group)
        for source in sources:
            aliases.append({
                "path": source["source"],
                "target": target_rel,
                "sha256": digest,
                "size_bytes": source["size_bytes"],
                "source_type": source["source_type"],
                "semantic_type": source["semantic_type"],
                "logical_name": source["logical_name"],
                "source_variant_path": source["source_variant_path"],
                "mode": source["mode"],
                "mtime_ns": source["mtime_ns"],
                "device": source["device"],
                "inode": source["inode"],
                "link_count": source["link_count"],
            })

    alias_paths = [item["path"] for item in aliases]
    if len(alias_paths) != len(set(alias_paths)):
        raise ValueError("Um caminho Main está repetido no plano de aliases")
    source_alias_paths = [item["path"] for item in source_aliases]
    if len(source_alias_paths) != len(set(source_alias_paths)):
        raise ValueError("Um caminho de arte-fonte Main está repetido no plano de aliases")
    for item in new_assets:
        source_path = ROOT / item["representative_source"]
        target_path = ROOT / item["target"]
        ensure_no_symlink_components(target_path, allow_missing=True)
        parent = target_path.parent
        while not parent.exists():
            parent = parent.parent
        if parent.stat().st_dev != source_path.stat().st_dev:
            raise OSError(f"Movimento cruzaria dispositivos: {source_path} -> {target_path}")

    for item in source_new_assets:
        source_path = ROOT / item["representative_source"]
        target_path = ROOT / item["target"]
        ensure_no_symlink_components(target_path, allow_missing=True)
        parent = target_path.parent
        while not parent.exists():
            parent = parent.parent
        if parent.stat().st_dev != source_path.stat().st_dev:
            raise OSError(f"Movimento de arte-fonte cruzaria dispositivos: {source_path} -> {target_path}")

    support_moves = main_support_file_moves()
    source_index_bytes = SOURCE_ARTWORK_INDEX.read_bytes() if SOURCE_ARTWORK_INDEX.exists() else b""
    source_index_append = source_artwork_index_append_rows(source_groups)

    new_payload_bytes = sum(item["size_bytes"] for item in new_assets)
    source_payload_bytes = sum(item["size_bytes"] for item in aliases)
    duplicate_bytes = source_payload_bytes - new_payload_bytes
    source_payload_bytes = sum(item["size_bytes"] for item in source_aliases)
    source_new_payload_bytes = sum(item["size_bytes"] for item in source_new_assets)
    source_duplicate_bytes = source_payload_bytes - source_new_payload_bytes
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "operation_id": f"retrofe-main-assets-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "kind": "retrofe_main_artwork_and_source_asset_organization",
        "status": "planned",
        "created_at": now(),
        "project_root": ".",
        "catalog_path": relative(CATALOG_PATH),
        "catalog_before_sha256": sha256_bytes(catalog_bytes),
        "catalog_before": catalog,
        "method": "SHA-256 exact equality; centralize duplicate runtime artwork and all non-runtime design templates; preserve runtime/source lookup paths with relative symlinks; move vendor documentation to Docs/asset-sources.",
        "scope": {
            "runtime_artwork": "collections/Main/medium_artwork/**/* with image, audio, or video extensions, excluding _Template sources",
            "source_artwork": "all supported media files under _Template plus PSD masters under Main/medium_artwork, with one exact-hash canonical payload under Assets/source-artwork or reuse of an existing canonical asset",
            "support_files": "vendor documentation and the archived Windows list helper move to Docs/asset-sources without content changes",
            "excluded": ["story text, ROMs, BIOS, saves", "per-game artwork outside Main"],
        },
        "main_artwork_assets": new_assets,
        "main_artwork_groups": selected_groups,
        "main_artwork_aliases": aliases,
        "source_artwork_assets": source_new_assets,
        "source_artwork_existing_assets": source_existing_assets,
        "source_artwork_groups": source_groups,
        "source_artwork_aliases": source_aliases,
        "support_file_moves": support_moves,
        "source_artwork_index_before_sha256": sha256_bytes(source_index_bytes),
        "source_artwork_index_before": source_index_bytes.decode("utf-8"),
        "source_artwork_index_append": source_index_append.decode("utf-8"),
        "summary": {
            "main_runtime_media_files_scanned": len(files),
            "main_runtime_media_hash_groups": len(grouped),
            "main_artwork_hash_groups_centralized": len(selected_groups),
            "main_artwork_alias_paths": len(aliases),
            "new_canonical_assets": len(new_assets),
            "groups_reusing_existing_shared_assets": sum(1 for item in selected_groups if item["reuses_existing_asset"]),
            "duplicate_regular_payload_bytes_removed_from_source_paths": duplicate_bytes,
            "new_canonical_payload_bytes": new_payload_bytes,
            "source_artwork_files_scanned": len(source_files),
            "source_artwork_hash_groups": len(source_grouped),
            "source_artwork_alias_paths": len(source_aliases),
            "source_artwork_hash_groups_centralized": len(source_groups),
            "new_source_artwork_assets": len(source_new_assets),
            "source_groups_reusing_existing_assets": sum(1 for item in source_groups if item["reuses_existing_asset"]),
            "source_artwork_duplicate_regular_payload_bytes_removed": source_duplicate_bytes,
            "new_source_artwork_payload_bytes": source_new_payload_bytes,
            "support_files_moved_to_docs": len(support_moves),
            "rollback_extra_space_required_bytes": duplicate_bytes + source_duplicate_bytes,
            "main_nonruntime_files_preserved": sum(1 for path in MAIN_ARTWORK_ROOT.rglob("*")
                                                     if path.is_file() and not path.is_symlink()
                                                     and path.suffix.casefold() not in MAIN_MEDIA_EXTENSIONS),
            "all_paths_stay_inside_project": True,
        },
        "rollback": {
            "command": "python3 Tools/materializers/reorganize-shared-assets.py rollback-main --manifest Manifests/<this-file>",
            "method": "Restore Main runtime/source paths and support documents from verified canonical payloads; remove only newly created payloads; restore the saved catalog and source-artwork index baselines.",
            "permanent_deletion": False,
        },
    }
    manifest["manifest_path"] = relative(manifest_path)
    write_json(manifest_path, manifest)
    return manifest


def catalog_with_main_artwork(catalog: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(catalog, ensure_ascii=False))
    assets = {item["path"]: item for item in result.get("canonical_assets", [])}
    by_hash = {item["sha256"]: item for item in assets.values()}
    for item in manifest.get("main_artwork_assets", []):
        if item["sha256"] in by_hash:
            raise ValueError(f"Hash já existe no catálogo ao adicionar Main: {item['sha256']}")
        assets[item["target"]] = {
            "path": item["target"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "asset_class": "controller-artwork" if item["category"].startswith("controller/") else "collection-artwork",
            "category": item["category"],
            "logical_name": item["logical_name"],
            "source_types": item["source_types"],
            "semantic_types": item["semantic_types"],
            "main_artwork_alias_count": item["source_count"],
        }
        by_hash[item["sha256"]] = assets[item["target"]]
    for item in manifest.get("main_artwork_groups", []):
        if item["reuses_existing_asset"]:
            asset = assets.get(item["target"])
            if asset is None:
                raise ValueError(f"Asset compartilhado de Main ausente do catálogo: {item['target']}")
            asset["main_artwork_alias_count"] = asset.get("main_artwork_alias_count", 0) + len(item["sources"])
            asset["main_artwork_source_types"] = sorted(
                set(asset.get("main_artwork_source_types", [])) | {source["source_type"] for source in item["sources"]},
                key=str.casefold,
            )
    old_aliases = result.get("main_artwork_aliases", [])
    existing_paths = {item["path"] for item in old_aliases}
    new_aliases = [
        {
            "path": item["path"], "target": item["target"], "sha256": item["sha256"],
            "size_bytes": item["size_bytes"], "source_type": item["source_type"],
            "semantic_type": item["semantic_type"], "logical_name": item["logical_name"],
            "source_variant_path": item["source_variant_path"],
        }
        for item in manifest.get("main_artwork_aliases", [])
    ]
    if existing_paths.intersection(item["path"] for item in new_aliases):
        raise ValueError("Plano tenta substituir um alias Main já registrado")
    result["canonical_assets"] = sorted(assets.values(), key=lambda item: item["path"].casefold())
    result["main_artwork_aliases"] = sorted(old_aliases + new_aliases, key=lambda item: item["path"].casefold())

    source_assets = {item["path"]: item for item in manifest.get("source_artwork_existing_assets", [])}
    for item in manifest.get("source_artwork_assets", []):
        if item["target"] in source_assets:
            raise ValueError(f"Caminho canônico de arte-fonte repetido: {item['target']}")
        source_assets[item["target"]] = {
            "path": item["target"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "category": item["category"],
            "logical_name": item["logical_name"],
            "source_types": item["source_types"],
            "semantic_types": item["semantic_types"],
            "main_source_artwork_alias_count": item["source_count"],
        }
    # Existing canonical source files keep their historical provenance, while
    # source groups that reuse them gain an explicit current alias count.
    source_by_hash = {item["sha256"]: item for item in source_assets.values()}
    if len(source_by_hash) != len(source_assets):
        raise ValueError("Assets/source-artwork possui hashes duplicados ao atualizar o catálogo")
    shared_hashes = {item["sha256"] for item in result["canonical_assets"]}
    if shared_hashes.intersection(source_by_hash):
        raise ValueError("O catálogo não pode duplicar payload entre Assets/shared e Assets/source-artwork")
    for group in manifest.get("source_artwork_groups", []):
        if group["reuses_existing_asset"] and group["target"] in source_assets:
            asset = source_assets[group["target"]]
            asset["main_source_artwork_alias_count"] = asset.get("main_source_artwork_alias_count", 0) + len(group["sources"])
            asset["main_source_artwork_source_types"] = sorted(
                set(asset.get("main_source_artwork_source_types", []))
                | {source["source_type"] for source in group["sources"]},
                key=str.casefold,
            )
        elif group["reuses_existing_asset"] and group["target"] in assets:
            asset = assets[group["target"]]
            asset["main_source_artwork_alias_count"] = asset.get("main_source_artwork_alias_count", 0) + len(group["sources"])
            asset["main_source_artwork_source_types"] = sorted(
                set(asset.get("main_source_artwork_source_types", []))
                | {source["source_type"] for source in group["sources"]},
                key=str.casefold,
            )
    old_source_aliases = result.get("source_artwork_aliases", [])
    existing_source_paths = {item["path"] for item in old_source_aliases}
    new_source_aliases = [
        {
            "path": item["path"], "target": item["target"], "sha256": item["sha256"],
            "size_bytes": item["size_bytes"], "source_type": item["source_type"],
            "semantic_type": item["semantic_type"], "logical_name": item["logical_name"],
            "source_variant_path": item["source_variant_path"],
        }
        for item in manifest.get("source_artwork_aliases", [])
    ]
    if existing_source_paths.intersection(item["path"] for item in new_source_aliases):
        raise ValueError("Plano tenta substituir um alias de arte-fonte Main já registrado")
    result["source_artwork_assets"] = sorted(source_assets.values(), key=lambda item: item["path"].casefold())
    result["source_artwork_aliases"] = sorted(old_source_aliases + new_source_aliases, key=lambda item: item["path"].casefold())
    result.setdefault("storage_rules", {})["main_collection_artwork"] = (
        "exact-hash canonical payloads under Assets/shared/collection-artwork or Assets/shared/controller; "
        "relative symlinks preserve collections/Main/medium_artwork lookup names"
    )
    result["storage_rules"]["source_artwork"] = (
        "non-runtime source art and design masters under Assets/source-artwork/<semantic-category>/<sha-prefix>/; "
        "source lookup aliases remain relative and are recorded separately from runtime artwork"
    )
    result["updated_at"] = now()
    result["reorganization_manifest"] = manifest.get("manifest_path", result.get("reorganization_manifest"))
    result["summary"]["canonical_assets"] = len(result["canonical_assets"])
    result["summary"]["unique_sha256"] = len({item["sha256"] for item in result["canonical_assets"]})
    result["summary"]["relative_main_artwork_aliases"] = len(result["main_artwork_aliases"])
    result["summary"]["source_artwork_assets"] = len(result["source_artwork_assets"])
    result["summary"]["relative_source_artwork_aliases"] = len(result["source_artwork_aliases"])
    result["summary"]["category_counts"] = dict(sorted(Counter(item["category"] for item in result["canonical_assets"]).items()))
    return result


def apply_main_artwork_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "planned":
        raise ValueError(f"Manifesto precisa estar planned, estado atual: {manifest.get('status')}")
    current_catalog_bytes = CATALOG_PATH.read_bytes()
    if sha256_bytes(current_catalog_bytes) != manifest.get("catalog_before_sha256"):
        raise ValueError("Catálogo de assets mudou desde o planejamento")
    current_source_index = SOURCE_ARTWORK_INDEX.read_bytes() if SOURCE_ARTWORK_INDEX.exists() else b""
    if sha256_bytes(current_source_index) != manifest.get("source_artwork_index_before_sha256"):
        raise ValueError("Índice de arte-fonte mudou desde o planejamento")
    for item in manifest.get("main_artwork_assets", []):
        check_source(ROOT / item["representative_source"], item["sha256"], item["size_bytes"])
        if os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino canônico está ocupado: {item['target']}")
    for item in manifest.get("source_artwork_assets", []):
        check_source(ROOT / item["representative_source"], item["sha256"], item["size_bytes"])
        if os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino canônico de arte-fonte está ocupado: {item['target']}")
    for alias in manifest.get("main_artwork_aliases", []):
        check_source(ROOT / alias["path"], alias["sha256"], alias["size_bytes"])
    for alias in manifest.get("source_artwork_aliases", []):
        check_source(ROOT / alias["path"], alias["sha256"], alias["size_bytes"])
    for item in manifest.get("support_file_moves", []):
        check_source(ROOT / item["source"], item["sha256"], item["size_bytes"])
        if os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino de documentação está ocupado: {item['target']}")

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_json(manifest_path, manifest)
    try:
        for item in manifest.get("main_artwork_assets", []):
            source, target = ROOT / item["representative_source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for item in manifest.get("source_artwork_assets", []):
            source, target = ROOT / item["representative_source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for alias in manifest.get("main_artwork_aliases", []):
            path, target = ROOT / alias["path"], ROOT / alias["target"]
            if os.path.lexists(path):
                check_source(path, alias["sha256"], alias["size_bytes"])
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(rel_link_target(path, target))
        for alias in manifest.get("source_artwork_aliases", []):
            path, target = ROOT / alias["path"], ROOT / alias["target"]
            if os.path.lexists(path):
                check_source(path, alias["sha256"], alias["size_bytes"])
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(rel_link_target(path, target))
        for item in manifest.get("support_file_moves", []):
            source, target = ROOT / item["source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        if manifest.get("source_artwork_index_append"):
            with SOURCE_ARTWORK_INDEX.open("ab") as stream:
                if manifest["source_artwork_index_before"] and not manifest["source_artwork_index_before"].endswith("\n"):
                    stream.write(b"\n")
                stream.write(manifest["source_artwork_index_append"].encode("utf-8"))
            manifest["source_artwork_index_after_sha256"] = sha256_file(SOURCE_ARTWORK_INDEX)
        catalog = catalog_with_main_artwork(json.loads(current_catalog_bytes.decode("utf-8")), manifest)
        write_json(CATALOG_PATH, catalog)
        manifest["catalog_after_sha256"] = sha256_file(CATALOG_PATH)
        result = audit_catalog(catalog)
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["verification"] = result
        write_json(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        write_json(manifest_path, manifest)
        raise


def resume_main_artwork_operation(manifest_path: Path) -> dict[str, Any]:
    """Finish catalog/index registration after a fully materialized interrupted apply."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"failed_partial", "applying"}:
        raise ValueError(f"Manifesto não pode ser recuperado no estado {manifest.get('status')}")

    current_catalog_hash = sha256_file(CATALOG_PATH)
    if current_catalog_hash not in {manifest.get("catalog_before_sha256"), manifest.get("catalog_after_sha256")}:
        raise ValueError("Catálogo divergiu; recuperação bloqueada para preservar mudanças posteriores")

    for group_key in ("main_artwork_groups", "source_artwork_groups"):
        for group in manifest.get(group_key, []):
            target = ROOT / group["target"]
            check_target(target, group["sha256"], group["size_bytes"])
            for source in group["sources"]:
                path = ROOT / source["source"]
                if not path.is_symlink():
                    raise ValueError(f"Alias ausente após materialização; recuperação bloqueada: {relative(path)}")
                literal = path.readlink()
                expected_literal = Path(os.path.relpath(target, start=path.parent))
                if literal.is_absolute() or literal != expected_literal or path.resolve(strict=True) != target.resolve(strict=True):
                    raise ValueError(f"Alias divergente após materialização; recuperação bloqueada: {relative(path)}")
                check_target(path.resolve(strict=True), group["sha256"], group["size_bytes"])

    for item in manifest.get("support_file_moves", []):
        source, target = ROOT / item["source"], ROOT / item["target"]
        if source.exists() or not target.is_file():
            raise ValueError(f"Movimento de documento incompleto; recuperação bloqueada: {relative(source)}")
        check_target(target, item["sha256"], item["size_bytes"])

    current_index_hash = sha256_file(SOURCE_ARTWORK_INDEX) if SOURCE_ARTWORK_INDEX.exists() else sha256_bytes(b"")
    before_hash = manifest.get("source_artwork_index_before_sha256")
    after_hash = manifest.get("source_artwork_index_after_sha256")
    if current_index_hash == before_hash:
        with SOURCE_ARTWORK_INDEX.open("ab") as stream:
            baseline = manifest.get("source_artwork_index_before", "")
            if baseline and not baseline.endswith("\n"):
                stream.write(b"\n")
            stream.write(manifest.get("source_artwork_index_append", "").encode("utf-8"))
        current_index_hash = sha256_file(SOURCE_ARTWORK_INDEX)
        manifest["source_artwork_index_after_sha256"] = current_index_hash
    if current_index_hash != manifest.get("source_artwork_index_after_sha256"):
        raise ValueError("Índice de arte-fonte divergiu; recuperação bloqueada para preservar mudanças posteriores")

    baseline = manifest["catalog_before"]
    catalog = catalog_with_main_artwork(baseline, manifest)
    write_json(CATALOG_PATH, catalog)
    manifest["catalog_after_sha256"] = sha256_file(CATALOG_PATH)
    result = audit_catalog(catalog)
    manifest["status"] = "completed_verified"
    manifest["completed_at"] = now()
    manifest["recovered_at"] = now()
    manifest["recovery_method"] = "Verified all source hashes, relative aliases, payloads, and document moves; completed catalog registration without repeating file moves."
    manifest["verification"] = result
    write_json(manifest_path, manifest)
    return manifest


def rollback_main_artwork_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado não reversível: {manifest.get('status')}")
    current_catalog_hash = sha256_file(CATALOG_PATH)
    allowed_catalog_hashes = {manifest.get("catalog_before_sha256"), manifest.get("catalog_after_sha256")}
    if current_catalog_hash not in allowed_catalog_hashes:
        raise ValueError("Catálogo de assets divergiu; rollback Main bloqueado para não apagar mudanças posteriores")
    required = manifest.get("summary", {}).get("rollback_extra_space_required_bytes", 0)
    if shutil.disk_usage(ROOT).free < required:
        raise OSError(f"Espaço livre insuficiente para reversão segura; necessário ao menos {required} bytes")

    groups = manifest.get("main_artwork_groups", [])
    aliases_by_path = {item["path"]: item for item in manifest.get("main_artwork_aliases", [])}
    for group in groups:
        target = ROOT / group["target"]
        sources = group["sources"]
        if not group["reuses_existing_asset"]:
            representative = ROOT / group["representative_source"]
            if representative.is_symlink():
                literal = representative.readlink()
                if literal.is_absolute() or representative.resolve(strict=False) != target.resolve(strict=False):
                    raise ValueError(f"Alias representante divergente: {relative(representative)}")
                representative.unlink()
            elif representative.exists():
                check_source(representative, group["sha256"], group["size_bytes"])
            if target.exists():
                check_target(target, group["sha256"], group["size_bytes"])
                if not representative.exists():
                    representative.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, representative)
            elif not representative.exists():
                raise FileNotFoundError(f"Payload ausente no rollback: {group['target']}")
            restore_source = representative
        else:
            check_target(target, group["sha256"], group["size_bytes"])
            restore_source = target

        for source in sources:
            path = ROOT / source["source"]
            alias = aliases_by_path[source["source"]]
            if path.is_symlink():
                literal = path.readlink()
                expected = Path(os.path.relpath(ROOT / alias["target"], start=path.parent))
                if literal.is_absolute() or literal != expected:
                    raise ValueError(f"Symlink foi alterado; rollback bloqueado: {alias['path']}")
                path.unlink()
            elif path.exists():
                check_source(path, alias["sha256"], alias["size_bytes"])
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + f".rollback-{os.getpid()}")
            shutil.copy2(restore_source, temp)
            if sha256_file(temp) != alias["sha256"] or temp.stat().st_size != alias["size_bytes"]:
                temp.unlink(missing_ok=True)
                raise ValueError(f"Cópia de rollback divergiu: {alias['path']}")
            os.chmod(temp, alias["mode"])
            os.utime(temp, ns=(alias["mtime_ns"], alias["mtime_ns"]))
            os.replace(temp, path)

    for item in manifest.get("main_artwork_assets", []):
        target = ROOT / item["target"]
        if target.exists():
            check_target(target, item["sha256"], item["size_bytes"])
            for source in item["sources"]:
                check_source(ROOT / source["source"], item["sha256"], item["size_bytes"])
            target.unlink()

    source_aliases_by_path = {item["path"]: item for item in manifest.get("source_artwork_aliases", [])}
    for group in manifest.get("source_artwork_groups", []):
        target = ROOT / group["target"]
        sources = group["sources"]
        if not group["reuses_existing_asset"]:
            representative = ROOT / group["representative_source"]
            if representative.is_symlink():
                literal = representative.readlink()
                if literal.is_absolute() or representative.resolve(strict=False) != target.resolve(strict=False):
                    raise ValueError(f"Alias representante de arte-fonte divergente: {relative(representative)}")
                representative.unlink()
            elif representative.exists():
                check_source(representative, group["sha256"], group["size_bytes"])
            if target.exists():
                check_target(target, group["sha256"], group["size_bytes"])
                if not representative.exists():
                    representative.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, representative)
            elif not representative.exists():
                raise FileNotFoundError(f"Payload de arte-fonte ausente no rollback: {group['target']}")
            restore_source = representative
        else:
            check_target(target, group["sha256"], group["size_bytes"])
            restore_source = target

        for source in sources:
            path = ROOT / source["source"]
            alias = source_aliases_by_path[source["source"]]
            if path.is_symlink():
                literal = path.readlink()
                expected = Path(os.path.relpath(ROOT / alias["target"], start=path.parent))
                if literal.is_absolute() or literal != expected:
                    raise ValueError(f"Symlink de arte-fonte foi alterado; rollback bloqueado: {alias['path']}")
                path.unlink()
            elif path.exists():
                check_source(path, alias["sha256"], alias["size_bytes"])
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + f".rollback-{os.getpid()}")
            shutil.copy2(restore_source, temp)
            if sha256_file(temp) != alias["sha256"] or temp.stat().st_size != alias["size_bytes"]:
                temp.unlink(missing_ok=True)
                raise ValueError(f"Cópia de rollback de arte-fonte divergiu: {alias['path']}")
            os.chmod(temp, alias["mode"])
            os.utime(temp, ns=(alias["mtime_ns"], alias["mtime_ns"]))
            os.replace(temp, path)

    for item in manifest.get("source_artwork_assets", []):
        target = ROOT / item["target"]
        if target.exists():
            check_target(target, item["sha256"], item["size_bytes"])
            for source in item["sources"]:
                check_source(ROOT / source["source"], item["sha256"], item["size_bytes"])
            target.unlink()

    for item in reversed(manifest.get("support_file_moves", [])):
        source, target = ROOT / item["source"], ROOT / item["target"]
        if source.exists() and target.exists():
            raise FileExistsError(f"Rollback de documentação bloquearia sobrescrita: {relative(source)} e {relative(target)}")
        if target.exists():
            check_source(target, item["sha256"], item["size_bytes"])
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
        if not source.exists():
            raise FileNotFoundError(f"Arquivo de documentação ausente no rollback: {relative(source)}")
        check_source(source, item["sha256"], item["size_bytes"])

    current_source_index_hash = sha256_file(SOURCE_ARTWORK_INDEX) if SOURCE_ARTWORK_INDEX.exists() else sha256_bytes(b"")
    allowed_source_index_hashes = {
        manifest.get("source_artwork_index_before_sha256"),
        manifest.get("source_artwork_index_after_sha256"),
    }
    if current_source_index_hash not in allowed_source_index_hashes:
        raise ValueError("Índice de arte-fonte divergiu; rollback bloqueado para preservar alterações posteriores")
    baseline_index = manifest.get("source_artwork_index_before", "").encode("utf-8")
    if baseline_index:
        SOURCE_ARTWORK_INDEX.write_bytes(baseline_index)
    else:
        SOURCE_ARTWORK_INDEX.unlink(missing_ok=True)

    baseline = manifest["catalog_before"]
    write_json(CATALOG_PATH, baseline)
    if sha256_file(CATALOG_PATH) != manifest["catalog_before_sha256"]:
        raise ValueError("O catálogo restaurado não reproduziu o SHA-256 anterior")
    result = audit_catalog(baseline)
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["rollback_verification"] = result
    write_json(manifest_path, manifest)
    return manifest


def audit_catalog(catalog: dict[str, Any], *, allow_staged_main_intake: bool = False) -> dict[str, Any]:
    expected_assets = {item["path"]: item for item in catalog.get("canonical_assets", [])}
    if len(expected_assets) != len(catalog.get("canonical_assets", [])):
        raise ValueError("Catálogo possui caminhos canônicos repetidos")
    if len({item["sha256"] for item in expected_assets.values()}) != len(expected_assets):
        raise ValueError("Catálogo possui mais de um payload regular com o mesmo SHA-256")

    actual_files: dict[str, Path] = {}
    actual_shared_links: set[str] = set()
    for path in SHARED.rglob("*"):
        if path.is_symlink():
            actual_shared_links.add(relative(path))
        elif path.is_file():
            actual_files[relative(path)] = path
    if set(actual_files) != set(expected_assets):
        missing = sorted(set(expected_assets) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected_assets))
        raise ValueError(f"Árvore Assets/shared diverge do catálogo; ausentes={missing[:5]}, extras={extra[:5]}")
    if actual_shared_links:
        raise ValueError(f"Symlink não catalogado dentro de Assets/shared: {sorted(actual_shared_links)[:5]}")
    for relative_path, item in expected_assets.items():
        check_target(ROOT / relative_path, item["sha256"], item["size_bytes"])

    expected_aliases = {item["path"]: item for item in catalog.get("layout_aliases", [])}
    if len(expected_aliases) != len(catalog.get("layout_aliases", [])):
        raise ValueError("Catálogo possui aliases de layout repetidos")
    actual_aliases: set[str] = set()
    regular_common_files: list[str] = []
    for theme in (p for p in LAYOUTS.iterdir() if p.is_dir()):
        base = theme / "collections" / "_common" / "medium_artwork"
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_symlink():
                actual_aliases.add(relative(path))
            elif path.is_file():
                regular_common_files.append(relative(path))
    if catalog.get("mode") == "deduplicated":
        if regular_common_files:
            raise ValueError(f"Artwork comum ainda duplicado como arquivo regular: {regular_common_files[:5]}")
        if actual_aliases != set(expected_aliases):
            missing = sorted(set(expected_aliases) - actual_aliases)
            extra = sorted(actual_aliases - set(expected_aliases))
            raise ValueError(f"Aliases de layout divergem do catálogo; ausentes={missing[:5]}, extras={extra[:5]}")
        for alias_path, item in expected_aliases.items():
            path = ROOT / alias_path
            target = ROOT / item["target"]
            if item["target"] not in expected_assets:
                raise ValueError(f"Alias aponta para destino ausente do catálogo: {alias_path} -> {item['target']}")
            if not path.is_symlink():
                raise ValueError(f"Alias não é symlink: {alias_path}")
            literal = path.readlink()
            if literal.is_absolute():
                raise ValueError(f"Symlink absoluto não portável: {alias_path} -> {literal}")
            expected_literal = Path(os.path.relpath(target, start=path.parent))
            if literal != expected_literal:
                raise ValueError(f"Alvo relativo inesperado: {alias_path} -> {literal}; esperado {expected_literal}")
            resolved = path.resolve(strict=True)
            if not inside_root(resolved) or resolved != target.resolve(strict=True):
                raise ValueError(f"Alias sai do projeto ou aponta para outro destino: {alias_path}")
            check_target(resolved, item["sha256"], item["size_bytes"])
        root_images = [p for p in STATIC_ROOT.iterdir() if p.is_file() and not p.is_symlink()]
        if root_images:
            raise ValueError(f"Imagens sem subcategoria em Assets/shared/images/: {[p.name for p in root_images[:5]]}")
    expected_static_aliases = {item["path"]: item for item in catalog.get("static_layout_aliases", [])}
    if len(expected_static_aliases) != len(catalog.get("static_layout_aliases", [])):
        raise ValueError("Catálogo possui aliases estáticos de layout repetidos")
    actual_static_aliases: set[str] = set()
    for path in LAYOUTS.rglob("*"):
        if not path.is_symlink() or path.suffix.casefold() not in LAYOUT_MEDIA_EXTENSIONS:
            continue
        parts = {part.casefold() for part in path.relative_to(LAYOUTS).parts}
        if "medium_artwork" in parts:
            continue
        actual_static_aliases.add(relative(path))
    if actual_static_aliases != set(expected_static_aliases):
        missing = sorted(set(expected_static_aliases) - actual_static_aliases)
        extra = sorted(actual_static_aliases - set(expected_static_aliases))
        raise ValueError(f"Aliases estáticos de layout divergem do catálogo; ausentes={missing[:5]}, extras={extra[:5]}")
    for alias_path, item in expected_static_aliases.items():
        path = ROOT / alias_path
        target = ROOT / item["target"]
        if item["target"] not in expected_assets:
            raise ValueError(f"Alias estático aponta para destino ausente do catálogo: {alias_path} -> {item['target']}")
        if not path.is_symlink():
            raise ValueError(f"Alias estático não é symlink: {alias_path}")
        literal = path.readlink()
        if literal.is_absolute():
            raise ValueError(f"Symlink estático absoluto não portável: {alias_path} -> {literal}")
        expected_literal = Path(os.path.relpath(target, start=path.parent))
        if literal != expected_literal:
            raise ValueError(f"Alvo relativo estático inesperado: {alias_path} -> {literal}; esperado {expected_literal}")
        resolved = path.resolve(strict=True)
        if not inside_root(resolved) or resolved != target.resolve(strict=True):
            raise ValueError(f"Alias estático sai do projeto ou aponta para outro destino: {alias_path}")
        check_target(resolved, item["sha256"], item["size_bytes"])
    layout_media_duplicate_groups: set[str] = set()
    layout_shared_collisions: list[str] = []
    if catalog.get("static_layout_assets_migrated"):
        media_hashes: dict[str, str] = {}
        for path in layout_media_files():
            digest = sha256_file(path)
            rel = relative(path)
            if digest in media_hashes:
                layout_media_duplicate_groups.add(digest)
            media_hashes[digest] = rel
            if digest in expected_assets:
                layout_shared_collisions.append(rel)
        legacy_sources = layout_legacy_numberplayer_files()
        if legacy_sources:
            raise ValueError(f"Arte numberplayers fora de medium_artwork ainda existe: {[relative(p) for p in legacy_sources[:5]]}")
        if layout_media_duplicate_groups:
            raise ValueError(f"Assets regulares de layout ainda têm hashes duplicados: {len(layout_media_duplicate_groups)} grupos")
        if layout_shared_collisions:
            raise ValueError(f"Assets regulares de layout duplicam Assets/shared: {layout_shared_collisions[:5]}")
    expected_main_aliases = {item["path"]: item for item in catalog.get("main_artwork_aliases", [])}
    if len(expected_main_aliases) != len(catalog.get("main_artwork_aliases", [])):
        raise ValueError("Catálogo possui aliases Main repetidos")
    expected_source_aliases = {item["path"]: item for item in catalog.get("source_artwork_aliases", [])}
    if len(expected_source_aliases) != len(catalog.get("source_artwork_aliases", [])):
        raise ValueError("Catálogo possui aliases de arte-fonte Main repetidos")
    expected_source_assets = {item["path"]: item for item in catalog.get("source_artwork_assets", [])}
    if len(expected_source_assets) != len(catalog.get("source_artwork_assets", [])):
        raise ValueError("Catálogo possui assets de arte-fonte repetidos")
    if catalog.get("source_artwork_assets") is not None:
        if len({item["sha256"] for item in expected_source_assets.values()}) != len(expected_source_assets):
            raise ValueError("Assets/source-artwork possui hashes canônicos duplicados")
        source_hashes = {item["sha256"] for item in expected_source_assets.values()}
        if source_hashes.intersection(item["sha256"] for item in expected_assets.values()):
            raise ValueError("O catálogo repete payload entre Assets/shared e Assets/source-artwork")
        actual_source_assets = {
            relative(path): path for path in SOURCE_ARTWORK_ROOT.rglob("*")
            if path.is_file() and not path.is_symlink()
        } if SOURCE_ARTWORK_ROOT.is_dir() else {}
        if set(actual_source_assets) != set(expected_source_assets):
            missing = sorted(set(expected_source_assets) - set(actual_source_assets))
            extra = sorted(set(actual_source_assets) - set(expected_source_assets))
            raise ValueError(f"Assets/source-artwork diverge do catálogo; ausentes={missing[:5]}, extras={extra[:5]}")
        for source_path, item in expected_source_assets.items():
            check_target(ROOT / source_path, item["sha256"], item["size_bytes"])
    else:
        actual_source_assets = {}
    actual_main_aliases: set[str] = set()
    for path in MAIN_ARTWORK_ROOT.rglob("*") if MAIN_ARTWORK_ROOT.is_dir() else []:
        if path.is_symlink():
            actual_main_aliases.add(relative(path))
    if "main_artwork_aliases" in catalog or "source_artwork_aliases" in catalog:
        expected_all_main_aliases = set(expected_main_aliases) | set(expected_source_aliases)
        if actual_main_aliases != expected_all_main_aliases:
            missing = sorted(expected_all_main_aliases - actual_main_aliases)
            extra = sorted(actual_main_aliases - expected_all_main_aliases)
            raise ValueError(f"Aliases Main divergem do catálogo; ausentes={missing[:5]}, extras={extra[:5]}")
        canonical_hashes = {item["sha256"] for item in expected_assets.values()}
        all_main_aliases = expected_all_main_aliases
        regular_main_files = main_media_files(known_aliases=all_main_aliases)
        regular_source_files = main_source_artwork_files(known_aliases=all_main_aliases)
        regular_hashes: dict[str, str] = {}
        duplicate_main_groups: set[str] = set()
        shared_collisions: list[str] = []
        for path in regular_main_files:
            digest = sha256_file(path)
            rel = relative(path)
            if digest in regular_hashes:
                duplicate_main_groups.add(digest)
            regular_hashes[digest] = rel
            if digest in canonical_hashes:
                shared_collisions.append(rel)
        # The semantic asset planner ingests regular files from Main and groups
        # them by SHA-256 before replacing them with relative aliases. During
        # that planning phase, staged duplicate payloads and copies of already
        # canonical assets are expected; final verification keeps the strict
        # default and rejects both conditions.
        if duplicate_main_groups and not allow_staged_main_intake:
            raise ValueError(f"Artwork Main ainda contém hashes regulares duplicados: {len(duplicate_main_groups)} grupos")
        if shared_collisions and not allow_staged_main_intake:
            raise ValueError(f"Artwork Main ainda duplica payloads de Assets/shared: {shared_collisions[:5]}")
        source_hashes = {item["sha256"] for item in expected_source_assets.values()}
        regular_source_hashes: dict[str, str] = {}
        duplicate_source_groups: set[str] = set()
        source_collisions = []
        for path in regular_source_files:
            digest = sha256_file(path)
            rel = relative(path)
            if digest in regular_source_hashes:
                duplicate_source_groups.add(digest)
            regular_source_hashes[digest] = rel
            if digest in canonical_hashes or digest in source_hashes:
                source_collisions.append(rel)
        if duplicate_source_groups:
            raise ValueError(f"Arte-fonte Main ainda contém hashes regulares duplicados: {len(duplicate_source_groups)} grupos")
        if source_collisions:
            raise ValueError(f"Arte-fonte Main ainda duplica payloads canônicos: {source_collisions[:5]}")
        for alias_path, item in expected_main_aliases.items():
            path = ROOT / alias_path
            target = ROOT / item["target"]
            if item["target"] not in expected_assets:
                raise ValueError(f"Alias Main aponta para destino fora do catálogo: {alias_path} -> {item['target']}")
            if not path.is_symlink():
                raise ValueError(f"Alias Main não é symlink: {alias_path}")
            literal = path.readlink()
            if literal.is_absolute():
                raise ValueError(f"Symlink Main absoluto não portável: {alias_path} -> {literal}")
            expected_literal = Path(os.path.relpath(target, start=path.parent))
            if literal != expected_literal:
                raise ValueError(f"Alvo relativo inesperado: {alias_path} -> {literal}; esperado {expected_literal}")
            resolved = path.resolve(strict=True)
            if not inside_root(resolved) or resolved != target.resolve(strict=True):
                raise ValueError(f"Alias Main sai do projeto ou aponta para outro destino: {alias_path}")
            check_target(resolved, item["sha256"], item["size_bytes"])
        expected_targets = set(expected_assets) | set(expected_source_assets)
        for alias_path, item in expected_source_aliases.items():
            path = ROOT / alias_path
            target = ROOT / item["target"]
            if item["target"] not in expected_targets:
                raise ValueError(f"Alias de arte-fonte aponta para destino fora do catálogo: {alias_path} -> {item['target']}")
            if not path.is_symlink():
                raise ValueError(f"Alias de arte-fonte não é symlink: {alias_path}")
            literal = path.readlink()
            if literal.is_absolute():
                raise ValueError(f"Symlink de arte-fonte absoluto não portável: {alias_path} -> {literal}")
            expected_literal = Path(os.path.relpath(target, start=path.parent))
            if literal != expected_literal:
                raise ValueError(f"Alvo relativo inesperado de arte-fonte: {alias_path} -> {literal}; esperado {expected_literal}")
            resolved = path.resolve(strict=True)
            if not inside_root(resolved) or resolved != target.resolve(strict=True):
                raise ValueError(f"Alias de arte-fonte sai do projeto ou aponta para outro destino: {alias_path}")
            check_target(resolved, item["sha256"], item["size_bytes"])
    else:
        regular_main_files = []
        duplicate_main_groups = set()
    return {
        "canonical_assets": len(expected_assets),
        "unique_sha256": len({item["sha256"] for item in expected_assets.values()}),
        "layout_aliases": len(expected_aliases),
        "actual_layout_aliases": len(actual_aliases),
        "regular_common_files": len(regular_common_files),
        "main_artwork_aliases": len(expected_main_aliases),
        "source_artwork_aliases": len(expected_source_aliases),
        "source_artwork_assets": len(expected_source_assets),
        "actual_main_artwork_aliases": len(actual_main_aliases),
        "static_layout_aliases": len(expected_static_aliases),
        "actual_static_layout_aliases": len(actual_static_aliases),
        "layout_media_duplicate_hash_groups": len(layout_media_duplicate_groups),
        "layout_media_shared_collisions": len(layout_shared_collisions),
        "regular_main_media_files": len(regular_main_files),
        "duplicate_main_media_hash_groups": len(duplicate_main_groups),
        "actual_source_artwork_assets": len(actual_source_assets),
        "mode": catalog.get("mode"),
    }


def is_archived_layout_asset(path: Path) -> bool:
    relative_parts = path.relative_to(LAYOUTS).parts
    directory_parts = {part.casefold() for part in relative_parts[:-1]}
    return bool(directory_parts.intersection({"_layout_bkp", "copyofxml", "backup", "backups", "bkp"}))


def layout_legacy_numberplayer_files() -> list[Path]:
    files: list[Path] = []
    for theme in sorted((path for path in LAYOUTS.iterdir() if path.is_dir()), key=lambda path: path.name.casefold()):
        base = theme / "collections" / "_common" / "numberplayers"
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
            if path.is_symlink():
                raise ValueError(f"Symlink inesperado no caminho legado numberplayers: {relative(path)}")
            if path.is_file():
                files.append(path)
    return files


def layout_media_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(LAYOUTS.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in LAYOUT_MEDIA_EXTENSIONS:
            continue
        parts = tuple(part.casefold() for part in path.relative_to(LAYOUTS).parts)
        if is_archived_layout_asset(path) or "medium_artwork" in parts:
            continue
        # `_setup` is theme authoring/documentation material, not a runtime
        # asset. Game samples inside it remain with their template and are
        # handled by the separate game-media operation.
        if "_setup" in parts:
            continue
        if len(parts) >= 5 and parts[1:4] == ("collections", "_common", "numberplayers"):
            continue
        files.append(path)
    return files


def layout_static_category(sources: list[dict[str, Any]]) -> str:
    keys = {normalized_key(source["logical_name"]) for source in sources}
    if keys and keys.issubset(LAYOUT_COLLECTION_ART_KEYS):
        return "collection-artwork/collection-menu"
    categories = {STATIC_CATEGORIES[key] for key in keys if key in STATIC_CATEGORIES}
    if len(categories) == 1 and len(categories) == len({STATIC_CATEGORIES.get(key) for key in keys}):
        return next(iter(categories))
    raise ValueError(f"Asset de layout duplicado sem categoria semântica explícita: {sorted(keys)}")


def layout_static_destination(category: str, logical_name: str, digest: str, extension: str) -> str:
    filename = f"{slug(logical_name)}--{digest[:12]}{extension.casefold()}"
    if category.startswith("collection-artwork/"):
        return f"Assets/shared/{category}/{digest[:2]}/{filename}"
    return f"Assets/shared/{category}/{filename}"


def plan_layout_media_operation(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    catalog_before_bytes = CATALOG_PATH.read_bytes()
    catalog_before = json.loads(catalog_before_bytes.decode("utf-8"))
    audit_catalog(catalog_before)
    canonical_by_hash = {item["sha256"]: item for item in catalog_before.get("canonical_assets", [])}
    if len(canonical_by_hash) != len(catalog_before.get("canonical_assets", [])):
        raise ValueError("Catálogo atual tem mais de um destino canônico para o mesmo hash")
    existing_aliases = {item["path"]: item for item in catalog_before.get("static_layout_aliases", [])}
    if len(existing_aliases) != len(catalog_before.get("static_layout_aliases", [])):
        raise ValueError("Catálogo atual possui aliases estáticos repetidos")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    media_files = layout_media_files()
    for path in media_files:
        before = snapshot(path)
        grouped[before["sha256"]].append({
            "source": relative(path),
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "mode": before["mode"],
            "mtime_ns": before["mtime_ns"],
            "device": before["device"],
            "inode": before["inode"],
            "logical_name": path.name,
            "theme": path.relative_to(LAYOUTS).parts[0],
        })

    layout_static_groups: list[dict[str, Any]] = []
    layout_static_aliases: list[dict[str, Any]] = []
    planned_targets: dict[str, str] = {}
    for digest, sources in sorted(grouped.items()):
        existing = canonical_by_hash.get(digest)
        if existing is None and len(sources) < 2:
            continue
        sources.sort(key=lambda item: item["source"].casefold())
        sizes = {item["size_bytes"] for item in sources}
        extensions = {Path(item["logical_name"]).suffix.casefold() for item in sources}
        if len(sizes) != 1 or len(extensions) != 1:
            raise ValueError(f"Arquivos do mesmo hash têm tamanho ou extensão inconsistente: {digest}")
        names = Counter(Path(item["logical_name"]).stem for item in sources)
        logical_name = sorted(names, key=lambda name: (-names[name], name.casefold(), name))[0]
        if existing:
            target_rel = existing["path"]
            category = existing.get("category", "shared-existing")
            check_target(ROOT / target_rel, digest, sources[0]["size_bytes"])
            reused = True
        else:
            category = layout_static_category(sources)
            target_rel = layout_static_destination(category, logical_name, digest, next(iter(extensions)))
            if target_rel in planned_targets and planned_targets[target_rel] != digest:
                raise FileExistsError(f"Colisão entre assets planejados: {target_rel}")
            if os.path.lexists(ROOT / target_rel):
                raise FileExistsError(f"Destino canônico já existe fora do catálogo: {target_rel}")
            nearest = ROOT / target_rel
            while not nearest.exists():
                nearest = nearest.parent
            if nearest.stat().st_dev != (ROOT / sources[0]["source"]).stat().st_dev:
                raise OSError(f"Centralização cruzaria dispositivos: {sources[0]['source']} -> {target_rel}")
            planned_targets[target_rel] = digest
            reused = False
        group = {
            "sha256": digest,
            "size_bytes": sources[0]["size_bytes"],
            "target": target_rel,
            "category": category,
            "logical_name": logical_name,
            "source_count": len(sources),
            "reuses_existing_asset": reused,
            "representative_source": None if reused else sources[0]["source"],
            "sources": sources,
        }
        layout_static_groups.append(group)
        for source in sources:
            alias = {
                "path": source["source"],
                "target": target_rel,
                "sha256": digest,
                "size_bytes": source["size_bytes"],
                "category": category,
                "logical_name": source["logical_name"],
                "theme": source["theme"],
                "mode": source["mode"],
                "mtime_ns": source["mtime_ns"],
            }
            if alias["path"] in existing_aliases:
                raise ValueError(f"Alias estático já está registrado e não é uma fonte regular: {alias['path']}")
            layout_static_aliases.append(alias)

    existing_layout_aliases = {item["path"]: item for item in catalog_before.get("layout_aliases", [])}
    legacy_numberplayer_moves: list[dict[str, Any]] = []
    for path in layout_legacy_numberplayer_files():
        if path.suffix.casefold() != ".png":
            raise ValueError(f"Arquivo inesperado no diretório legado numberplayers: {relative(path)}")
        before = snapshot(path)
        canonical = canonical_by_hash.get(before["sha256"])
        if canonical is None or not canonical["path"].startswith("Assets/shared/collection-artwork/players/"):
            raise ValueError(f"Arte numberplayers sem payload canônico exato: {relative(path)}")
        theme = path.relative_to(LAYOUTS).parts[0]
        alias_rel = (path.parent.parent / "medium_artwork" / "numberplayers" / path.name).relative_to(ROOT).as_posix()
        alias_path = ROOT / alias_rel
        existing_alias = existing_layout_aliases.get(alias_rel)
        create_alias = existing_alias is None
        if create_alias and os.path.lexists(alias_path):
            raise FileExistsError(f"Destino de artwork comum existe fora do catálogo: {alias_rel}")
        if create_alias:
            nearest = alias_path.parent
            while not nearest.exists():
                nearest = nearest.parent
            if nearest.stat().st_dev != path.stat().st_dev:
                raise OSError(f"Alias de numberplayers cruzaria dispositivos: {relative(path)} -> {alias_rel}")
        elif not alias_path.is_symlink():
            raise ValueError(f"Alias numberplayers existente diverge do catálogo: {alias_rel}")
        if not create_alias:
            check_target(ROOT / existing_alias["target"], existing_alias["sha256"], existing_alias["size_bytes"])
        legacy_numberplayer_moves.append({
            "source": relative(path),
            "alias_path": alias_rel,
            "target": canonical["path"],
            "sha256": before["sha256"],
            "size_bytes": before["size_bytes"],
            "mode": before["mode"],
            "mtime_ns": before["mtime_ns"],
            "device": before["device"],
            "inode": before["inode"],
            "theme": theme,
            "logical_name": path.name,
            "source_type": "numberplayers",
            "semantic_type": "players",
            "source_variant_path": "numberplayers",
            "create_alias": create_alias,
        })

    aliases_to_add = [
        {
            "path": item["alias_path"],
            "target": item["target"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "source_type": item["source_type"],
            "semantic_type": item["semantic_type"],
            "theme": item["theme"],
            "logical_name": item["logical_name"],
            "source_variant_path": item["source_variant_path"],
        }
        for item in legacy_numberplayer_moves if item["create_alias"]
    ]
    if len({item["path"] for item in aliases_to_add}) != len(aliases_to_add):
        raise ValueError("O plano tem caminhos repetidos para aliases numberplayers")

    manifest = {
        "schema_version": 1,
        "operation_id": f"retrofe-layout-media-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "kind": "retrofe_layout_media_deduplication",
        "status": "planned",
        "created_at": now(),
        "project_root": ".",
        "method": "SHA-256 exato; arquivos estáticos repetidos apontam para Assets/shared; artwork numberplayers passa ao lookup documentado medium_artwork.",
        "historical_source": "Manifests/asset-sharing.json e Manifests/asset-catalog.json; fontes locais preservadas no pacote pré-canônico.",
        "catalog_before_sha256": sha256_bytes(catalog_before_bytes),
        "catalog_before_zlib_base64": base64.b64encode(zlib.compress(catalog_before_bytes, level=9)).decode("ascii"),
        "layout_static_groups": layout_static_groups,
        "layout_static_aliases": layout_static_aliases,
        "legacy_numberplayer_moves": legacy_numberplayer_moves,
        "layout_aliases_to_add": aliases_to_add,
        "summary": {
            "layout_media_files_scanned": len(media_files),
            "static_layout_aliases": len(layout_static_aliases),
            "static_layout_hash_groups": len(layout_static_groups),
            "new_canonical_assets": sum(1 for item in layout_static_groups if not item["reuses_existing_asset"]),
            "reused_canonical_assets": sum(1 for item in layout_static_groups if item["reuses_existing_asset"]),
            "legacy_numberplayer_files_to_retire": len(legacy_numberplayer_moves),
            "new_medium_artwork_aliases": len(aliases_to_add),
            "duplicate_layout_payload_copies_to_replace": sum(max(0, len(item["sources"]) - 1) for item in layout_static_groups),
        },
        "rollback": {
            "command": "python3 Tools/materializers/reorganize-shared-assets.py rollback-layout-media --manifest Manifests/<this-file>",
            "method": "Restaurar cada arquivo regular a partir do destino canônico com hash conferido, remover os aliases criados e repor exatamente o catálogo registrado antes da operação.",
            "permanent_deletion": False,
        },
    }
    write_json(manifest_path, manifest)
    return manifest


def catalog_with_layout_media(catalog_before: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    catalog = json.loads(json.dumps(catalog_before))
    assets = list(catalog.get("canonical_assets", []))
    asset_paths = {item["path"] for item in assets}
    asset_hashes = {item["sha256"] for item in assets}
    for group in manifest.get("layout_static_groups", []):
        if group["reuses_existing_asset"]:
            continue
        if group["target"] in asset_paths or group["sha256"] in asset_hashes:
            raise ValueError(f"Asset novo colide com o catálogo anterior: {group['target']}")
        assets.append({
            "path": group["target"],
            "sha256": group["sha256"],
            "size_bytes": group["size_bytes"],
            "asset_class": "static-layout-asset",
            "category": group["category"],
            "logical_name": group["logical_name"],
            "source_paths": [item["source"] for item in group["sources"]],
            "layout_alias_count": len(group["sources"]),
        })
        asset_paths.add(group["target"])
        asset_hashes.add(group["sha256"])
    catalog["canonical_assets"] = sorted(assets, key=lambda item: item["path"].casefold())
    aliases = list(catalog.get("layout_aliases", []))
    known_alias_paths = {item["path"] for item in aliases}
    for alias in manifest.get("layout_aliases_to_add", []):
        if alias["path"] in known_alias_paths:
            raise ValueError(f"Alias commonlayout colide com catálogo existente: {alias['path']}")
        aliases.append(alias)
        known_alias_paths.add(alias["path"])
    catalog["layout_aliases"] = sorted(aliases, key=lambda item: item["path"].casefold())
    static_aliases = list(catalog.get("static_layout_aliases", []))
    known_static_paths = {item["path"] for item in static_aliases}
    for item in manifest.get("layout_static_aliases", []):
        alias = {key: item[key] for key in ("path", "target", "sha256", "size_bytes", "category", "logical_name", "theme")}
        if alias["path"] in known_static_paths:
            raise ValueError(f"Alias estático colide com catálogo existente: {alias['path']}")
        static_aliases.append(alias)
        known_static_paths.add(alias["path"])
    catalog["static_layout_aliases"] = sorted(static_aliases, key=lambda item: item["path"].casefold())
    catalog["static_layout_assets_migrated"] = True
    catalog["static_layout_manifest"] = manifest.get("manifest_path", "Manifests/layout-media-reorganization.json")
    catalog["updated_at"] = now()
    catalog.setdefault("storage_rules", {})
    catalog["storage_rules"]["static_layout_assets"] = "One regular payload per SHA-256 in Assets/shared; referenced layout paths use relative aliases recorded in static_layout_aliases."
    catalog["storage_rules"]["static_collection_menu_art"] = "Assets/shared/collection-artwork/collection-menu/<sha256-prefix>/<logical-name>--<sha256-prefix>.<ext>"
    catalog["storage_rules"]["common_numberplayers"] = "layouts/<theme>/collections/_common/medium_artwork/numberplayers/<item>.png; legacy collections/_common/numberplayers paths are not a runtime lookup."
    catalog.setdefault("summary", {})
    catalog["summary"]["canonical_assets"] = len(catalog["canonical_assets"])
    catalog["summary"]["unique_sha256"] = len({item["sha256"] for item in catalog["canonical_assets"]})
    catalog["summary"]["relative_layout_aliases"] = len(catalog["layout_aliases"])
    catalog["summary"]["static_layout_aliases"] = len(catalog["static_layout_aliases"])
    catalog["summary"]["category_counts"] = dict(sorted(Counter(item.get("category", "other") for item in catalog["canonical_assets"]).items()))
    return catalog


def restore_copy_from_target(source: Path, target: Path, expected_hash: str, expected_size: int, mode: int, mtime_ns: int) -> None:
    if source.is_symlink():
        raise ValueError(f"Não sobrescrever symlink durante rollback: {relative(source)}")
    if source.exists():
        check_source(source, expected_hash, expected_size)
        return
    check_target(target, expected_hash, expected_size)
    source.parent.mkdir(parents=True, exist_ok=True)
    temp = source.with_name(source.name + f".rollback-{os.getpid()}")
    shutil.copy2(target, temp)
    if sha256_file(temp) != expected_hash or temp.stat().st_size != expected_size:
        temp.unlink(missing_ok=True)
        raise ValueError(f"Cópia de rollback divergiu: {relative(source)}")
    os.chmod(temp, mode)
    os.utime(temp, ns=(mtime_ns, mtime_ns))
    os.replace(temp, source)


def apply_layout_media_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "planned":
        raise ValueError(f"Manifesto precisa estar planned, estado atual: {manifest.get('status')}")
    manifest["manifest_path"] = relative(manifest_path)
    if sha256_file(CATALOG_PATH) != manifest["catalog_before_sha256"]:
        raise ValueError("Catálogo mudou desde o planejamento; aplicação bloqueada")
    for group in manifest["layout_static_groups"]:
        for source in group["sources"]:
            check_source(ROOT / source["source"], group["sha256"], group["size_bytes"])
        target = ROOT / group["target"]
        if group["reuses_existing_asset"]:
            check_target(target, group["sha256"], group["size_bytes"])
        elif os.path.lexists(target):
            raise FileExistsError(f"Destino canônico ocupado: {group['target']}")
    for item in manifest["legacy_numberplayer_moves"]:
        check_source(ROOT / item["source"], item["sha256"], item["size_bytes"])
        check_target(ROOT / item["target"], item["sha256"], item["size_bytes"])
        if item["create_alias"] and os.path.lexists(ROOT / item["alias_path"]):
            raise FileExistsError(f"Destino de artwork comum ocupado: {item['alias_path']}")

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_json(manifest_path, manifest)
    try:
        for group in manifest["layout_static_groups"]:
            if group["reuses_existing_asset"]:
                continue
            source, target = ROOT / group["representative_source"], ROOT / group["target"]
            check_source(source, group["sha256"], group["size_bytes"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for alias in manifest["layout_static_aliases"]:
            path, target = ROOT / alias["path"], ROOT / alias["target"]
            check_target(target, alias["sha256"], alias["size_bytes"])
            if os.path.lexists(path):
                check_source(path, alias["sha256"], alias["size_bytes"])
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(rel_link_target(path, target))
        for item in manifest["legacy_numberplayer_moves"]:
            source, alias_path, target = ROOT / item["source"], ROOT / item["alias_path"], ROOT / item["target"]
            check_source(source, item["sha256"], item["size_bytes"])
            check_target(target, item["sha256"], item["size_bytes"])
            if item["create_alias"]:
                alias_path.parent.mkdir(parents=True, exist_ok=True)
                alias_path.symlink_to(rel_link_target(alias_path, target))
            source.unlink()
        removed_dirs: list[str] = []
        for item in manifest["legacy_numberplayer_moves"]:
            legacy_dir = (ROOT / item["source"]).parent
            if legacy_dir.exists() and not any(legacy_dir.iterdir()):
                legacy_dir.rmdir()
                removed_dirs.append(relative(legacy_dir))
        manifest["removed_legacy_directories"] = sorted(set(removed_dirs))

        catalog_before = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        if sha256_bytes(zlib.decompress(base64.b64decode(manifest["catalog_before_zlib_base64"]))) != manifest["catalog_before_sha256"]:
            raise ValueError("Snapshot do catálogo no manifesto está corrompido")
        catalog = catalog_with_layout_media(catalog_before, manifest)
        write_json(CATALOG_PATH, catalog)
        result = audit_catalog(catalog)
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["catalog_after_sha256"] = sha256_file(CATALOG_PATH)
        manifest["verification"] = result
        write_json(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        write_json(manifest_path, manifest)
        raise


def rollback_layout_media_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado não reversível: {manifest.get('status')}")
    after_hash = manifest.get("catalog_after_sha256")
    if after_hash and sha256_file(CATALOG_PATH) not in {manifest.get("catalog_before_sha256"), after_hash}:
        raise ValueError("Catálogo divergiu após a operação; rollback bloqueado para preservar mudanças posteriores")
    for alias in manifest.get("layout_static_aliases", []):
        path, target = ROOT / alias["path"], ROOT / alias["target"]
        if path.is_symlink():
            expected = Path(os.path.relpath(target, start=path.parent))
            if path.readlink().is_absolute() or path.readlink() != expected:
                raise ValueError(f"Alias estático alterado; rollback bloqueado: {alias['path']}")
            check_target(target, alias["sha256"], alias["size_bytes"])
            path.unlink()
        elif path.exists():
            check_source(path, alias["sha256"], alias["size_bytes"])
    for alias in manifest.get("layout_static_aliases", []):
        restore_copy_from_target(ROOT / alias["path"], ROOT / alias["target"], alias["sha256"], alias["size_bytes"], alias["mode"], alias["mtime_ns"])
    for item in manifest.get("legacy_numberplayer_moves", []):
        alias_path, target, source = ROOT / item["alias_path"], ROOT / item["target"], ROOT / item["source"]
        if item.get("create_alias") and alias_path.is_symlink():
            expected = Path(os.path.relpath(target, start=alias_path.parent))
            if alias_path.readlink().is_absolute() or alias_path.readlink() != expected:
                raise ValueError(f"Alias numberplayers alterado; rollback bloqueado: {item['alias_path']}")
            check_target(target, item["sha256"], item["size_bytes"])
            alias_path.unlink()
        elif item.get("create_alias") and alias_path.exists():
            check_source(alias_path, item["sha256"], item["size_bytes"])
        restore_copy_from_target(source, target, item["sha256"], item["size_bytes"], item["mode"], item["mtime_ns"])
    for group in reversed(manifest.get("layout_static_groups", [])):
        if group["reuses_existing_asset"]:
            continue
        target = ROOT / group["target"]
        if not target.exists():
            continue
        check_target(target, group["sha256"], group["size_bytes"])
        for source in group["sources"]:
            check_source(ROOT / source["source"], group["sha256"], group["size_bytes"])
        target.unlink()

    if sha256_file(CATALOG_PATH) != manifest.get("catalog_before_sha256"):
        before_bytes = zlib.decompress(base64.b64decode(manifest["catalog_before_zlib_base64"]))
        if sha256_bytes(before_bytes) != manifest["catalog_before_sha256"]:
            raise ValueError("Snapshot do catálogo no manifesto está corrompido")
        temp = CATALOG_PATH.with_name(CATALOG_PATH.name + f".rollback-{os.getpid()}")
        temp.write_bytes(before_bytes)
        os.replace(temp, CATALOG_PATH)
    baseline = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if sha256_file(CATALOG_PATH) != manifest["catalog_before_sha256"]:
        raise ValueError("Rollback não reproduziu o catálogo registrado antes da operação")
    result = audit_catalog(baseline)
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["rollback_verification"] = result
    write_json(manifest_path, manifest)
    return manifest


def apply_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "planned":
        raise ValueError(f"Manifesto precisa estar planned, estado atual: {manifest.get('status')}")
    manifest["manifest_path"] = relative(manifest_path)

    # Revalidate every source and destination before the first mutation.
    for item in manifest["static_assets"]:
        source = ROOT / item["source"]
        check_source(source, item["sha256"], item["size_bytes"])
        if item["move"] and os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino está ocupado: {item['target']}")
    for item in manifest["dynamic_assets"]:
        if not item["reuses_existing_asset"]:
            source = ROOT / item["representative_source"]
            check_source(source, item["sha256"], item["size_bytes"])
            if os.path.lexists(ROOT / item["target"]):
                raise FileExistsError(f"Destino está ocupado: {item['target']}")
    for alias in manifest["layout_aliases"]:
        check_source(ROOT / alias["path"], alias["sha256"], alias["size_bytes"])
    for edit in manifest["xml_edits"]:
        path = ROOT / edit["path"]
        if path.is_symlink() or not path.is_file() or sha256_file(path) != edit["sha256_before"]:
            raise ValueError(f"XML mudou desde o planejamento: {edit['path']}")

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_json(manifest_path, manifest)
    try:
        for item in manifest["static_assets"]:
            if not item["move"]:
                continue
            source, target = ROOT / item["source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for item in manifest["dynamic_assets"]:
            if item["reuses_existing_asset"]:
                continue
            source, target = ROOT / item["representative_source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for edit in manifest["xml_edits"]:
            path = ROOT / edit["path"]
            current = path.read_bytes()
            if sha256_bytes(current) != edit["sha256_before"]:
                raise ValueError(f"XML mudou durante aplicação: {edit['path']}")
            updated, changes = rewrite_xml(current, {
                entry["from"]: entry["to"] for entry in edit["edits"]
            })
            if sha256_bytes(updated) != edit["sha256_after"] or changes != edit["edits"]:
                raise ValueError(f"Resultado XML divergente do plano: {edit['path']}")
            atomic_write_bytes(path, updated, edit["mode"], edit["mtime_ns"])
        for alias in manifest["layout_aliases"]:
            path, target = ROOT / alias["path"], ROOT / alias["target"]
            if os.path.lexists(path):
                check_source(path, alias["sha256"], alias["size_bytes"])
                path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(rel_link_target(path, target))

        catalog = build_current_catalog(manifest)
        write_json(CATALOG_PATH, catalog)
        result = audit_catalog(catalog)
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["verification"] = result
        manifest["canonical_catalog_sha256"] = sha256_file(CATALOG_PATH)
        write_json(manifest_path, manifest)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        write_json(manifest_path, manifest)
        raise


def rollback_operation(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado não reversível: {manifest.get('status')}")
    reverse_map = {
        edit["to"]: edit["from"]
        for row in manifest.get("xml_edits", [])
        for edit in row["edits"]
    }
    for edit in manifest.get("xml_edits", []):
        path = ROOT / edit["path"]
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"XML ausente ou substituído; rollback bloqueado: {edit['path']}")
        current = path.read_bytes()
        current_hash = sha256_bytes(current)
        if current_hash == edit["sha256_after"]:
            updated, _ = rewrite_xml(current, reverse_map)
            if sha256_bytes(updated) != edit["sha256_before"]:
                raise ValueError(f"Rollback XML não reproduz o hash anterior: {edit['path']}")
            atomic_write_bytes(path, updated, edit["mode"], edit["mtime_ns"])
        elif current_hash != edit["sha256_before"]:
            raise ValueError(f"XML divergiu após a operação; rollback bloqueado: {edit['path']}")

    # Restore each commonlayout path as a verified regular file, using the
    # recorded per-path mode and timestamp, before central payloads are removed.
    for alias in manifest.get("layout_aliases", []):
        path, target = ROOT / alias["path"], ROOT / alias["target"]
        if path.is_symlink():
            literal = path.readlink()
            if literal.is_absolute() or path.resolve(strict=True) != target.resolve(strict=True):
                raise ValueError(f"Symlink foi alterado; rollback bloqueado: {alias['path']}")
            check_target(target.resolve(strict=True), alias["sha256"], alias["size_bytes"])
            path.unlink()
        elif path.exists():
            check_source(path, alias["sha256"], alias["size_bytes"])
            continue
        else:
            check_target(target, alias["sha256"], alias["size_bytes"])
        temp = path.with_name(path.name + f".rollback-{os.getpid()}")
        shutil.copy2(target, temp)
        if sha256_file(temp) != alias["sha256"] or temp.stat().st_size != alias["size_bytes"]:
            temp.unlink(missing_ok=True)
            raise ValueError(f"Cópia de rollback divergiu: {alias['path']}")
        os.chmod(temp, alias["mode"])
        os.utime(temp, ns=(alias["mtime_ns"], alias["mtime_ns"]))
        os.replace(temp, path)

    # Move static images back to their exact historical paths.
    for item in reversed(manifest.get("static_assets", [])):
        if not item["move"]:
            continue
        source, target = ROOT / item["source"], ROOT / item["target"]
        if source.exists():
            check_source(source, item["sha256"], item["size_bytes"])
            continue
        check_target(target, item["sha256"], item["size_bytes"])
        source.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, source)

    # The dynamic representative copies have now been restored at every old
    # theme path. Remove only newly-created canonical payloads after exact hash
    # matches are present at all former paths.
    for item in reversed(manifest.get("dynamic_assets", [])):
        if item["reuses_existing_asset"]:
            continue
        target = ROOT / item["target"]
        if not target.exists():
            continue
        check_target(target, item["sha256"], item["size_bytes"])
        for source in item["sources"]:
            restored = ROOT / source["source"]
            check_source(restored, item["sha256"], item["size_bytes"])
        target.unlink()

    baseline = build_current_catalog(manifest, mode="legacy-restored")
    write_json(CATALOG_PATH, baseline)
    result = audit_catalog(baseline)
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["rollback_verification"] = result
    write_json(manifest_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="inventaria hashes e grava o plano; não altera assets/layouts")
    plan_parser.add_argument("--manifest", type=Path, help="destino do manifesto (padrão: Manifests/shared-asset-reorganization-<data>.json)")
    plan_main_parser = subparsers.add_parser("plan-main", help="planeja a centralização por hash da arte comum da coleção Main")
    plan_main_parser.add_argument("--manifest", type=Path, help="destino do manifesto (padrão: Manifests/shared-main-artwork-<data>.json)")
    plan_layout_media_parser = subparsers.add_parser("plan-layout-media", help="planeja centralização de mídias estáticas repetidas entre layouts")
    plan_layout_media_parser.add_argument("--manifest", type=Path, help="destino do manifesto (padrão: Manifests/layout-media-reorganization-<data>.json)")
    apply_parser = subparsers.add_parser("apply", help="move payloads verificados e cria aliases relativos")
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_main_parser = subparsers.add_parser("apply-main", help="aplica aliases relativos para arte duplicada de Main")
    apply_main_parser.add_argument("--manifest", type=Path, required=True)
    apply_layout_media_parser = subparsers.add_parser("apply-layout-media", help="centraliza mídias repetidas de layout e registra aliases")
    apply_layout_media_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify", help="confere o catálogo e todos os aliases")
    verify_parser.add_argument("--catalog", type=Path, default=CATALOG_PATH)
    rollback_parser = subparsers.add_parser("rollback", help="restaura caminhos, arquivos e XMLs verificados")
    rollback_parser.add_argument("--manifest", type=Path, required=True)
    rollback_main_parser = subparsers.add_parser("rollback-main", help="reverte aliases e payloads centralizados em Main")
    rollback_main_parser.add_argument("--manifest", type=Path, required=True)
    rollback_layout_media_parser = subparsers.add_parser("rollback-layout-media", help="reverte a centralização de mídias estáticas dos layouts")
    rollback_layout_media_parser.add_argument("--manifest", type=Path, required=True)
    resume_main_parser = subparsers.add_parser("resume-main", help="conclui o catálogo após materialização parcial verificada da arte Main")
    resume_main_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command in {
        "plan", "plan-main", "plan-layout-media",
        "apply", "apply-main", "apply-layout-media",
        "verify", "resume-main",
    }:
        print(
            "Este comando legado usa caminhos por hash e não deve criar ou validar a biblioteca atual. "
            "Use Tools/materializers/organize-asset-library.py plan|apply|verify; "
            "rollback continua disponível para manifestos históricos.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "plan":
            target = project_path(args.manifest) if args.manifest else ROOT / "Manifests" / f"shared-asset-reorganization-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
            result = plan_operation(target)
            print(json.dumps({"manifest": relative(target), "status": result["status"], **result["summary"]}, ensure_ascii=False, indent=2))
        elif args.command == "plan-main":
            target = project_path(args.manifest) if args.manifest else ROOT / "Manifests" / f"shared-main-artwork-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
            result = plan_main_artwork_operation(target)
            print(json.dumps({"manifest": relative(target), "status": result["status"], **result["summary"]}, ensure_ascii=False, indent=2))
        elif args.command == "plan-layout-media":
            target = project_path(args.manifest) if args.manifest else ROOT / "Manifests" / f"layout-media-reorganization-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
            result = plan_layout_media_operation(target)
            print(json.dumps({"manifest": relative(target), "status": result["status"], **result["summary"]}, ensure_ascii=False, indent=2))
        elif args.command == "apply":
            manifest_path = project_path(args.manifest)
            result = apply_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "verification": result.get("verification")}, ensure_ascii=False, indent=2))
        elif args.command == "apply-main":
            manifest_path = project_path(args.manifest)
            result = apply_main_artwork_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "verification": result.get("verification")}, ensure_ascii=False, indent=2))
        elif args.command == "apply-layout-media":
            manifest_path = project_path(args.manifest)
            result = apply_layout_media_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "verification": result.get("verification")}, ensure_ascii=False, indent=2))
        elif args.command == "verify":
            catalog_path = project_path(args.catalog)
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            result = audit_catalog(catalog)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "rollback":
            manifest_path = project_path(args.manifest)
            result = rollback_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "rollback_verification": result.get("rollback_verification")}, ensure_ascii=False, indent=2))
        elif args.command == "rollback-main":
            manifest_path = project_path(args.manifest)
            result = rollback_main_artwork_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "rollback_verification": result.get("rollback_verification")}, ensure_ascii=False, indent=2))
        elif args.command == "rollback-layout-media":
            manifest_path = project_path(args.manifest)
            result = rollback_layout_media_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "rollback_verification": result.get("rollback_verification")}, ensure_ascii=False, indent=2))
        elif args.command == "resume-main":
            manifest_path = project_path(args.manifest)
            result = resume_main_artwork_operation(manifest_path)
            print(json.dumps({"manifest": relative(manifest_path), "status": result["status"], "verification": result.get("verification")}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"reorganize-shared-assets: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
