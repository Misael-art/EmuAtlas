#!/usr/bin/env python3
"""Organize RetroFE's shared asset library by media, meaning, and style.

Hash digests identify and deduplicate payloads in metadata. They are not used
as directory names or routine filenames. Existing RetroFE lookup paths remain
as relative symlinks, while static XML references point to canonical paths.
"""

from __future__ import annotations

import argparse
import base64
import collections
import csv
import datetime as dt
import hashlib
import html
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path, PurePosixPath
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LAYOUTS = ROOT / "layouts"
ASSETS = ROOT / "Assets"
SHARED = ASSETS / "shared"
SOURCE_ARTWORK = ASSETS / "source-artwork"
CATALOG = ROOT / "Manifests" / "asset-catalog.json"
SEARCH_INDEX = ROOT / "Manifests" / "asset-search-index.csv"
MAIN_ROOT = ROOT / "collections" / "Main" / "medium_artwork"
MATERIALIZER_PATH = HERE / "reorganize-shared-assets.py"
AUDITOR_PATH = ROOT / "Tools" / "audits" / "layout-references.py"
DEFAULT_MANIFEST = ROOT / "Manifests" / "asset-library-migration-20261025.json"


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Não foi possível carregar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MATERIALIZER = load_module("retrofe_asset_materializer", MATERIALIZER_PATH)
AUDITOR = load_module("retrofe_layout_reference_auditor", AUDITOR_PATH)

MAIN_SEMANTICS = MATERIALIZER.MAIN_TYPE_SEMANTICS
STATIC_CATEGORIES = MATERIALIZER.STATIC_CATEGORIES
MAIN_EXTENSIONS = MATERIALIZER.MAIN_MEDIA_EXTENSIONS
MEDIA_EXTENSIONS = MATERIALIZER.LAYOUT_MEDIA_EXTENSIONS
ATTRIBUTE_RE = MATERIALIZER.ATTRIBUTE_RE
COMMENT_RE = MATERIALIZER.COMMENT_RE
DYNAMIC_MARKERS = AUDITOR.DYNAMIC_MARKERS

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg"}
FONT_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
AUDIO_EXTENSIONS = {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".webm"}
GENERIC_NAMES = {"shared", "generic", "default", "asset", "image", "media", "0", "none"}
STYLE_COMPONENTS = {
    "black", "black-white", "blue", "clean", "clean-grey", "clean-inverse", "color", "white",
    "dark", "grey", "inverse", "light", "modern", "modern-color",
    "modern-ident", "pixel", "thin-outline", "thick-outline", "white-outline",
}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def digest_file(path: Path) -> str:
    return MATERIALIZER.sha256_file(path)


def compressed(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data, 9)).decode("ascii")


def expanded(data: str) -> bytes:
    return zlib.decompress(base64.b64decode(data))


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}")
    temp.write_bytes(data)
    os.replace(temp, path)


def slug(value: str) -> str:
    return path_slug(clean_logical_name(value))


def path_slug(value: str) -> str:
    """Normalize a path component without treating taxonomy words as titles."""
    normalized = unicodedata.normalize("NFKD", str(value).strip().casefold())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:84].strip("-") or "asset"


def clean_semantic(raw: str) -> str:
    value = (raw or "").replace("\\", "/").strip(" /").casefold()
    if value.startswith("controller/"):
        value = "controllers/" + value[len("controller/"):]
    elif value == "controller-platforms":
        value = "controllers/platforms"
    elif value.startswith("controller-prompts/"):
        value = "controllers/prompts/" + value[len("controller-prompts/"):]
    elif value.startswith("controller-"):
        value = "controllers/" + value[len("controller-"):]
    if value.startswith("metadata/"):
        value = "collection-metadata/" + value[len("metadata/"):]
    for prefix in ("collection-artwork/", "images/", "assets/shared/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    value = value.replace("_", "-").replace(" ", "-")
    synonyms = {
        "controller": "controllers",
        "device": "devices",
        "devices": "devices",
        "manufacturer": "branding/manufacturers",
        "manufacturers": "branding/manufacturers",
        "developer": "branding/developers",
        "game-developer": "branding/developers",
        "logo": "branding/logos",
        "logos": "branding/logos",
        "logo-clean": "branding/logos/clean",
        "logo-fullset": "branding/logos/full-set",
        "bezel-frames": "frames/bezel",
        "crystal": "overlays/crystal",
        "fanartleft": "fanart/left",
        "fanartsys": "fanart/systems",
        "color": "collections/color-artwork",
        "colors": "collections/color-artwork",
        "game-platform": "platform-artwork/systems",
        "platform": "platform-artwork/systems",
        "title": "collections/title-artwork",
        "screenshot": "previews/screenshots",
        "screenshots": "previews/screenshots",
        "music": "themes",
        "firstletter": "collection-metadata/first-letter",
        "first-letter": "collection-metadata/first-letter",
        "playlist": "collection-metadata/playlist",
        "genre": "collection-metadata/genre",
        "players": "collection-metadata/players",
        "score": "collection-metadata/score",
        "year": "collection-metadata/year",
        "favorite": "collection-metadata/favorite",
        "favorites": "collection-metadata/favorite",
        "format": "collection-metadata/format",
        "section": "collection-metadata/section",
        "aspect-ratio": "collection-metadata/aspect-ratio",
        "collection-menu": "collections/menu-art",
        "hidden-source": "collection-metadata/hidden-source",
    }
    return synonyms.get(value, value or "miscellaneous")


def split_category_style(semantic: str) -> tuple[str, str]:
    value = clean_semantic(semantic)
    parts = [part for part in value.split("/") if part]
    if not parts:
        return "miscellaneous", "standard"
    # Category subtypes such as fanart/picture-movie, devices/full-setups,
    # and controllers/prompts/buttons stay in the category. Only known visual
    # treatments are split into the optional style component.
    style_parts: list[str] = []
    while len(parts) > 1 and parts[-1] in STYLE_COMPONENTS:
        style_parts.insert(0, parts.pop())
    category = "/".join(parts)
    style = "/".join(style_parts) or "standard"
    return category, style


def media_type_for(extension: str, source_type: str = "", category: str = "") -> str:
    ext = extension.casefold()
    source = source_type.casefold().replace("_", "-")
    cat = category.casefold().strip("/")
    if ext in FONT_EXTENSIONS or cat == "fonts" or source == "font":
        return "fonts"
    if source in {"music", "soundtrack"} or cat == "music":
        return "music"
    if source in {"video", "videos"} or cat in {"video", "videos"} or ext in VIDEO_EXTENSIONS:
        return "videos"
    if source in {"sound", "sounds", "audio"} or cat in {"sound", "sounds"}:
        return "sounds"
    if ext in AUDIO_EXTENSIONS:
        return "sounds"
    return "images"


def infer_style(text: str, current: str = "standard") -> str:
    """Infer visual treatment from source folders, never from artwork titles."""
    value = text.casefold().replace("_", "-").replace(" ", "-")
    value = value.replace("black-and-white", "black-white").replace("black-white", "black-white")
    value = value.replace("full-setups", "full-setups")
    tokens = set(re.split(r"[^a-z0-9]+", value))
    context_style: list[str] = []
    if "white" in tokens and "outline" in tokens:
        context_style.append("white-outline")
    if "outline" in tokens and "thin" in tokens:
        context_style.append("thin-outline")
    if "outline" in tokens and "thick" in tokens:
        context_style.append("thick-outline")
    if "black" in tokens and ("white" in tokens or "and" in tokens or "&" in value or "bw" in tokens):
        context_style.append("black-white")
    elif "black" in tokens and ("just" in tokens or len(tokens) <= 3):
        context_style.append("black")
    if "white" in tokens and "just" in tokens and "black" not in tokens:
        context_style.append("white")
    elif "color" in tokens or "colour" in tokens:
        context_style.append("color")
    if "dark" in tokens and "dark" not in context_style:
        context_style.append("dark")
    if "light" in tokens and "light" not in context_style:
        context_style.append("light")
    styles = [
        ("black-white", {"bw", "monochrome", "mono", "blackwhite", "black-white", "grayscale", "greyscale"}),
        ("clean-grey", {"clean-grey"}), ("clean-inverse", {"clean-inverse"}),
        ("modern-color", {"modern-color"}), ("modern-ident", {"modern-ident"}),
        ("thin-outline", {"thin-outline"}), ("thick-outline", {"thick-outline"}),
        ("white-outline", {"white-outline"}), ("clean", {"clean"}),
        ("modern", {"modern"}),
        ("pixel", {"pixel", "pixelated"}), ("inverse", {"inverse"}),
        ("grey", {"grey", "gray"}),
    ]
    found = [name for name, keys in styles if tokens & keys]
    ordered = list(dict.fromkeys(context_style + found))
    existing = [part for part in (current or "standard").split("/") if part and part != "standard"]
    merged = list(dict.fromkeys(existing + ordered))
    return "/".join(merged) if merged else "standard"


# These 48 local files were visually reviewed from the generated contact sheet.
# Category carries the asset's role; style carries only its visible treatment.
# The original path and basename remain searchable in asset-search-index.csv.
LAYOUT_VISUAL_CLASSIFICATIONS: dict[str, tuple[str, str, str]] = {
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/ArcadeDusk.png": ("backgrounds/arcade", "dark", "Arcade Dusk"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/Splash.png": ("system-screens/splash", "standard", "Forgotten Worlds splash"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/ArcadeSmoker.png": ("backgrounds/arcade", "dark", "Arcade smoke background"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/R3BORN.png": ("branding/logos", "standard", "Forgotten Worlds Reborn logo"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/Forgotten Worlds Sunrise.jpg": ("backgrounds/landscape", "color", "Forgotten Worlds sunrise"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/cab.png": ("devices/cabinets", "black", "Arcade cabinet silhouette"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/Forgotten Worlds Dusk.jpg": ("backgrounds/landscape", "dark", "Forgotten Worlds dusk"),
    "Hyper - CoinOPS Forgotten Worlds REBORN/images/ArcadeDay.png": ("backgrounds/arcade", "light", "Arcade day background"),
    "Worlds - Diamonds Linux/images/NeonMarquee.png": ("overlays/marquees", "light", "Neon marquee glow"),
    "Worlds - Diamonds Linux/images/Splash Orig.png": ("system-screens/splash", "standard", "Diamonds original splash"),
    "Worlds - Diamonds Linux/images/Splash Clear.png": ("system-screens/splash", "clean", "Diamonds clean splash"),
    "Worlds - Diamonds Linux/images/Cabinet.png": ("devices/cabinets", "standard", "Arcade cabinet"),
    "Worlds - Diamonds Linux/images/Rayer.png": ("branding/logos", "standard", "Diamonds emblem"),
    "Worlds - Diamonds Linux/images/Neon.png": ("overlays/neon-strips", "light", "Neon light strip"),
    "Worlds - Diamonds Linux/images/IntroFront.png": ("system-screens/intro", "standard", "Diamonds intro emblem"),
    "Worlds - Diamonds Linux/images/Intro - Alt.png": ("system-screens/intro", "standard", "Diamonds alternate intro"),
    "Worlds - Diamonds Linux/images/CabinetUni.png": ("devices/cabinets", "standard", "Universal arcade cabinet"),
    "Worlds - Diamonds Linux/images/hider.png": ("overlays/hide-panels", "standard", "Fade panel"),
    "Aeon Nox/images/system_info_bg.png": ("backgrounds/system-info", "standard", "System information background"),
    "Aeon Nox/images/aeon-nox-platform-preview-unavailable.png": ("previews/unavailable-media", "dark", "Aeon Nox platform preview unavailable"),
    "Aeon Nox/images/aeon-nox-game-preview-unavailable.png": ("previews/unavailable-media", "dark", "Aeon Nox game preview unavailable"),
    "Aeon Nox/images/placeholder.png": ("previews/unavailable-media", "dark", "Aeon Nox quiet cover placeholder"),
    "Aeon Nox/images/aeon-nox-game-cover-unavailable.png": ("previews/unavailable-media", "dark", "Aeon Nox game cover unavailable"),
    "Aeon Nox/images/aeon-nox-game-preview-frame.png": ("frames/bezel", "color", "Aeon Nox game preview frame"),
    "Worlds - Retro ARCADE 2/images/Arcade Playlist/all.png": ("collection-metadata/playlist", "standard", "All arcade playlists"),
    "Worlds - Retro ARCADE 2/images/Arcade Playlist/zarcades.png": ("collection-metadata/playlist", "standard", "Arcades playlist icon"),
    "Worlds - Retro ARCADE 2/images/Arcade Playlist/zarcadesman.png": ("collection-metadata/playlist", "standard", "Arcade manufacturers playlist icon"),
    "Worlds - Retro ARCADE 2/images/Splash - Manufacturers.png": ("system-screens/splash", "standard", "Manufacturer splash"),
    "Worlds - Retro ARCADE 2/images/Splash - Clean.png": ("system-screens/splash", "clean", "Clean arcade splash"),
    "Worlds - Retro ARCADE 2/images/Splash.png": ("system-screens/splash", "standard", "Arcade splash"),
    "Worlds - Retro ARCADE 2/images/sky1.png": ("backgrounds/sky", "dark", "Night sky background"),
    "Worlds - Retro ARCADE 2/images/Intro - Alt.png": ("system-screens/intro", "standard", "Alternate arcade intro"),
    "Worlds - Retro ARCADE 2/images/hider.png": ("overlays/hide-panels", "standard", "Fade panel"),
    "Worlds - Retro ARCADE 2/images/Next.png": ("branding/logos", "standard", "Next wordmark"),
    "Horizontal - psX ui/images/bootblue.png": ("branding/logos", "blue", "PlayStation blue boot logo"),
    "Horizontal - Mini Classic/images/main vidbg - Copy.png": ("backgrounds/video", "dark", "Video background panel"),
    "Horizontal - Mini Dark Revived/images/systag.png": ("ui/system-tags", "standard", "System tag bar"),
    "Horizontal - Player 3 Micro/images/Lower.png": ("ui/panels", "standard", "Lower information panel"),
    "Horizontal - Player 3 Micro/images/Splash.png": ("system-screens/splash", "standard", "Player 3 splash"),
    "Worlds - CoinOPS Universe/default.png": ("system-screens/startup", "standard", "CoinOPS Universe startup screen"),
    "Worlds - CoinOPS Universe/_setup/CoinOPS Universe.jpg": ("documentation/setup-guides", "standard", "CoinOPS Universe setup reference"),
}

for number, category, style, logical in [
    (26, "effects/bursts", "color", "Color particle burst"),
    (36, "effects/smoke", "color", "Blue pink smoke plume"),
    (35, "effects/paint-splashes", "color", "Color paint splatter"),
    (61, "backgrounds/starfield", "dark", "Blue starfield"),
    (101, "backgrounds/nebulae", "color", "Color nebula"),
    (38, "backgrounds/geometric", "color", "Geometric gradient"),
    (68, "textures/paper", "standard", "Crumpled paper texture"),
    (74, "effects/particles", "light", "Star particles"),
    (37, "effects/smoke", "color", "Blue pink smoke texture"),
    (106, "backgrounds/nebulae", "color", "Pink blue nebula"),
    (109, "backgrounds/nebulae", "light", "Glowing nebula"),
    (50, "backgrounds/nebulae", "color", "Purple blue nebula"),
    (29, "effects/paint-splashes", "color", "Color liquid splash"),
    (97, "backgrounds/starfield", "dark", "Vertical starfield"),
    (73, "effects/bokeh", "light", "Blue white bokeh"),
]:
    LAYOUT_VISUAL_CLASSIFICATIONS[f"Aura - Collection/images/pngwing.com({number}).png"] = (category, style, logical)


def clean_logical_name(value: str) -> str:
    name = str(value).strip()
    suffix = Path(name).suffix
    supported_suffixes = IMAGE_EXTENSIONS | FONT_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS | {".psd"}
    if suffix.casefold() in supported_suffixes:
        name = name[:-len(suffix)]
    name = re.sub(r"--[0-9a-f]{8,}$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^[▫▪•·\s]+", "", name)
    name = re.sub(r"^collection[ _-]+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" ._-\t")
    return name or "asset"


def classify_layout_file(path: Path) -> dict[str, str]:
    ext = path.suffix.casefold()
    relpath = path.relative_to(LAYOUTS)
    parts = [part.casefold() for part in relpath.parts]
    name = path.stem
    override = LAYOUT_VISUAL_CLASSIFICATIONS.get(relpath.as_posix())
    if override:
        category, style, logical_name = override
        return {"media_type": "images", "category": category, "style": style, "classification": "visual-contact-sheet", "logical_name": logical_name}
    if "_setup" in parts and "collections" not in parts:
        return {"media_type": media_type_for(path.suffix), "category": "documentation/setup-guides", "style": "standard", "classification": "setup-material", "logical_name": clean_logical_name(path.name)}
    if ext in FONT_EXTENSIONS or "fonts" in parts:
        context = " ".join(relpath.parts[1:-1])
        return {"media_type": "fonts", "category": "families", "style": infer_style(context), "classification": "font-family"}
    if ext in AUDIO_EXTENSIONS:
        if "music" in parts:
            return {"media_type": "music", "category": "themes", "style": "standard", "classification": "source-directory"}
        return {"media_type": "sounds", "category": "interface", "style": "standard", "classification": "source-directory"}
    if ext in VIDEO_EXTENSIONS:
        category = "intros" if "intro" in name.casefold() or "splash" in name.casefold() else "previews"
        return {"media_type": "videos", "category": category, "style": "standard", "classification": "source-name"}

    static_key = slug(name)
    semantic = STATIC_CATEGORIES.get(static_key)
    if semantic:
        category, style = split_category_style(semantic)
        context = " ".join(relpath.parts[1:-1])
        if style == "standard" and re.search(r"(?:^|[ _-])(clean|monochrome|black-white|thin-outline|thick-outline|white-outline|modern|pixel)(?:$|[ _-])", name.casefold()):
            style = infer_style(name, style)
        return {"media_type": "images", "category": category, "style": infer_style(context, style), "classification": "known-static-name"}

    lowered = name.casefold().replace("_", "-")
    parent_words = set("/".join(parts[1:]).replace("_", "-").split("/"))
    word_text = f"{lowered} {' '.join(parent_words)}"
    rules = [
        ("overlays", ("overlay", "over")),
        ("frames", ("frame", "bezel", "border")),
        ("backgrounds", ("background", "backdrop", "wall", "floor", "black")),
        ("devices", ("console", "device", "system-art", "hardware")),
        ("controllers", ("controller", "gamepad", "joystick")),
        ("branding/logos", ("logo", "brand", "mark")),
        ("ui", ("help", "button", "bar", "menu", "pointer", "infobar", "block", "select")),
        ("effects", ("effect", "fx", "blur", "shadow", "mask", "scanline", "texture")),
        ("characters", ("character", "person", "mascot")),
    ]
    for category, words in rules:
        if any(word in word_text for word in words):
            context = " ".join(relpath.parts[1:-1])
            return {"media_type": "images", "category": category, "style": infer_style(context), "classification": "name-and-directory"}
    if "fx" in parent_words or "effects" in parent_words:
        return {"media_type": "images", "category": "effects", "style": infer_style(name), "classification": "source-directory"}
    if "mask" in parent_words or "masks" in parent_words:
        return {"media_type": "images", "category": "masks", "style": infer_style(name), "classification": "source-directory"}
    # Keep the origin in metadata and flag opaque source names for the library index.
    theme = slug(relpath.parts[0]) if relpath.parts else "unknown-theme"
    category = "theme-artwork"
    style = "standard"
    if name.casefold().startswith("pngwing"):
        category, style = "decorative-elements", "standard"
    return {"media_type": "images", "category": category, "style": style, "classification": "needs-review"}


def classify_main_file(path: Path) -> dict[str, str]:
    relpath = path.relative_to(MAIN_ROOT)
    source_type = relpath.parts[0]
    semantic = MAIN_SEMANTICS.get(source_type, "miscellaneous")
    media_type = media_type_for(path.suffix, source_type, semantic)
    if media_type == "fonts":
        category, style = "families", infer_style(path.stem)
    elif media_type == "music":
        category, style = "themes", "standard"
    elif media_type == "videos":
        category, style = "previews", "standard"
    elif media_type == "sounds":
        category, style = "interface", "standard"
    else:
        category, style = split_category_style(semantic)
        context = " ".join(relpath.parts[1:-1])
        style = infer_style(context, style)
    return {
        "media_type": media_type,
        "category": category,
        "style": style,
        "source_type": source_type,
        "semantic_type": semantic,
        "source_variant_path": relpath.parent.as_posix(),
        "classification": "main-image-type-map",
    }


def classify_catalog_asset(asset: dict[str, Any], extension: str) -> dict[str, Any]:
    override = asset.get("classification_override")
    if isinstance(override, dict) and all(override.get(key) for key in ("media_type", "category", "style")):
        return {
            "media_type": str(override["media_type"]),
            "category": str(override["category"]),
            "style": str(override["style"]),
            "classification": "catalog-classification-override",
            "classification_override": override,
        }
    category_value = str(asset.get("category", ""))
    source_types = asset.get("source_types", [])
    source_type = str(source_types[0]) if source_types else ""
    media_type = media_type_for(extension, source_type, category_value)
    if media_type == "fonts":
        return {"media_type": "fonts", "category": "families", "style": infer_style(str(asset.get("logical_name", ""))), "classification": "catalog-type"}
    if media_type == "music":
        return {"media_type": "music", "category": "themes", "style": "standard", "classification": "catalog-type"}
    if media_type == "videos":
        return {"media_type": "videos", "category": "previews", "style": "standard", "classification": "catalog-type"}
    if media_type == "sounds":
        return {"media_type": "sounds", "category": "interface", "style": "standard", "classification": "catalog-type"}
    category, style = split_category_style(category_value)
    if source_type in MAIN_SEMANTICS:
        mapped = split_category_style(MAIN_SEMANTICS[source_type])
        category, style = mapped
    return {"media_type": "images", "category": category, "style": style, "classification": "catalog-category"}


def logical_candidates(group: dict[str, Any]) -> list[dict[str, str]]:
    return group["names"]


def choose_logical_name(group: dict[str, Any], category: str, source_types: list[str]) -> str:
    counts: collections.Counter[str] = collections.Counter()
    priorities: dict[str, int] = {}
    for item in logical_candidates(group):
        raw = clean_logical_name(str(item.get("name", "")))
        if not raw:
            continue
        key = re.sub(r"\s+", " ", raw).strip()
        counts[key] += int(item.get("weight", 1))
        priorities[key] = max(priorities.get(key, 0), int(item.get("priority", 0)))
    useful = [key for key in counts if slug(key).casefold() not in GENERIC_NAMES and not slug(key).casefold().endswith("-shared")]
    choices = useful or list(counts)
    if not choices:
        choices = [category.rsplit("/", 1)[-1] or (source_types[0] if source_types else "asset")]
    choices.sort(key=lambda key: (-counts[key], -priorities.get(key, 0), len(key), key.casefold(), key))
    return choices[0]


def canonical_path(media_type: str, category: str, style: str, logical_name: str, extension: str) -> str:
    parts = ["Assets", "shared", media_type]
    parts.extend(path_slug(part) for part in category.split("/") if part)
    if style and style != "standard":
        parts.extend(path_slug(part) for part in style.split("/") if part)
    filename = slug(clean_logical_name(logical_name)) + extension.casefold()
    return PurePosixPath(*parts, filename).as_posix()


def source_artwork_classification(asset: dict[str, Any], aliases: list[dict[str, Any]]) -> dict[str, str]:
    """Classify non-runtime editable masters while keeping them searchable."""
    old_path = PurePosixPath(str(asset["path"]))
    stem = clean_logical_name(str(asset.get("logical_name") or old_path.name)).casefold()
    category = str(asset.get("category", ""))
    alias_names = [clean_logical_name(str(item.get("logical_name") or Path(item["path"]).name)) for item in aliases]
    alias_text = " ".join(alias_names).casefold().replace("_", "-")
    if "bezel" in alias_text and "glass" in alias_text:
        category = "glass/bezel"
        style = "light" if "light" in alias_text else "standard"
    elif "scanline" in alias_text:
        category, style = "effects/scanlines", "color" if "colorful" in alias_text else "standard"
    elif "ghostbusters" in alias_text:
        category, style = "game-art/box-fronts", "standard"
    elif "master-system-girl" in alias_text:
        category, style = "branding/promotions", "color"
    elif "02-remove-mpr" in alias_text:
        category, style = "devices/components", "standard"
    elif "dsc9345" in alias_text or "dsc9344" in alias_text:
        category, style = "documentation/product-guides", "standard"
    elif category == "device-fullsetup-templates" or "device-fullsetup-templates" in old_path.as_posix():
        category, style = "devices/full-setups", "standard"
    elif stem.startswith("logo-template"):
        category = "branding/logos"
        style = "white"
    elif stem.startswith("next-letter-template"):
        category = "collection-metadata/first-letter"
        style = "standard"
    elif stem.startswith("sem-titulo"):
        category = "collections/menu-art"
        style = "color"
    elif stem.startswith("text-psd"):
        category = "ui/labels"
        style = "standard"
    else:
        category = "design-sources"
        style = "standard"
    media_type = media_type_for(old_path.suffix)
    useful_names = [name for name in alias_names if slug(name).casefold() not in {"device-fullsetup-templates-shared", "shared", "generic"}]
    logical_name = sorted(useful_names or [clean_logical_name(str(asset.get("logical_name") or old_path.name))], key=lambda name: (len(name), name.casefold()))[0]
    if category == "devices/full-setups" and re.fullmatch(r"(?:dsc|img)[-_ ]?[0-9a-z-]+", logical_name, re.IGNORECASE):
        logical_name = f"console-photo-{logical_name}"
    if logical_name.casefold() == "device":
        logical_name = "device-full-setup-template"
    if slug(logical_name) in {"sms-catriche", "sms-catrige"}:
        logical_name = "master-system-cartridge-set"
    elif slug(logical_name) == "sms-catrige-2k":
        logical_name = "master-system-cartridge-set-2k"
    return {"media_type": media_type, "category": category, "style": style, "logical_name": logical_name, "classification": "source-artwork-category"}


def asset_path(root_name: str, media_type: str, category: str, style: str, logical_name: str, extension: str) -> str:
    parts = ["Assets", root_name, media_type]
    parts.extend(path_slug(part) for part in category.split("/") if part)
    if style and style != "standard":
        parts.extend(path_slug(part) for part in style.split("/") if part)
    return PurePosixPath(*parts, slug(clean_logical_name(logical_name)) + extension.casefold()).as_posix()


def collision_source_qualifiers(group: dict[str, Any]) -> list[str]:
    """Return usable source labels in order of semantic relevance."""
    def concise(value: str) -> str:
        label = slug(value)
        if len(label) <= 48:
            return label
        compact = "-".join(label.split("-")[:7])
        return compact[:48].strip("-")

    paths = sorted(set(group.get("source_paths", [])) | {item["path"] for item in group.get("regular_sources", [])}, key=str.casefold)
    qualifiers: list[str] = []
    style_words = {"black", "white", "colour", "color", "dark", "light", "clean", "grey", "gray", "inverse", "modern", "pixel", "outline", "thin", "thick"}
    for raw in paths:
        parts = PurePosixPath(raw).parts
        if len(parts) >= 5 and parts[:3] == ("collections", "Main", "medium_artwork"):
            source_type = parts[3]
            context = []
            for part in parts[4:-1]:
                if part.startswith("_"):
                    continue
                words = [word for word in slug(part).split("-") if word not in style_words and word not in {"and", "the", "of", "from"}]
                value = "-".join(words)
                if value and value not in {"consoles", "computers", "arcade", "handhelds", "systems", "misc"}:
                    context.append(value)
            detail = context[-1] if context else ""
            qualifiers.append(concise("main " + source_type + (" " + detail if detail else "")))
    for raw in paths:
        parts = PurePosixPath(raw).parts
        if len(parts) >= 7 and parts[0] == "layouts" and parts[2:5] == ("collections", "_common", "medium_artwork"):
            theme, image_type = parts[1], parts[5]
            nested = [slug(part) for part in parts[6:-1] if not part.startswith("_")]
            qualifiers.append(concise(" ".join([theme, image_type, *nested])))
    for raw in paths:
        parts = PurePosixPath(raw).parts
        if len(parts) >= 4 and parts[0] == "layouts":
            theme = parts[1]
            parent = [slug(part) for part in parts[2:-1] if part.casefold() not in {"images", "fonts", "sounds", "music", "video"}]
            qualifiers.append(concise(" ".join([theme, *parent[-1:]])))
    return list(dict.fromkeys(value for value in qualifiers if value))


def resolve_name_collisions(groups: dict[str, dict[str, Any]], raw_targets: dict[str, str]) -> tuple[dict[str, str], int, int]:
    by_target: dict[str, list[str]] = collections.defaultdict(list)
    for digest, target in raw_targets.items():
        by_target[target].append(digest)
    collisions = {base: digests for base, digests in by_target.items() if len(digests) > 1}
    targets = {base_hash: target for base_hash, target in raw_targets.items() if target not in collisions}
    used = set(targets.values())
    qualified_groups = 0
    fallback_groups = 0
    for base, digests in sorted(collisions.items(), key=lambda item: item[0].casefold()):
        options = {digest: collision_source_qualifiers(groups[digest]) for digest in digests}
        option_counts = collections.Counter(value for values in options.values() for value in set(values))
        assigned: set[str] = set()
        selected: dict[str, str] = {}
        for digest in sorted(digests, key=lambda value: (len(options[value]), value)):
            available = [value for value in options[digest] if value not in assigned]
            unique = [value for value in available if option_counts[value] == 1]
            candidates = unique or available
            if candidates:
                qualifier = min(candidates, key=lambda value: (option_counts[value], len(value), value.casefold()))
                selected[digest] = qualifier
                assigned.add(qualifier)
            else:
                selected[digest] = ""
        label_counts = collections.Counter(selected.values())
        for index, digest in enumerate(sorted(digests, key=lambda value: (selected[value].casefold(), value)), 1):
            qualifier = selected[digest]
            if not qualifier:
                qualifier = f"alternate-{index:02d}"
                fallback_groups += 1
            elif label_counts[qualifier] > 1:
                same_label = sorted((value for value in digests if selected[value] == qualifier), key=lambda value: value)
                qualifier = f"{qualifier}-alternate-{same_label.index(digest) + 1:02d}"
                fallback_groups += 1
            else:
                qualified_groups += 1
            path = PurePosixPath(base)
            label = slug(qualifier)

            def qualified_path(extra: str = "") -> str:
                final_label = label + extra
                stem_limit = max(1, 120 - len(path.suffix) - len(final_label) - 1)
                stem = path.stem[:stem_limit].rstrip("-")
                return path.with_name(f"{stem}-{final_label}{path.suffix}").as_posix()

            candidate = qualified_path()
            suffix = 2
            while candidate in used:
                candidate = qualified_path(f"-alternate-{suffix:02d}")
                suffix += 1
            targets[digest] = candidate
            used.add(candidate)
            groups[digest]["path_qualifier"] = qualifier
            groups[digest]["display_name"] = f"{groups[digest]['logical_name']} ({qualifier.replace('-', ' ')})"
    for digest, group in groups.items():
        group.setdefault("path_qualifier", "")
        group.setdefault("display_name", group["logical_name"])
    return targets, qualified_groups, fallback_groups


def add_candidate(groups: dict[str, dict[str, Any]], digest: str, size: int, metadata: dict[str, Any], name: str, *, weight: int = 1, priority: int = 0) -> None:
    metadata = dict(metadata)
    category, category_style = split_category_style(str(metadata.get("category", "miscellaneous")))
    metadata["category"] = category
    current_style = str(metadata.get("style", "standard"))
    current_style_parts = [part for part in current_style.split("/") if part and part not in {"standard", "full-set", "fullset"}]
    current_style = "/".join(current_style_parts) or "standard"
    if category_style != "standard":
        metadata["style"] = "/".join(dict.fromkeys([*category_style.split("/"), *([current_style] if current_style != "standard" else [])]))
    else:
        metadata["style"] = current_style or "standard"
    group = groups.setdefault(digest, {"sha256": digest, "size_bytes": size, "taxonomies": [], "names": [], "source_types": set(), "source_paths": set(), "classification": []})
    if group["size_bytes"] != size:
        raise ValueError(f"Tamanho inconsistente para SHA-256 {digest}")
    group["taxonomies"].append({"media_type": metadata["media_type"], "category": metadata["category"], "style": metadata["style"], "classification": metadata.get("classification", "unknown"), "weight": weight})
    group["classification"].append(metadata.get("classification", "unknown"))
    group["names"].append({
        "name": name, "weight": weight, "priority": priority,
        "source_type": metadata.get("source_type", ""),
        "semantic_type": metadata.get("semantic_type", ""),
        "source_variant_path": metadata.get("source_variant_path", ""),
        "source_path": metadata.get("source_path", ""),
    })
    if metadata.get("source_type"):
        group["source_types"].add(metadata["source_type"])
    if metadata.get("semantic_type"):
        group["source_types"].add(metadata["semantic_type"])
    override = metadata.get("classification_override")
    if isinstance(override, dict):
        current = group.get("classification_override")
        if current and any(current.get(key) != override.get(key) for key in ("media_type", "category", "style")):
            raise ValueError(f"Overrides semânticos conflitantes para SHA-256 {digest}")
        group["classification_override"] = dict(override)


def preferred_taxonomy(group: dict[str, Any]) -> tuple[str, str, str, list[dict[str, Any]]]:
    weights: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for item in group["taxonomies"]:
        key = (item["media_type"], item["category"], item["style"] or "standard")
        weights[key] += int(item.get("weight", 1))
    ranked = sorted(weights, key=lambda key: (-weights[key], key[0], key[1], key[2]))
    if not ranked:
        return "images", "miscellaneous", "standard", []
    override = group.get("classification_override")
    if isinstance(override, dict):
        primary = (str(override["media_type"]), str(override["category"]), str(override["style"]))
        tags = [{"media_type": t, "category": c, "style": s, "weight": weights[(t, c, s)]} for t, c, s in ranked]
        if not any((tag["media_type"], tag["category"], tag["style"]) == primary for tag in tags):
            tags.append({"media_type": primary[0], "category": primary[1], "style": primary[2], "weight": 0})
        return primary[0], primary[1], primary[2], tags
    primary = ranked[0]
    tags = [{"media_type": t, "category": c, "style": s, "weight": weights[(t, c, s)]} for t, c, s in ranked]
    return primary[0], primary[1], primary[2], tags


def prepare_catalog(catalog_before: dict[str, Any], groups: dict[str, dict[str, Any]], targets: dict[str, str], aliases: dict[str, list[dict[str, Any]]], manifest_rel: str) -> dict[str, Any]:
    canonical = []
    aliases_by_hash: collections.Counter[str] = collections.Counter()
    for items in aliases.values():
        aliases_by_hash.update(item.get("sha256", "") for item in items)
    for digest, group in sorted(groups.items()):
        media_type, category, style, tags = preferred_taxonomy(group)
        logical = group["logical_name"]
        source_paths = sorted(group["source_paths"], key=str.casefold)
        alias_count = aliases_by_hash[digest]
        canonical.append({
            "path": targets[digest], "sha256": digest, "hash_bucket": digest[:2],
            "size_bytes": group["size_bytes"], "asset_class": "shared-runtime",
            "media_type": media_type, "category": category, "style": style,
            "logical_name": logical, "display_name": group.get("display_name", logical), "path_qualifier": group.get("path_qualifier", ""),
            "source_names": sorted({Path(str(item["name"])).name for item in group["names"] if item.get("name")}, key=str.casefold),
            "source_types": sorted(group["source_types"], key=str.casefold),
            "source_paths": source_paths, "semantic_tags": tags,
            "classification_status": "review_required" if "needs-review" in group["classification"] else "classified",
            "layout_alias_count": alias_count,
        })
        if group.get("classification_override"):
            canonical[-1]["classification_override"] = dict(group["classification_override"])
    result = json.loads(json.dumps(catalog_before))
    result["schema_version"] = 2
    result["mode"] = "semantic-library"
    result["updated_at"] = now()
    result["reorganization_manifest"] = manifest_rel
    result["storage_rules"] = {
        "library_path": "Assets/shared/<media-type>/<category>/<style>/<logical-name>.<ext>; omit style when standard",
        "hash_identity": "Full SHA-256 and its two-character bucket prefix are metadata only; no hash bucket directories or hash suffixes in routine filenames.",
        "media_types": ["fonts", "sounds", "music", "videos", "images"],
        "runtime_aliases": "Relative symlinks preserve RetroFE lookup names in layouts/*/collections/_common/medium_artwork, collections/Main/medium_artwork, and local static layout asset paths.",
        "per_game_artwork": "Artwork in collections other than Main remains with its collection until the separate migration to media/<system_id>/<media_type>/.",
        "source_artwork": "Editable masters remain under Assets/source-artwork and are indexed separately.",
        "static_layout_assets": "Every local layout media payload is cataloged once in Assets/shared; XML src/font consumers use the canonical path and legacy local names remain relative aliases.",
    }
    result["canonical_assets"] = canonical
    for field_name in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases"):
        updated = []
        for item in aliases.get(field_name, []):
            copy = dict(item)
            copy["target"] = targets[copy["sha256"]]
            copy["media_type"], copy["category"], copy["style"], _ = preferred_taxonomy(groups[copy["sha256"]])
            updated.append(copy)
        result[field_name] = sorted(updated, key=lambda item: item["path"].casefold())
    result["static_layout_assets_migrated"] = True
    result["static_layout_manifest"] = manifest_rel
    result["summary"] = {
        "canonical_assets": len(canonical),
        "unique_sha256": len({item["sha256"] for item in canonical}),
        "relative_layout_aliases": len(result["layout_aliases"]),
        "main_artwork_aliases": len(result["main_artwork_aliases"]),
        "static_layout_aliases": len(result["static_layout_aliases"]),
        "hash_buckets_metadata_only": len({item["hash_bucket"] for item in canonical}),
        "classification_review_required": sum(item["classification_status"] == "review_required" for item in canonical),
        "category_counts": dict(sorted(collections.Counter(f"{item['media_type']}/{item['category']}/{item['style']}" for item in canonical).items())),
    }
    return result


def make_search_index(catalog: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["scope", "media_type", "category", "style", "display_name", "logical_name", "canonical_path", "sha256", "hash_bucket", "source_types", "source_names", "source_paths", "aliases", "classification_status"])
    aliases_by_hash: dict[str, list[str]] = collections.defaultdict(list)
    for field in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases", "source_artwork_aliases"):
        for alias in catalog.get(field, []):
            aliases_by_hash[alias["sha256"]].append(alias["path"])
    for asset in catalog.get("canonical_assets", []):
        writer.writerow([
            "runtime", asset.get("media_type", ""), asset.get("category", ""), asset.get("style", ""), asset.get("display_name", asset.get("logical_name", "")),
            asset.get("logical_name", ""), asset["path"], asset["sha256"], asset.get("hash_bucket", asset["sha256"][:2]),
            " | ".join(asset.get("source_types", [])), " | ".join(asset.get("source_names", [])), " | ".join(asset.get("source_paths", [])),
            " | ".join(sorted(aliases_by_hash[asset["sha256"]], key=str.casefold)), asset.get("classification_status", "classified"),
        ])
    for asset in catalog.get("source_artwork_assets", []):
        writer.writerow([
            "source-artwork", asset.get("media_type", "images"), asset.get("category", ""), asset.get("style", "standard"),
            asset.get("display_name", asset.get("logical_name", "")), asset.get("logical_name", ""), asset["path"], asset["sha256"], asset.get("hash_bucket", asset["sha256"][:2]),
            " | ".join(asset.get("source_types", [])), " | ".join(asset.get("source_names", [])), " | ".join(asset.get("source_paths", [])),
            " | ".join(sorted(aliases_by_hash[asset["sha256"]], key=str.casefold)), asset.get("classification_status", "classified"),
        ])
    for asset in catalog.get("setup_media_assets", []):
        writer.writerow([
            "theme-setup", asset.get("media_type", "images"), asset.get("category", "documentation/setup-guides"), asset.get("style", "standard"),
            asset.get("logical_name", Path(asset["path"]).name), asset.get("logical_name", Path(asset["path"]).name), asset["path"], asset["sha256"], asset.get("hash_bucket", asset["sha256"][:2]),
            "theme setup material", Path(asset["path"]).name, asset["path"], "", asset.get("classification_status", "classified"),
        ])
    return output.getvalue().encode("utf-8")


def layout_setup_media_files() -> list[Path]:
    files = []
    for path in sorted(LAYOUTS.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
            continue
        parts = {part.casefold() for part in path.relative_to(LAYOUTS).parts}
        if "_setup" in parts and not MATERIALIZER.is_archived_layout_asset(path):
            files.append(path)
    return files


def build_plan(manifest_path: Path) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    catalog_bytes = CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    MATERIALIZER.audit_catalog(catalog, allow_staged_main_intake=True)
    groups: dict[str, dict[str, Any]] = {}
    old_canonical_paths: dict[str, list[str]] = collections.defaultdict(list)
    regular_sources: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    source_alias_paths: dict[str, set[str]] = collections.defaultdict(set)

    alias_fields = ("layout_aliases", "main_artwork_aliases", "static_layout_aliases")
    aliases: dict[str, list[dict[str, Any]]] = {field: [dict(item) for item in catalog.get(field, [])] for field in alias_fields}
    all_known_main = {item["path"] for item in catalog.get("main_artwork_aliases", [])} | {item["path"] for item in catalog.get("source_artwork_aliases", [])}

    for asset in catalog.get("canonical_assets", []):
        old_path = ROOT / asset["path"]
        MATERIALIZER.check_target(old_path, asset["sha256"], asset["size_bytes"])
        meta = classify_catalog_asset(asset, old_path.suffix)
        add_candidate(groups, asset["sha256"], asset["size_bytes"], meta, str(asset.get("logical_name", old_path.name)), weight=1, priority=1)
        groups[asset["sha256"]]["source_paths"].update(asset.get("source_paths", []))
        old_canonical_paths[asset["sha256"]].append(asset["path"])
        groups[asset["sha256"]].setdefault("old_assets", []).append(dict(asset))

    alias_hashes: dict[str, str] = {}
    for field in alias_fields:
        for alias in aliases[field]:
            alias_hashes[alias["path"]] = alias["sha256"]
            source_alias_paths[alias["sha256"]].add(alias["path"])
            source_type = alias.get("source_type", "")
            semantic = MAIN_SEMANTICS.get(source_type) if field == "main_artwork_aliases" else None
            mapped_category, mapped_style = split_category_style(semantic) if semantic else ("", "standard")
            meta = {
                "media_type": alias.get("media_type") or media_type_for(Path(alias["path"]).suffix, alias.get("source_type", ""), alias.get("category", "")),
                "category": mapped_category or alias.get("category", "") or alias.get("semantic_type", "miscellaneous"),
                "style": mapped_style if semantic else alias.get("style", "standard"),
                "source_type": source_type,
                "semantic_type": semantic or alias.get("semantic_type", ""),
                "classification": "existing-alias-metadata",
            }
            variant = alias.get("source_variant_path", "")
            variant_parts = [part for part in PurePosixPath(variant).parts if part.casefold() != str(source_type).casefold()]
            meta["source_variant_path"] = variant
            if variant_parts:
                meta["style"] = infer_style(" ".join(variant_parts), meta["style"])
            alias_weight = 3 if field == "main_artwork_aliases" else 1
            add_candidate(groups, alias["sha256"], alias["size_bytes"], meta, alias.get("logical_name", Path(alias["path"]).name), weight=alias_weight, priority=3 if field == "main_artwork_aliases" else 2)
            groups[alias["sha256"]]["source_paths"].add(alias["path"])

    main_files = MATERIALIZER.main_media_files(known_aliases=all_known_main)
    for path in main_files:
        digest = digest_file(path)
        snap = MATERIALIZER.snapshot(path)
        meta = classify_main_file(path)
        entry = {"path": rel(path), "size_bytes": snap["size_bytes"], "mode": snap["mode"], "mtime_ns": snap["mtime_ns"], "device": snap["device"], "inode": snap["inode"], "scope": "main_artwork", **meta}
        regular_sources[digest].append(entry)
        add_candidate(groups, digest, snap["size_bytes"], meta, path.name, weight=2, priority=5)
        groups[digest]["source_paths"].add(rel(path))

    setup_files = layout_setup_media_files()
    game_setup_files = [path for path in setup_files if {part.casefold() for part in path.relative_to(LAYOUTS).parts} >= {"_setup", "collections"}]
    setup_document_files = [path for path in setup_files if path not in set(game_setup_files)]
    layout_files = MATERIALIZER.layout_media_files()
    for path in layout_files:
        digest = digest_file(path)
        snap = MATERIALIZER.snapshot(path)
        meta = classify_layout_file(path)
        meta["source_path"] = rel(path)
        entry = {"path": rel(path), "size_bytes": snap["size_bytes"], "mode": snap["mode"], "mtime_ns": snap["mtime_ns"], "device": snap["device"], "inode": snap["inode"], "scope": "static_layout", **meta}
        regular_sources[digest].append(entry)
        add_candidate(groups, digest, snap["size_bytes"], meta, meta.get("logical_name", path.name), weight=2, priority=4)
        groups[digest]["source_paths"].add(rel(path))

    setup_media_assets = [dict(item) for item in catalog.get("setup_media_assets", [])]
    for path in setup_document_files:
        digest = digest_file(path)
        snap = MATERIALIZER.snapshot(path)
        meta = classify_layout_file(path)
        setup_asset = {
            "path": rel(path), "sha256": digest, "hash_bucket": digest[:2], "size_bytes": snap["size_bytes"],
            "media_type": meta["media_type"], "category": meta["category"], "style": meta["style"],
            "logical_name": meta.get("logical_name", clean_logical_name(path.name)), "source_names": [path.name],
            "classification_status": "classified", "reason": "Theme setup/documentation image; not a runtime layout asset.",
        }
        setup_media_assets.append(setup_asset)

    source_artwork_before = [dict(item) for item in catalog.get("source_artwork_assets", [])]
    source_artwork_aliases = [dict(item) for item in catalog.get("source_artwork_aliases", [])]
    aliases_by_hash: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for alias in source_artwork_aliases:
        aliases_by_hash[alias["sha256"]].append(alias)
    source_artwork_moves: list[dict[str, Any]] = []
    source_artwork_after: list[dict[str, Any]] = []
    source_artwork_targets: dict[str, str] = {}
    for item in source_artwork_before:
        source = ROOT / item["path"]
        MATERIALIZER.check_target(source, item["sha256"], item["size_bytes"])
        aliases_for_asset = aliases_by_hash.get(item["sha256"], [])
        meta = source_artwork_classification(item, aliases_for_asset)
        target = asset_path("source-artwork", meta["media_type"], meta["category"], meta["style"], meta["logical_name"], source.suffix)
        source_artwork_targets[item["sha256"]] = target
        if os.path.lexists(ROOT / target) and target != item["path"]:
            raise FileExistsError(f"Destino de arte-fonte ocupado: {target}")
        snap = MATERIALIZER.snapshot(source)
        source_artwork_moves.append({
            "source": item["path"], "target": target, "sha256": item["sha256"], "size_bytes": item["size_bytes"],
            "mode": snap["mode"], "mtime_ns": snap["mtime_ns"], "device": snap["device"], "inode": snap["inode"],
            "media_type": meta["media_type"], "category": meta["category"], "style": meta["style"],
            "logical_name": meta["logical_name"], "source_names": [Path(alias["path"]).name for alias in aliases_for_asset],
        })
        source_artwork_after.append({
            **item, "path": target, "hash_bucket": item["sha256"][:2], "media_type": meta["media_type"],
            "category": meta["category"], "style": meta["style"], "logical_name": meta["logical_name"],
            "display_name": meta["logical_name"], "source_names": sorted({Path(alias["path"]).name for alias in aliases_for_asset} or {Path(item["path"]).name}, key=str.casefold),
            "source_types": sorted(set(item.get("source_types", [])) | {alias.get("source_type", "") for alias in aliases_for_asset if alias.get("source_type")}, key=str.casefold),
            "source_paths": sorted({item["path"], *[alias["path"] for alias in aliases_for_asset]}, key=str.casefold),
            "scope": "source-artwork", "classification_status": "classified",
        })
    # The canonical path is a human-readable taxonomy path. Hashes remain metadata.
    raw_targets: dict[str, str] = {}
    for digest, group in groups.items():
        media_type, category, style, tags = preferred_taxonomy(group)
        source_types = sorted(group["source_types"], key=str.casefold)
        logical = choose_logical_name(group, category, source_types)
        group["media_type"], group["category"], group["style"] = media_type, category, style
        group["semantic_tags"] = tags
        group["logical_name"] = logical
        extensions = collections.Counter(Path(x).suffix.casefold() for x in old_canonical_paths.get(digest, []))
        extensions.update(Path(item["path"]).suffix.casefold() for item in regular_sources.get(digest, []))
        extension = extensions.most_common(1)[0][0] if extensions else ".png"
        group["extension"] = extension
        raw_targets[digest] = canonical_path(media_type, category, style, logical, extension)

    targets, qualified_collision_groups, fallback_collision_groups = resolve_name_collisions(groups, raw_targets)

    for alias in source_artwork_aliases:
        digest = alias["sha256"]
        if digest in source_artwork_targets:
            alias["target"] = source_artwork_targets[digest]
            asset = next(item for item in source_artwork_after if item["sha256"] == digest)
            alias.update({"media_type": asset["media_type"], "category": asset["category"], "style": asset["style"], "hash_bucket": asset["hash_bucket"]})
        elif digest in targets:
            alias["target"] = targets[digest]
            media_type, category, style, _ = preferred_taxonomy(groups[digest])
            alias.update({"media_type": media_type, "category": category, "style": style, "hash_bucket": digest[:2]})
        else:
            raise ValueError(f"Alias de source artwork sem payload canônico: {alias['path']}")

    all_sources: set[str] = set()
    for paths in old_canonical_paths.values():
        all_sources.update(paths)
    for entries in regular_sources.values():
        all_sources.update(item["path"] for item in entries)
    for digest, target in targets.items():
        target_path = ROOT / target
        if os.path.lexists(target_path) and rel(target_path) not in all_sources:
            raise FileExistsError(f"Destino semântico ocupado por arquivo fora do catálogo: {target}")
        for path in old_canonical_paths.get(digest, []):
            nearest = target_path.parent
            while not nearest.exists():
                nearest = nearest.parent
            if nearest.stat().st_dev != (ROOT / path).stat().st_dev:
                raise OSError(f"Reorganização cruzaria dispositivos: {path} -> {target}")

    # Add aliases for every remaining Main and local layout payload, including singleton hashes.
    new_main_aliases = []
    new_static_aliases = []
    for digest, entries in regular_sources.items():
        for entry in entries:
            path = entry["path"]
            meta = {"media_type": entry["media_type"], "category": entry["category"], "style": entry["style"]}
            if entry["scope"] == "main_artwork":
                new_main_aliases.append({
                    "path": path, "target": targets[digest], "sha256": digest, "size_bytes": entry["size_bytes"],
                    "source_type": entry["source_type"], "semantic_type": entry["semantic_type"], "logical_name": Path(path).name,
                    "source_variant_path": str(PurePosixPath(path).parent.relative_to(PurePosixPath("collections/Main/medium_artwork"))), **meta,
                })
            else:
                parts = PurePosixPath(path).parts
                theme = parts[1] if len(parts) > 1 else "unknown"
                new_static_aliases.append({"path": path, "target": targets[digest], "sha256": digest, "size_bytes": entry["size_bytes"],
                                          "category": entry["category"], "style": entry["style"], "media_type": entry["media_type"],
                                          "logical_name": Path(path).name, "theme": theme, "classification_status": entry["classification"]})
    aliases["main_artwork_aliases"].extend(new_main_aliases)
    aliases["static_layout_aliases"].extend(new_static_aliases)
    for field in alias_fields:
        paths = [item["path"] for item in aliases[field]]
        if len(paths) != len(set(paths)):
            raise ValueError(f"O catálogo/plano contém aliases repetidos em {field}")

    # Enrich canonical metadata with all old and new source names and aliases.
    for digest, group in groups.items():
        group["source_types"] = sorted(group["source_types"], key=str.casefold)
        group["source_paths"].update(source_alias_paths[digest])
        group["source_paths"].update(item["path"] for item in regular_sources.get(digest, []))

    manifest_rel = rel(manifest_path)
    catalog_after = prepare_catalog(catalog, groups, targets, aliases, manifest_rel)
    catalog_after["source_artwork_assets"] = source_artwork_after
    catalog_after["source_artwork_aliases"] = source_artwork_aliases
    catalog_after["setup_media_assets"] = sorted(setup_media_assets, key=lambda item: item["path"].casefold())
    catalog_after_bytes = (json.dumps(catalog_after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    index_after = make_search_index(catalog_after)

    old_path_hash: dict[str, str] = {}
    for digest, paths in old_canonical_paths.items():
        for path in paths:
            old_path_hash[path] = digest
    for field in alias_fields:
        for item in catalog.get(field, []):
            old_path_hash[item["path"]] = item["sha256"]
    for digest, entries in regular_sources.items():
        for item in entries:
            old_path_hash[item["path"]] = digest

    xml_edits = []
    for xml_path in sorted(LAYOUTS.rglob("*.xml"), key=lambda p: p.as_posix().casefold()):
        if AUDITOR.is_archived_xml(xml_path, LAYOUTS):
            continue
        before = xml_path.read_bytes()
        text = before.decode("utf-8", errors="surrogateescape")
        parts = COMMENT_RE.split(text)
        changes = []
        relative_parts = xml_path.relative_to(LAYOUTS).parts
        theme_root = LAYOUTS / relative_parts[0]

        def replace_attribute(match: re.Match[str]) -> str:
            attribute, quote, raw_value = match.group(1), match.group(2), match.group(3)
            value = html.unescape(raw_value.strip())
            if not value or value.startswith(("/", "http://", "https://", "data:")) or "\\" in value or any(marker in value for marker in DYNAMIC_MARKERS):
                return match.group(0)
            base = LAYOUTS if xml_path.name.casefold().startswith("splash") and attribute.casefold() == "src" else theme_root
            resolved = (base / value).resolve(strict=False)
            try:
                resolved_rel = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                return match.group(0)
            digest = old_path_hash.get(resolved_rel)
            if digest is None or digest not in targets:
                return match.group(0)
            updated = os.path.relpath(ROOT / targets[digest], start=base).replace(os.sep, "/")
            if updated == value:
                return match.group(0)
            changes.append({"attribute": attribute, "from": raw_value, "to": updated, "sha256": digest})
            return f"{attribute}={quote}{html.escape(updated, quote=False)}{quote}"

        for index in range(0, len(parts), 2):
            parts[index] = ATTRIBUTE_RE.sub(replace_attribute, parts[index])
        after = "".join(parts).encode("utf-8", errors="surrogateescape")
        if before != after:
            ET.fromstring(after)
            xml_edits.append({"path": rel(xml_path), "before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest(),
                              "before_zlib_base64": compressed(before), "after_zlib_base64": compressed(after), "changes": changes})

    old_index = SEARCH_INDEX.read_bytes() if SEARCH_INDEX.exists() else b""
    groups_out = []
    for digest, group in sorted(groups.items()):
        old_paths = old_canonical_paths.get(digest, [])
        sources = regular_sources.get(digest, [])
        canonical_source = old_paths[0] if old_paths else (sources[0]["path"] if sources else None)
        if canonical_source is None:
            raise ValueError(f"Sem origem física para o payload {digest}")
        groups_out.append({
            "sha256": digest, "size_bytes": group["size_bytes"], "target": targets[digest],
            "old_canonical_paths": old_paths, "regular_sources": sources, "canonical_source": canonical_source,
            "media_type": group["media_type"], "category": group["category"], "style": group["style"],
            "logical_name": group["logical_name"], "display_name": group.get("display_name", group["logical_name"]),
            "path_qualifier": group.get("path_qualifier", ""), "semantic_tags": group["semantic_tags"],
        })
    new_alias_paths = {item["path"] for item in new_main_aliases + new_static_aliases}
    new_target_by_hash = dict(targets)
    new_target_by_hash.update(source_artwork_targets)
    existing_alias_targets = []
    for field in alias_fields + ("source_artwork_aliases",):
        for item in catalog.get(field, []):
            digest = item["sha256"]
            if digest not in new_target_by_hash:
                raise ValueError(f"Alias sem destino calculado no plano: {field}: {item['path']}")
            existing_alias_targets.append({"field": field, "path": item["path"], "old_target": item["target"], "new_target": new_target_by_hash[digest], "sha256": digest})

    manifest = {
        "schema_version": 1, "kind": "retrofe_semantic_asset_library_migration", "status": "planned", "created_at": now(),
        "operation_id": f"retrofe-asset-library-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "project_root": ".", "manifest_path": manifest_rel,
        "design": {"path": "Assets/shared/<media-type>/<category>/<style>/<logical-name>.<ext>",
                   "source_artwork_path": "Assets/source-artwork/<media-type>/<category>/<style>/<logical-name>.<ext>",
                   "hash": "full SHA-256 and 8-bit bucket prefix are metadata only; not path components or routine filename suffixes",
                   "media_types": ["fonts", "sounds", "music", "videos", "images"]},
        "method": "Hash exact equality groups payloads; semantic metadata selects a readable canonical path; relative aliases preserve RetroFE lookup paths; XML src/font references are rewritten to canonical paths.",
        "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(), "catalog_before_zlib_base64": compressed(catalog_bytes),
        "catalog_after_zlib_base64": compressed(catalog_after_bytes), "catalog_after_sha256": hashlib.sha256(catalog_after_bytes).hexdigest(),
        "search_index_before_zlib_base64": compressed(old_index), "search_index_after_zlib_base64": compressed(index_after),
        "groups": groups_out, "source_artwork_moves": source_artwork_moves,
        "new_alias_paths": sorted(new_alias_paths), "existing_alias_targets": existing_alias_targets,
        "xml_edits": xml_edits,
        "summary": {
            "existing_shared_payloads_relocated": len(catalog.get("canonical_assets", [])),
            "source_artwork_payloads_reorganized": len(source_artwork_moves),
            "main_regular_payloads_centralized": len(main_files),
            "layout_regular_payloads_centralized": len(layout_files),
            "theme_setup_document_media_indexed": len(setup_document_files),
            "game_specific_setup_media_preserved": len(game_setup_files),
            "game_specific_setup_paths_preserved": [rel(path) for path in game_setup_files],
            "canonical_payloads_after": len(groups_out),
            "same_name_asset_groups_resolved": qualified_collision_groups + fallback_collision_groups,
            "collision_groups_named_from_source_context": qualified_collision_groups,
            "collision_groups_needing_numbered_alternate": fallback_collision_groups,
            "main_aliases_after": len(aliases["main_artwork_aliases"]),
            "static_layout_aliases_after": len(aliases["static_layout_aliases"]),
            "relative_commonlayout_aliases_preserved": len(aliases["layout_aliases"]),
            "xml_files_rewritten": len(xml_edits), "xml_reference_rewrites": sum(len(item["changes"]) for item in xml_edits),
            "review_required_classifications": sum(1 for item in catalog_after.get("canonical_assets", []) if item.get("classification_status") == "review_required"),
            "hash_bucket_directories_after": 0, "game_artwork_outside_Main_touched": False,
        },
        "rollback": {"command": "python3 Tools/materializers/organize-asset-library.py rollback --manifest Manifests/<this-file>",
                     "method": "Restore original XML/catalog/index, restore each regular source path from the verified canonical payload, restore prior relative aliases, and remove only migration-created canonical files.", "permanent_deletion": False},
    }
    return manifest


def build_scoped_static_asset_plan(manifest_path: Path, source_paths: list[str], reason: str,
                                 external_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Plan new, classified layout assets without rewriting unrelated catalog drift."""
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    if not source_paths or not reason.strip():
        raise ValueError("Informe ao menos um asset e uma justificativa")
    catalog_bytes = CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    index_before = SEARCH_INDEX.read_bytes()
    canonical_by_hash = {item["sha256"]: item for item in catalog.get("canonical_assets", [])}
    canonical_paths = {item["path"] for item in catalog.get("canonical_assets", [])}
    static_alias_paths = {item["path"] for item in catalog.get("static_layout_aliases", [])}
    planned: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_hashes: set[str] = set()
    for source_text in source_paths:
        source_parts = PurePosixPath(source_text)
        if not external_spec and (source_parts.is_absolute() or any(part in {".", ".."} for part in source_parts.parts)):
            raise ValueError(f"Use um caminho relativo canônico, sem traversal: {source_text}")
        source_rel = source_parts.as_posix()
        source = ROOT / source_rel
        if source_rel in seen_paths:
            raise ValueError(f"Asset repetido no plano: {source_rel}")
        seen_paths.add(source_rel)
        if not external_spec:
            try:
                source.relative_to(LAYOUTS)
                source.resolve(strict=True).relative_to(LAYOUTS.resolve())
            except ValueError as exc:
                raise ValueError(f"A origem precisa estar em layouts/: {source_rel}") from exc
        parts = {part.casefold() for part in PurePosixPath(source_rel).parts}
        if not external_spec and ("_setup" in parts or "medium_artwork" in parts):
            raise ValueError(f"Origem fora do escopo de mídia estática de layout: {source_rel}")
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"A origem precisa ser um arquivo regular, não symlink: {source_rel}")
        if source_rel in static_alias_paths:
            raise ValueError(f"O caminho já é alias catalogado: {source_rel}")
        if external_spec:
            meta = external_spec["entries"][source_text]
            validate_external_png(source)
        else:
            meta = classify_layout_file(source)
        if meta.get("classification") == "needs-review":
            raise ValueError(f"Classificação semântica exige revisão explícita: {source_rel}")
        digest = digest_file(source)
        if digest in seen_hashes or digest in canonical_by_hash:
            raise ValueError(f"O hash já aparece no catálogo/plano; use a operação de alias apropriada: {source_rel}")
        seen_hashes.add(digest)
        size = source.stat().st_size
        logical = meta.get("logical_name") or clean_logical_name(source.name)
        target = canonical_path(meta["media_type"], meta["category"], meta["style"], logical, source.suffix)
        if target in canonical_paths or os.path.lexists(ROOT / target):
            raise FileExistsError(f"Destino canônico já existe: {target}")
        canonical_paths.add(target)
        theme = external_spec["provenance"] if external_spec else PurePosixPath(source_rel).parts[1]
        tag = {"media_type": meta["media_type"], "category": meta["category"], "style": meta["style"], "weight": 1}
        planned.append({
            "source": source_rel,
            "target": target,
            "sha256": digest,
            "size_bytes": size,
            "media_type": meta["media_type"],
            "category": meta["category"],
            "style": meta["style"],
            "logical_name": logical,
            "display_name": logical,
            "source_name": source.name,
            "theme": theme,
            "classification_status": "classified",
            "classification": meta.get("classification", "scoped-layout-asset"),
            "semantic_tag": tag,
            "external_import": bool(external_spec),
            "evidence": meta.get("evidence", ""),
        })

    catalog_after = json.loads(json.dumps(catalog))
    canonical_after = catalog_after.setdefault("canonical_assets", [])
    static_after = catalog_after.setdefault("static_layout_aliases", [])
    for item in planned:
        canonical_after.append({
            "path": item["target"], "sha256": item["sha256"], "hash_bucket": item["sha256"][:2],
            "size_bytes": item["size_bytes"], "asset_class": "shared-runtime",
            "media_type": item["media_type"], "category": item["category"], "style": item["style"],
            "logical_name": item["logical_name"], "display_name": item["display_name"], "path_qualifier": "",
            "source_names": [item["source_name"]], "source_types": [], "source_paths": [item["source"]],
            "semantic_tags": [item["semantic_tag"]], "classification_status": item["classification_status"],
            "layout_alias_count": 0 if external_spec else 1,
            "import_evidence": item["evidence"],
        })
        if external_spec:
            continue
        static_after.append({
            "path": item["source"], "target": item["target"], "sha256": item["sha256"],
            "size_bytes": item["size_bytes"], "category": item["category"], "style": item["style"],
            "media_type": item["media_type"], "logical_name": item["source_name"], "theme": item["theme"],
            "classification_status": item["classification"],
        })
    canonical_after.sort(key=lambda item: item["path"].casefold())
    static_after.sort(key=lambda item: item["path"].casefold())
    counts = collections.Counter(
        f"{item.get('media_type', '')}/{item.get('category', '')}/{item.get('style', 'standard')}"
        for item in canonical_after
    )
    summary = catalog_after.setdefault("summary", {})
    summary["canonical_assets"] = len(canonical_after)
    summary["unique_sha256"] = len({item["sha256"] for item in canonical_after})
    summary["static_layout_aliases"] = len(static_after)
    summary["hash_buckets_metadata_only"] = len({item.get("hash_bucket", item["sha256"][:2]) for item in canonical_after})
    summary["classification_review_required"] = sum(item.get("classification_status") == "review_required" for item in canonical_after)
    summary["category_counts"] = dict(sorted(counts.items()))
    catalog_after["updated_at"] = now()
    manifest_rel = rel(manifest_path)
    catalog_after["asset_addition_manifest"] = manifest_rel
    catalog_after_bytes = (json.dumps(catalog_after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    index_after = make_search_index(catalog_after)
    return {
        "schema_version": 1,
        "kind": "retrofe_scoped_static_asset_addition",
        "status": "planned",
        "created_at": now(),
        "operation_id": f"retrofe-static-assets-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "project_root": ".",
        "manifest_path": manifest_rel,
        "reason": reason.strip(),
        "scope": "Only explicitly named static assets and their catalog/index rows; external imports create no source alias. Unrelated tree drift is preserved.",
        "external_import": bool(external_spec),
        "source_root_relative_to_frontend": external_spec.get("source_root_relative_to_frontend") if external_spec else None,
        "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "catalog_before_zlib_base64": compressed(catalog_bytes),
        "catalog_after_sha256": hashlib.sha256(catalog_after_bytes).hexdigest(),
        "catalog_after_zlib_base64": compressed(catalog_after_bytes),
        "search_index_before_sha256": hashlib.sha256(index_before).hexdigest(),
        "search_index_before_zlib_base64": compressed(index_before),
        "search_index_after_sha256": hashlib.sha256(index_after).hexdigest(),
        "search_index_after_zlib_base64": compressed(index_after),
        "assets": planned,
        "summary": {"canonical_payloads_added": len(planned), "relative_static_aliases_added": 0 if external_spec else len(planned), "bytes_added": sum(item["size_bytes"] for item in planned), "bytes_relocated": sum(item["size_bytes"] for item in planned), "unrelated_tree_drift_changed": False},
        "rollback": {"permanent_deletion": False, "method": "Remove the new relative aliases, return each verified canonical payload to its original layout path, and restore only the catalog/index snapshots if their after-hashes still match."},
    }


def validate_external_png(source: Path) -> None:
    """Full container/CRC and pixel decode; extension alone is never evidence."""
    from PIL import Image
    with Image.open(source) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError(f"Importação externa atual aceita somente PNG estático: {source}")
        image.verify()
    with Image.open(source) as image:
        image.load()


def build_external_static_asset_plan(manifest_path: Path, spec_path: Path, reason: str) -> dict[str, Any]:
    """Explicitly reviewed external PNG import, using the same catalog transaction."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    raw_root = Path(spec["source_root_relative_to_project"])
    if raw_root.is_absolute():
        raise ValueError("A raiz externa deve ser relativa à raiz do projeto")
    source_root = (ROOT.parents[1] / raw_root).absolute()
    resolved_root = source_root.resolve(strict=True)
    if source_root != resolved_root:
        # Normalize lexical .. first, while still rejecting actual symlink parents.
        source_root = Path(os.path.abspath(source_root))
    project_staging = spec.get("source_scope") == "project-staging"
    if project_staging:
        staging = ROOT.parents[1] / "work/roms-consolidation"
        if source_root != resolved_root or not resolved_root.is_relative_to(staging):
            raise ValueError("Staging revisado deve estar em work/roms-consolidation, sem symlinks")
    elif source_root != resolved_root or resolved_root == ROOT.parents[1] or ROOT.parents[1] in resolved_root.parents:
        raise ValueError("Raiz deve ser externa ao projeto e não pode atravessar symlinks")
    entries = {}
    allowed = {"controllers/artwork", "collection-metadata/genre", "fade"}
    if project_staging:
        allowed = {"branding/logos/full-set", "devices"}
    for entry in spec["assets"]:
        relative = PurePosixPath(entry["relative_path"])
        if relative.is_absolute() or any(p in {".", ".."} for p in relative.parts):
            raise ValueError("Origem externa com traversal")
        source = source_root / relative
        if source.is_symlink() or source.resolve(strict=True) != source:
            raise ValueError(f"Origem externa não pode atravessar symlinks: {source}")
        if entry.get("category") not in allowed or not entry.get("evidence", "").strip():
            raise ValueError("Categoria não aprovada ou evidência ausente")
        logical = entry["logical_name"]
        if not logical or slug(logical) != logical:
            raise ValueError("Nome lógico deve ser um slug legível explícito")
        if source.suffix.lower() != ".png":
            raise ValueError("Somente PNG estático validado está coberto nesta importação")
        if digest_file(source) != entry.get("sha256"):
            raise ValueError("Origem divergiu do SHA-256 aprovado no inventário")
        source_key = os.path.relpath(source, ROOT)
        if source_key in entries:
            raise ValueError("Origem repetida")
        style = entry.get("style", "standard") if project_staging else "standard"
        if style not in {"standard", "white/light"}:
            raise ValueError("Estilo de importação não aprovado")
        entries[source_key] = dict(media_type="images", category=entry["category"], style=style,
            logical_name=logical, classification="visually-reviewed-external-art", evidence=entry["evidence"])
    if not spec.get("references_checked_none") or not spec.get("reference_evidence"):
        raise ValueError("Registre a busca de referências ativas antes do plano")
    plan = build_scoped_static_asset_plan(manifest_path, list(entries), reason, {
        "entries": entries, "provenance": spec["provenance"],
        "source_root_relative_to_frontend": os.path.relpath(source_root, ROOT)})
    plan["external_import_spec"] = spec
    plan["external_import_spec_sha256"] = digest_file(spec_path)
    plan["rollback"]["method"] = "Move each verified canonical PNG back to its exact external source; restore catalog/index snapshots only if their hashes still match. No external aliases or deletion."
    return plan


def build_scoped_layout_common_alias_plan(
    manifest_path: Path,
    alias_specs: list[tuple[str, str]],
    reason: str,
) -> dict[str, Any]:
    """Plan common-layout lookup symlinks that reuse existing canonical media."""
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    if not alias_specs or not reason.strip():
        raise ValueError("Inclusão de alias comum exige caminhos e motivo registrado")

    catalog_bytes = CATALOG.read_bytes()
    index_before = SEARCH_INDEX.read_bytes()
    catalog_before = json.loads(catalog_bytes.decode("utf-8"))
    catalog_after = json.loads(json.dumps(catalog_before))
    canonical_by_path = {item["path"]: item for item in catalog_after.get("canonical_assets", [])}
    aliases = catalog_after.setdefault("layout_aliases", [])
    existing_by_path = {item["path"]: item for item in aliases}
    planned_paths: set[str] = set()
    planned: list[dict[str, Any]] = []
    supported_semantics = {"isFavorite": ("favorite", "collection-metadata/favorite")}

    for alias_path_text, target_text in alias_specs:
        alias_path = PurePosixPath(alias_path_text)
        target_path = PurePosixPath(target_text)
        if alias_path.is_absolute() or ".." in alias_path.parts:
            raise ValueError(f"Caminho de alias inválido: {alias_path_text}")
        if target_path.is_absolute() or ".." in target_path.parts:
            raise ValueError(f"Caminho canônico inválido: {target_text}")
        parts = alias_path.parts
        if len(parts) != 7 or parts[0] != "layouts" or parts[2:5] != ("collections", "_common", "medium_artwork"):
            raise ValueError(f"O alias precisa ficar em layouts/<tema>/collections/_common/medium_artwork/<tipo>/<arquivo>: {alias_path_text}")
        theme, image_type, filename = parts[1], parts[5], parts[6]
        semantic = supported_semantics.get(image_type)
        if semantic is None or Path(filename).suffix.casefold() != ".png" or Path(filename).stem not in {"yes", "no"}:
            raise ValueError(f"Tipo de commonlayout ainda não suportado por esta operação: {image_type}/{filename}")
        path = alias_path.as_posix()
        target = target_path.as_posix()
        if path in planned_paths or path in existing_by_path or os.path.lexists(ROOT / path):
            raise FileExistsError(f"Alias já existe ou está duplicado: {path}")
        asset = canonical_by_path.get(target)
        if (
            asset is None
            or asset.get("media_type") != "images"
            or asset.get("category") != semantic[1]
            or asset.get("classification_status", "classified") != "classified"
        ):
            raise ValueError(f"O alvo precisa ser uma imagem favorite catalogada e classificada: {target}")
        MATERIALIZER.check_target(ROOT / target, asset["sha256"], asset["size_bytes"])
        alias = {
            "path": path,
            "target": target,
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
            "source_type": image_type,
            "semantic_type": semantic[0],
            "theme": theme,
            "logical_name": filename,
            "source_variant_path": image_type,
            "media_type": "images",
            "category": asset["category"],
            "style": asset["style"],
        }
        aliases.append(alias)
        planned.append({**alias, "action": "create"})
        planned_paths.add(path)

    catalog_after["layout_aliases"] = sorted(aliases, key=lambda item: item["path"].casefold())
    by_digest: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for alias in catalog_after["layout_aliases"]:
        by_digest[alias["sha256"]].append(alias)
    for asset in catalog_after.get("canonical_assets", []):
        if asset["sha256"] in by_digest:
            asset["layout_alias_count"] = len(by_digest[asset["sha256"]])
            asset["source_paths"] = sorted(set(asset.get("source_paths", [])) | {item["path"] for item in by_digest[asset["sha256"]]}, key=str.casefold)
            asset["source_names"] = sorted(set(asset.get("source_names", [])) | {Path(item["path"]).name for item in by_digest[asset["sha256"]]}, key=str.casefold)
    catalog_after["updated_at"] = now()
    summary = catalog_after.setdefault("summary", {})
    summary["relative_layout_aliases"] = len(catalog_after["layout_aliases"])
    catalog_after_bytes = (json.dumps(catalog_after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    index_after = make_search_index(catalog_after)
    manifest_rel = rel(manifest_path.resolve())
    return {
        "schema_version": 1,
        "kind": "retrofe_scoped_common_layout_alias_addition",
        "status": "planned",
        "created_at": now(),
        "operation_id": f"retrofe-common-layout-aliases-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "project_root": ".",
        "manifest_path": manifest_rel,
        "reason": reason.strip(),
        "scope": "Only the named commonlayout relative aliases, their catalog rows, and search-index rows are changed; canonical image payloads are reused without copying.",
        "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "catalog_before_zlib_base64": compressed(catalog_bytes),
        "catalog_after_sha256": hashlib.sha256(catalog_after_bytes).hexdigest(),
        "catalog_after_zlib_base64": compressed(catalog_after_bytes),
        "search_index_before_sha256": hashlib.sha256(index_before).hexdigest(),
        "search_index_before_zlib_base64": compressed(index_before),
        "search_index_after_sha256": hashlib.sha256(index_after).hexdigest(),
        "search_index_after_zlib_base64": compressed(index_after),
        "aliases": planned,
        "summary": {"relative_commonlayout_aliases_added": len(planned), "canonical_payloads_copied": 0},
        "rollback": {"permanent_deletion": False, "method": "Remove only the listed relative aliases and restore catalog/index snapshots when their after-hashes still match."},
    }


def apply_scoped_common_layout_alias_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_scoped_common_layout_alias_addition" or manifest.get("status") != "planned":
        raise ValueError(f"Manifesto de alias commonlayout fora do estado planned: {manifest.get('status')}")
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo/índice mudou desde o plano; aplicação bloqueada")
    for alias in manifest["aliases"]:
        target = ROOT / alias["target"]
        MATERIALIZER.check_target(target, alias["sha256"], alias["size_bytes"])
        if os.path.lexists(ROOT / alias["path"]):
            raise FileExistsError(f"O alias precisa continuar ausente até a aplicação: {alias['path']}")
    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_manifest(path, manifest)
    try:
        for alias in manifest["aliases"]:
            link_relative(ROOT / alias["path"], ROOT / alias["target"])
        atomic_write(CATALOG, expanded(manifest["catalog_after_zlib_base64"]))
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_after_zlib_base64"]))
        for alias in manifest["aliases"]:
            link = ROOT / alias["path"]
            if not link.is_symlink() or link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"O alias commonlayout não resolve ao payload planejado: {alias['path']}")
            MATERIALIZER.check_target(ROOT / alias["target"], alias["sha256"], alias["size_bytes"])
        manifest["verification"] = {"status": "passed_scoped", "aliases": len(manifest["aliases"]), "payloads_copied": 0}
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        write_manifest(path, manifest)
        return manifest
    except Exception:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        write_manifest(path, manifest)
        raise


def rollback_scoped_common_layout_alias_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_scoped_common_layout_alias_addition" or manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Manifesto de alias commonlayout não reversível: {manifest.get('status')}")
    current_catalog = digest_file(CATALOG)
    current_index = digest_file(SEARCH_INDEX)
    if current_catalog not in {manifest["catalog_before_sha256"], manifest.get("catalog_after_sha256", "")} or current_index not in {manifest["search_index_before_sha256"], manifest.get("search_index_after_sha256", "")}:
        raise ValueError("Catálogo/índice mudou depois da operação; rollback bloqueado")
    for alias in manifest["aliases"]:
        link = ROOT / alias["path"]
        if link.is_symlink():
            if link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"Alias mudou de alvo; rollback bloqueado: {alias['path']}")
        elif link.exists():
            raise ValueError(f"Alias foi substituído por arquivo; rollback bloqueado: {alias['path']}")
    for alias in manifest["aliases"]:
        link = ROOT / alias["path"]
        if link.is_symlink():
            link.unlink()
    if current_catalog == manifest.get("catalog_after_sha256"):
        atomic_write(CATALOG, expanded(manifest["catalog_before_zlib_base64"]))
    if current_index == manifest.get("search_index_after_sha256"):
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_before_zlib_base64"]))
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo ou índice não retornou ao estado anterior")
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["rollback_verification"] = {"status": "passed_scoped", "aliases_removed": len(manifest["aliases"])}
    write_manifest(path, manifest)
    return manifest


def apply_scoped_static_asset_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_scoped_static_asset_addition" or manifest.get("status") != "planned":
        raise ValueError(f"Manifesto de inclusão de assets fora do estado planned: {manifest.get('status')}")
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo/índice mudou desde o plano; aplicação bloqueada")
    for item in manifest["assets"]:
        source, target = ROOT / item["source"], ROOT / item["target"]
        MATERIALIZER.check_source(source, item["sha256"], item["size_bytes"])
        if manifest.get("external_import"):
            validate_external_source(manifest, item)
            validate_external_png(source)
        if os.path.lexists(target):
            raise FileExistsError(f"Destino canônico já existe: {item['target']}")
        if source.stat().st_dev != SHARED.stat().st_dev:
            raise ValueError("Importação exige rename no mesmo volume, sem cópia temporária")
    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_manifest(path, manifest)
    try:
        for item in manifest["assets"]:
            source, target = ROOT / item["source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            if not manifest.get("external_import"):
                link_relative(source, target)
        atomic_write(CATALOG, expanded(manifest["catalog_after_zlib_base64"]))
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_after_zlib_base64"]))
        for item in manifest["assets"]:
            MATERIALIZER.check_target(ROOT / item["target"], item["sha256"], item["size_bytes"])
            alias = ROOT / item["source"]
            if manifest.get("external_import"):
                if os.path.lexists(alias):
                    raise ValueError(f"Origem externa continua ocupada: {alias}")
                validate_external_png(ROOT / item["target"])
                continue
            if not alias.is_symlink() or alias.resolve(strict=True) != (ROOT / item["target"]).resolve(strict=True):
                raise ValueError(f"Alias estático não aponta ao payload planejado: {item['source']}")
        manifest["verification"] = {"status": "passed_scoped", "payloads": len(manifest["assets"]), "aliases": 0 if manifest.get("external_import") else len(manifest["assets"]), "unrelated_tree_drift_checked_or_changed": False}
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["catalog_after_sha256"] = digest_file(CATALOG)
        manifest["search_index_after_sha256"] = digest_file(SEARCH_INDEX)
        write_manifest(path, manifest)
        return manifest
    except Exception:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        write_manifest(path, manifest)
        raise


def rollback_scoped_static_asset_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_scoped_static_asset_addition" or manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Manifesto de inclusão de assets não reversível: {manifest.get('status')}")
    for filename, before_key, after_key in ((CATALOG, "catalog_before_sha256", "catalog_after_sha256"), (SEARCH_INDEX, "search_index_before_sha256", "search_index_after_sha256")):
        current = digest_file(filename)
        if current not in {manifest[before_key], manifest.get(after_key, "")}:
            raise ValueError(f"{filename.name} mudou após a operação; rollback bloqueado")
    for item in manifest["assets"]:
        source, target = ROOT / item["source"], ROOT / item["target"]
        if manifest.get("external_import"):
            validate_external_source(manifest, item, allow_missing=True)
        if target.exists():
            MATERIALIZER.check_target(target, item["sha256"], item["size_bytes"])
        if manifest.get("external_import") and source.is_symlink():
            raise ValueError("Origem externa foi substituída por symlink; rollback bloqueado")
        if source.is_symlink():
            if source.resolve(strict=True) != target.resolve(strict=True):
                raise ValueError(f"Alias mudou de alvo; rollback bloqueado: {item['source']}")
        elif source.exists():
            MATERIALIZER.check_source(source, item["sha256"], item["size_bytes"])
            if target.exists():
                raise ValueError(f"Origem e destino coexistem; rollback bloqueado: {item['source']}")
        elif not target.exists():
            raise FileNotFoundError(f"Payload não encontrado para rollback: {item['target']}")
    for item in manifest["assets"]:
        source, target = ROOT / item["source"], ROOT / item["target"]
        if source.is_symlink():
            source.unlink()
        if target.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
    if digest_file(CATALOG) == manifest.get("catalog_after_sha256"):
        atomic_write(CATALOG, expanded(manifest["catalog_before_zlib_base64"]))
    if digest_file(SEARCH_INDEX) == manifest.get("search_index_after_sha256"):
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_before_zlib_base64"]))
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo ou índice não retornou ao estado anterior")
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["rollback_verification"] = {"status": "passed_scoped", "payloads_restored": len(manifest["assets"])}
    write_manifest(path, manifest)
    return manifest


def validate_external_source(manifest: dict[str, Any], item: dict[str, Any], allow_missing: bool = False) -> None:
    source = Path(os.path.abspath(ROOT / item["source"]))
    source_root = Path(os.path.abspath(ROOT / manifest["source_root_relative_to_frontend"]))
    if source_root == source or source_root not in source.parents or source.resolve() != source:
        raise ValueError("Origem escapou da raiz externa ou atravessa symlink")
    if not allow_missing and not source.is_file():
        raise FileNotFoundError(source)
    target = ROOT / item["target"]
    if not target.is_relative_to(SHARED) or target.resolve() != target or ".." in Path(item["target"]).parts:
        raise ValueError("Destino escapou de Assets/shared ou atravessa symlink")


def verify_scoped_static_asset_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_scoped_static_asset_addition" or manifest.get("status") != "completed_verified":
        raise ValueError("Verificação exige manifesto de inclusão concluído")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    by_path = {a["path"]: a for a in catalog["canonical_assets"]}
    for item in manifest["assets"]:
        asset = by_path[item["target"]]
        for key in ("sha256", "size_bytes", "media_type", "category", "style"):
            if asset[key] != item[key]:
                raise ValueError(f"Catálogo divergiu: {item['target']} / {key}")
        MATERIALIZER.check_target(ROOT / item["target"], item["sha256"], item["size_bytes"])
        source = ROOT / item["source"]
        if manifest.get("external_import"):
            validate_external_source(manifest, item, allow_missing=True)
            validate_external_png(ROOT / item["target"])
            if os.path.lexists(source):
                raise ValueError("Origem externa reapareceu; revisão necessária")
        elif not source.is_symlink() or source.resolve() != (ROOT / item["target"]).resolve():
            raise ValueError("Alias estático divergente")
    if SEARCH_INDEX.read_bytes() != make_search_index(catalog):
        raise ValueError("Índice de busca diverge do catálogo atual")
    return {"status": "passed_scoped", "payloads": len(manifest["assets"]),
            "external_aliases": 0, "unrelated_tree_drift_checked_or_changed": False}


def write_manifest(path: Path, data: dict[str, Any]) -> None:
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def catalog_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return json.loads(expanded(manifest["catalog_after_zlib_base64"]).decode("utf-8"))


def link_relative(alias: Path, target: Path) -> None:
    alias.parent.mkdir(parents=True, exist_ok=True)
    literal = Path(os.path.relpath(target, start=alias.parent))
    os.symlink(literal, alias)


def main_artwork_alias_audit_status(catalog: dict[str, Any]) -> dict[str, Any]:
    """Report a known shared-tree drift without letting it hide target checks."""
    try:
        summary = MATERIALIZER.audit_catalog(catalog)
        return {"status": "passed", "summary": summary}
    except ValueError as exc:
        message = str(exc)
        if not message.startswith("Árvore Assets/shared diverge do catálogo;"):
            raise
        return {"status": "preexisting_shared_tree_drift", "error": message}


def verify_main_artwork_alias_scope(manifest: dict[str, Any], *, applied: bool) -> dict[str, Any]:
    """Verify only aliases owned by this operation and preserve global drift."""
    catalog_bytes = CATALOG.read_bytes()
    catalog = json.loads(catalog_bytes.decode("utf-8"))
    audit = main_artwork_alias_audit_status(catalog)
    expected_audit = manifest.get("preexisting_global_audit", {"status": "passed"})
    if applied and expected_audit.get("status") == "passed":
        expected_audit = json.loads(json.dumps(expected_audit))
        # The transaction adds exactly these links. All other audited counts
        # must remain equal; case-only renames do not increase cardinality.
        created = sum(item.get("action") == "create" for item in manifest["main_artwork_aliases"])
        for key in ("main_artwork_aliases", "actual_main_artwork_aliases"):
            if key in expected_audit.get("summary", {}):
                expected_audit["summary"][key] += created
    if audit != expected_audit:
        raise ValueError("A auditoria global de Assets/shared mudou desde o plano; gere outro manifesto após revisar a divergência")

    alias_records = {item["path"]: item for item in catalog.get("main_artwork_aliases", [])}
    if len(alias_records) != len(catalog.get("main_artwork_aliases", [])):
        raise ValueError("O catálogo contém caminhos Main duplicados")
    canonical_by_path = {item["path"]: item for item in catalog.get("canonical_assets", [])}
    for item in manifest.get("main_artwork_aliases", []):
        alias = alias_records.get(item["path"])
        if applied:
            if alias is None:
                raise ValueError(f"O catálogo não registra o alias planejado: {item['path']}")
            for key in ("target", "sha256", "size_bytes", "source_type", "semantic_type", "category", "style"):
                if alias.get(key) != item.get(key):
                    raise ValueError(f"Metadados de alias divergiram do plano em {item['path']}: {key}")
            link = ROOT / item["path"]
            if not link.is_symlink() or link.resolve(strict=True) != (ROOT / item["target"]).resolve(strict=True):
                raise ValueError(f"O alias não resolve ao alvo relativo planejado: {item['path']}")
            if item.get("rename_from") and os.path.lexists(ROOT / item["rename_from"]):
                raise ValueError(f"O alias anterior ainda existe após a reconciliação: {item['rename_from']}")
        else:
            if alias is not None or os.path.lexists(ROOT / item["path"]):
                raise ValueError(f"O alias novo deveria estar ausente antes da aplicação: {item['path']}")
            if item.get("rename_from"):
                old_record = alias_records.get(item["rename_from"])
                if old_record != item.get("rename_from_alias"):
                    raise ValueError(f"O registro do alias anterior não foi restaurado: {item['rename_from']}")
                old_link = ROOT / item["rename_from"]
                if not old_link.is_symlink() or old_link.resolve(strict=True) != (ROOT / item["target"]).resolve(strict=True):
                    raise ValueError(f"O symlink anterior não foi restaurado: {item['rename_from']}")
        asset = canonical_by_path.get(item["target"])
        if not asset or asset.get("media_type") != "images" or asset.get("category") != item.get("category"):
            raise ValueError(f"O alvo não conserva a categoria de imagem planejada: {item['target']}")
        if asset.get("sha256") != item["sha256"] or asset.get("size_bytes") != item["size_bytes"]:
            raise ValueError(f"O hash ou tamanho do alvo mudou desde o plano: {item['target']}")
        MATERIALIZER.check_target(ROOT / item["target"], item["sha256"], item["size_bytes"])

    canonical = catalog.get("canonical_assets", [])
    unique_hashes = {item["sha256"] for item in canonical}
    if len(canonical) != manifest["summary"]["canonical_assets_before"] or len(unique_hashes) != manifest["summary"]["unique_hashes_before"]:
        raise ValueError("A quantidade de payloads canônicos mudou durante a operação de aliases")
    if applied:
        if hashlib.sha256(catalog_bytes).hexdigest() != manifest.get("catalog_after_sha256"):
            raise ValueError("O catálogo mudou após a gravação dos aliases planejados")
        if digest_file(SEARCH_INDEX) != manifest.get("search_index_after_sha256"):
            raise ValueError("O índice de busca mudou após a gravação dos aliases planejados")
    return {
        "scope": "aliases listed in this manifest",
        "aliases_checked": len(manifest.get("main_artwork_aliases", [])),
        "canonical_payloads_added": 0,
        "payload_bytes_copied": 0,
        "unique_hashes": len(unique_hashes),
        "global_catalog_audit": audit,
    }


def build_main_artwork_alias_plan(manifest_path: Path, alias_specs: list[tuple[str, str, str]], reason: str) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    if not alias_specs or not reason.strip():
        raise ValueError("A inclusão exige coleção, asset canônico e motivo registrado")
    if len({(alias_type.casefold(), collection.casefold()) for alias_type, collection, _ in alias_specs}) != len(alias_specs):
        raise ValueError("A mesma coleção e variante de arte foram informadas mais de uma vez")

    catalog_bytes = CATALOG.read_bytes()
    catalog_before = json.loads(catalog_bytes.decode("utf-8"))
    global_audit = main_artwork_alias_audit_status(catalog_before)
    catalog_after = json.loads(json.dumps(catalog_before))
    search_index_before = SEARCH_INDEX.read_bytes()
    canonical_by_path = {item["path"]: item for item in catalog_after.get("canonical_assets", [])}
    known_alias_paths = {item["path"] for item in catalog_after.get("main_artwork_aliases", [])}
    menu_path = ROOT / "collections" / "Main" / "menu.txt"
    if menu_path.is_symlink() or not menu_path.is_file():
        raise ValueError("collections/Main/menu.txt precisa ser arquivo local")
    menu_entries = {line.strip() for line in menu_path.read_text(encoding="utf-8").splitlines() if line.strip()}

    aliases = []
    planned_paths = set()
    for alias_type, collection, canonical_path in alias_specs:
        alias_type, collection, canonical_path = alias_type.strip(), collection.strip(), canonical_path.strip()
        permitted_categories = {
            "logo": {"branding/logos/full-set"},
            "device": {"devices"},
            # Main/fanartsys is RetroFE's platform-background lookup path. A
            # platform-matched fan-art payload may fill only a missing system
            # background; its canonical catalog category remains fanart/fan-art.
            "fanartsys": {"fanart/systems", "fanart/fan-art"},
        }
        if alias_type.startswith("logo_aeon_nox_"):
            permitted_categories[alias_type] = {"branding/logos/full-set"}
        if alias_type not in MAIN_SEMANTICS or alias_type not in permitted_categories:
            raise ValueError(f"Tipo de alias Main não permitido ou sem classificação: {alias_type!r}")
        if not collection or Path(collection).name != collection or collection in {".", ".."}:
            raise ValueError(f"Nome de coleção inválido: {collection!r}")
        if collection not in menu_entries:
            raise ValueError(f"A coleção não aparece em collections/Main/menu.txt: {collection}")
        collection_dir = ROOT / "collections" / collection
        settings = collection_dir / "settings.conf"
        if collection_dir.is_symlink() or not collection_dir.is_dir() or settings.is_symlink() or not settings.is_file():
            raise ValueError(f"A coleção precisa de diretório e settings.conf locais: {collection}")
        settings_bytes = settings.read_bytes()
        settings_text = settings_bytes.decode("utf-8", errors="replace")
        list_paths = re.findall(r"^\s*list\.path\s*=\s*(.*?)\s*$", settings_text, flags=re.IGNORECASE | re.MULTILINE)
        if len(list_paths) != 1 or not list_paths[0]:
            raise ValueError(f"A coleção precisa de um único list.path para validar sua identidade: {collection}")
        suffix = Path(canonical_path).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise ValueError(f"Extensão de imagem não permitida para alias Main: {canonical_path}")
        alias_path = PurePosixPath("collections", "Main", "medium_artwork", alias_type, f"{collection}{suffix}").as_posix()
        if alias_path in known_alias_paths or alias_path in planned_paths:
            raise ValueError(f"O alias Main já está catalogado ou duplicado no plano: {alias_path}")
        target_rel = PurePosixPath(canonical_path)
        if target_rel.is_absolute() or ".." in target_rel.parts:
            raise ValueError(f"Caminho canônico inválido para alias Main: {canonical_path}")
        asset = canonical_by_path.get(canonical_path)
        if asset is None or asset.get("media_type") != "images" or asset.get("category") not in permitted_categories[alias_type]:
            raise ValueError(f"A categoria do alvo não corresponde ao tipo de alias {alias_type}: {canonical_path}")
        if alias_type == "fanartsys" and asset.get("category") == "fanart/fan-art":
            if "fallback" not in reason.casefold():
                raise ValueError("Um alias fanart/systems para fanart/fan-art exige motivo explícito de fallback")
            matching_fan_art = [
                item for item in catalog_after.get("main_artwork_aliases", [])
                if item.get("source_type") == "fanart_coverart"
                and Path(item.get("path", "")).stem.casefold() == collection.casefold()
            ]
            if len(matching_fan_art) != 1:
                raise ValueError(
                    f"Fanart fallback exige um único alias fanart_coverart para a mesma plataforma: {collection}"
                )
            source_alias = matching_fan_art[0]
            target_path = ROOT / source_alias["path"]
            if (
                source_alias.get("target") != canonical_path
                or source_alias.get("sha256") != asset.get("sha256")
                or source_alias.get("size_bytes") != asset.get("size_bytes")
                or source_alias.get("category") != "fanart/fan-art"
                or not target_path.is_symlink()
                or target_path.resolve(strict=True) != (ROOT / canonical_path).resolve(strict=True)
            ):
                raise ValueError(
                    f"Fanart fallback não corresponde ao alias de plataforma já validado: {collection}"
                )
        if asset.get("classification_status", "classified") != "classified":
            raise ValueError(f"O asset canônico requer revisão de classificação: {canonical_path}")
        MATERIALIZER.check_target(ROOT / canonical_path, asset["sha256"], asset["size_bytes"])

        case_matches = [
            item for item in catalog_after.get("main_artwork_aliases", [])
            if item.get("source_type") == alias_type
            and Path(item["path"]).stem.casefold() == collection.casefold()
            and item["path"] != alias_path
        ]
        rename_from = None
        rename_from_alias = None
        if case_matches:
            if len(case_matches) != 1:
                raise ValueError(f"Há mais de um alias com o mesmo nome ignorando caixa: {alias_type}/{collection}")
            old_alias = case_matches[0]
            old_path = old_alias["path"]
            if old_path.casefold() != alias_path.casefold():
                raise ValueError(f"O alias existente conflita com a coleção sem ser apenas uma diferença de caixa: {old_path}")
            old_link = ROOT / old_path
            if (old_alias.get("target"), old_alias.get("sha256"), old_alias.get("size_bytes")) != (canonical_path, asset["sha256"], asset["size_bytes"]):
                raise ValueError(f"O alias existente tem outro alvo; revisão manual necessária: {old_path}")
            if not old_link.is_symlink() or old_link.resolve(strict=True) != (ROOT / canonical_path).resolve(strict=True):
                raise ValueError(f"O alias anterior não resolve ao payload planejado: {old_path}")
            rename_from, rename_from_alias = old_path, dict(old_alias)
            catalog_after["main_artwork_aliases"].remove(old_alias)
            old_asset = canonical_by_path[old_alias["target"]]
            old_asset["source_paths"] = sorted(set(old_asset.get("source_paths", [])) - {old_path}, key=str.casefold)
        target_alias = ROOT / alias_path
        if os.path.lexists(target_alias):
            same_case_rename = False
            if rename_from:
                try:
                    same_case_rename = target_alias.samefile(ROOT / rename_from)
                except OSError:
                    same_case_rename = False
            if not same_case_rename:
                raise FileExistsError(f"O caminho precisa estar ausente para inclusão: {alias_path}")

        alias = {
            "path": alias_path,
            "target": canonical_path,
            "sha256": asset["sha256"],
            "size_bytes": asset["size_bytes"],
            "source_type": alias_type,
            "semantic_type": MAIN_SEMANTICS[alias_type],
            "logical_name": f"{collection}{suffix}",
            "source_variant_path": alias_type,
            "media_type": "images",
            "category": asset["category"],
            "style": asset["style"],
        }
        catalog_after["main_artwork_aliases"].append(alias)
        catalog_asset = canonical_by_path[canonical_path]
        catalog_asset["source_paths"] = sorted(set(catalog_asset.get("source_paths", [])) | {alias_path}, key=str.casefold)
        catalog_asset["source_names"] = sorted(set(catalog_asset.get("source_names", [])) | {alias["logical_name"]}, key=str.casefold)
        aliases.append({
            **alias,
            "action": "rename_case_only" if rename_from else "create",
            "rename_from": rename_from,
            "rename_from_alias": rename_from_alias,
            "collection": collection,
            "collection_settings": PurePosixPath("collections", collection, "settings.conf").as_posix(),
            "collection_settings_sha256": hashlib.sha256(settings_bytes).hexdigest(),
            "collection_list_path": list_paths[0],
            "collection_in_main_menu": True,
            "canonical_display_name": asset.get("display_name", asset.get("logical_name", "")),
            "canonical_source_names": list(asset.get("source_names", [])),
            "identity_evidence": f"Coleção confirmada no menu A1 e em sua configuração local; payload identificado pelo catálogo, categoria {asset['category']} e SHA-256.",
        })
        planned_paths.add(alias_path)

    catalog_after["main_artwork_aliases"] = sorted(catalog_after["main_artwork_aliases"], key=lambda item: item["path"].casefold())
    catalog_after["updated_at"] = now()
    canonical_assets = catalog_after.get("canonical_assets", [])
    unique_hashes = {item["sha256"] for item in canonical_assets}
    category_counts = collections.Counter(
        f"{item.get('media_type', 'images')}/{item.get('category', 'miscellaneous')}/{item.get('style', 'standard')}"
        for item in canonical_assets
    )
    catalog_after.setdefault("summary", {}).update({
        "canonical_assets": len(canonical_assets),
        "unique_sha256": len(unique_hashes),
        "relative_layout_aliases": len(catalog_after.get("layout_aliases", [])),
        "main_artwork_aliases": len(catalog_after.get("main_artwork_aliases", [])),
        "static_layout_aliases": len(catalog_after.get("static_layout_aliases", [])),
        "hash_buckets_metadata_only": len({item.get("hash_bucket", item["sha256"][:2]) for item in canonical_assets}),
        "classification_review_required": sum(item.get("classification_status") == "review_required" for item in canonical_assets),
        "category_counts": dict(sorted(category_counts.items())),
    })
    catalog_after_bytes = (json.dumps(catalog_after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    search_index_after = make_search_index(catalog_after)
    manifest_rel = rel(manifest_path.resolve())
    return {
        "schema_version": 1,
        "kind": "retrofe_main_artwork_alias_addition",
        "status": "planned",
        "created_at": now(),
        "operation_id": f"retrofe-main-artwork-aliases-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "project_root": ".",
        "manifest_path": manifest_rel,
        "reason": reason.strip(),
        "method": "Add catalog-backed relative symlinks under registered Main artwork lookup folders, reuse categorized image payloads, update metadata/search index, and create no image payloads.",
        "preexisting_global_audit": global_audit,
        "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "catalog_after_sha256": hashlib.sha256(catalog_after_bytes).hexdigest(),
        "catalog_before_zlib_base64": compressed(catalog_bytes),
        "catalog_after_zlib_base64": compressed(catalog_after_bytes),
        "search_index_before_sha256": hashlib.sha256(search_index_before).hexdigest(),
        "search_index_after_sha256": hashlib.sha256(search_index_after).hexdigest(),
        "search_index_before_zlib_base64": compressed(search_index_before),
        "search_index_after_zlib_base64": compressed(search_index_after),
        "main_artwork_aliases": aliases,
        "summary": {
            "main_artwork_aliases_added": sum(item["action"] == "create" for item in aliases),
            "main_artwork_aliases_case_renamed": sum(item["action"] == "rename_case_only" for item in aliases),
            "main_artwork_aliases_before": len(catalog_before.get("main_artwork_aliases", [])),
            "main_artwork_aliases_after": len(catalog_after.get("main_artwork_aliases", [])),
            "canonical_assets_before": len(canonical_assets),
            "canonical_payloads_added": 0,
            "payload_bytes_copied": 0,
            "global_catalog_audit_state": global_audit["status"],
            "unique_hashes_before": len({item["sha256"] for item in catalog_before.get("canonical_assets", [])}),
            "unique_hashes_after": len(unique_hashes),
            "relative_symlinks_created": sum(item["action"] == "create" for item in aliases),
            "relative_symlinks_case_renamed": sum(item["action"] == "rename_case_only" for item in aliases),
        },
        "rollback": {
            "command": f"rtk python Tools/materializers/organize-asset-library.py rollback --manifest {manifest_rel}",
            "method": "Remove only aliases in this manifest after verifying their symlink target and payload hash; restore catalog/search index snapshots only while their current hashes match this operation.",
            "permanent_deletion": False,
        },
    }


def apply_main_artwork_alias_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_main_artwork_alias_addition" or manifest.get("status") != "planned":
        raise ValueError("Manifesto precisa ser uma inclusão de arte Main planejada")
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo ou índice mudou desde o plano; aplicação bloqueada")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    observed_audit = main_artwork_alias_audit_status(catalog)
    if observed_audit != manifest.get("preexisting_global_audit"):
        raise ValueError("A divergência global de Assets/shared mudou desde o plano; aplicação bloqueada")
    menu_entries = {line.strip() for line in (ROOT / "collections" / "Main" / "menu.txt").read_text(encoding="utf-8").splitlines() if line.strip()}
    canonical_by_path = {item["path"]: item for item in catalog.get("canonical_assets", [])}
    for alias in manifest["main_artwork_aliases"]:
        if os.path.lexists(ROOT / alias["path"]):
            same_case_rename = False
            if alias.get("rename_from"):
                try:
                    same_case_rename = (ROOT / alias["path"]).samefile(ROOT / alias["rename_from"])
                except OSError:
                    same_case_rename = False
            if not same_case_rename:
                raise FileExistsError(f"O caminho ficou ocupado após o plano: {alias['path']}")
        if alias.get("rename_from"):
            old_link = ROOT / alias["rename_from"]
            old_record = next((item for item in catalog.get("main_artwork_aliases", []) if item["path"] == alias["rename_from"]), None)
            if old_record != alias.get("rename_from_alias") or not old_link.is_symlink() or old_link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"O alias que seria renomeado mudou desde o plano: {alias['rename_from']}")
        if alias["collection"] not in menu_entries:
            raise ValueError(f"A coleção saiu do menu Main após o plano: {alias['collection']}")
        settings = ROOT / alias["collection_settings"]
        if settings.is_symlink() or not settings.is_file():
            raise ValueError(f"A configuração local mudou após o plano: {alias['collection_settings']}")
        settings_bytes = settings.read_bytes()
        list_paths = re.findall(r"^\s*list\.path\s*=\s*(.*?)\s*$", settings_bytes.decode("utf-8", errors="replace"), flags=re.IGNORECASE | re.MULTILINE)
        if hashlib.sha256(settings_bytes).hexdigest() != alias["collection_settings_sha256"] or list_paths != [alias["collection_list_path"]]:
            raise ValueError(f"O contexto da coleção mudou após o plano: {alias['collection_settings']}")
        asset = canonical_by_path.get(alias["target"])
        if not asset or asset.get("sha256") != alias["sha256"] or asset.get("size_bytes") != alias["size_bytes"]:
            raise ValueError(f"O registro canônico mudou após o plano: {alias['target']}")
        MATERIALIZER.check_target(ROOT / alias["target"], alias["sha256"], alias["size_bytes"])

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_manifest(path, manifest)
    try:
        for alias in manifest["main_artwork_aliases"]:
            if alias.get("rename_from"):
                os.rename(ROOT / alias["rename_from"], ROOT / alias["path"])
            else:
                link_relative(ROOT / alias["path"], ROOT / alias["target"])
        atomic_write(CATALOG, expanded(manifest["catalog_after_zlib_base64"]))
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_after_zlib_base64"]))
        verification = verify_main_artwork_alias_scope(manifest, applied=True)
        manifest["verification"] = verification
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["catalog_after_sha256"] = digest_file(CATALOG)
        manifest["search_index_after_sha256"] = digest_file(SEARCH_INDEX)
        write_manifest(path, manifest)
        return manifest
    except Exception:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        write_manifest(path, manifest)
        raise


def rollback_main_artwork_alias_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_main_artwork_alias_addition" or manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado de inclusão de aliases não reversível: {manifest.get('status')}")
    catalog_hash, index_hash = digest_file(CATALOG), digest_file(SEARCH_INDEX)
    if catalog_hash not in {manifest["catalog_before_sha256"], manifest["catalog_after_sha256"]}:
        raise ValueError("Catálogo mudou após esta operação; rollback bloqueado")
    if index_hash not in {manifest["search_index_before_sha256"], manifest["search_index_after_sha256"]}:
        raise ValueError("Índice mudou após esta operação; rollback bloqueado")
    for alias in manifest["main_artwork_aliases"]:
        link = ROOT / alias["path"]
        if alias.get("rename_from"):
            old_link = ROOT / alias["rename_from"]
            states = (os.path.lexists(link), os.path.lexists(old_link))
            if states not in {(True, False), (False, True)}:
                raise ValueError(f"Os caminhos do alias renomeado estão ambíguos; rollback bloqueado: {alias['path']}")
            active_link = link if states == (True, False) else old_link
            if not active_link.is_symlink() or active_link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"O alias renomeado mudou de alvo; rollback bloqueado: {alias['path']}")
            MATERIALIZER.check_target(ROOT / alias["target"], alias["sha256"], alias["size_bytes"])
        elif link.is_symlink():
            if link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"O alias mudou de alvo; rollback bloqueado: {alias['path']}")
            MATERIALIZER.check_target(ROOT / alias["target"], alias["sha256"], alias["size_bytes"])
        elif os.path.lexists(link):
            raise ValueError(f"Rollback não sobrescreverá arquivo regular: {alias['path']}")
    for alias in manifest["main_artwork_aliases"]:
        link = ROOT / alias["path"]
        if alias.get("rename_from") and link.is_symlink():
            os.rename(link, ROOT / alias["rename_from"])
        elif link.is_symlink():
            link.unlink()
    if catalog_hash == manifest["catalog_after_sha256"]:
        atomic_write(CATALOG, expanded(manifest["catalog_before_zlib_base64"]))
    if index_hash == manifest["search_index_after_sha256"]:
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_before_zlib_base64"]))
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo ou índice não retornou aos hashes anteriores durante rollback")
    manifest["rollback_verification"] = verify_main_artwork_alias_scope(manifest, applied=False)
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    write_manifest(path, manifest)
    return manifest


def apply_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") == "retrofe_unregistered_diagnostic_retirement":
        sys.path.insert(0, str(ROOT.parents[1] / 'tools/roms'))
        import romtool
        with romtool.media_session.write_lock(vars(romtool)):
            return apply_diagnostic_retirement(path, manifest)
    if manifest.get("kind") == "retrofe_main_artwork_alias_addition":
        return apply_main_artwork_alias_manifest(path)
    if manifest.get("kind") == "retrofe_scoped_common_layout_alias_addition":
        return apply_scoped_common_layout_alias_manifest(path)
    if manifest.get("kind") == "retrofe_scoped_static_asset_addition":
        return apply_scoped_static_asset_manifest(path)
    if manifest.get("kind") == "retrofe_asset_reclassification":
        return apply_reclassification_manifest(path)
    if manifest.get("status") != "planned":
        raise ValueError(f"Manifesto precisa estar planned, estado atual: {manifest.get('status')}")
    if digest_file(CATALOG) != manifest["catalog_before_sha256"]:
        raise ValueError("Catálogo mudou desde o plano; aplicação bloqueada")
    for group in manifest["groups"]:
        source = ROOT / group["canonical_source"]
        MATERIALIZER.check_source(source, group["sha256"], group["size_bytes"])
        target = ROOT / group["target"]
        if os.path.lexists(target):
            raise FileExistsError(f"Destino canônico já existe: {group['target']}")
        for item in group["regular_sources"]:
            if item["path"] != group["canonical_source"]:
                MATERIALIZER.check_source(ROOT / item["path"], group["sha256"], group["size_bytes"])
    for item in manifest.get("source_artwork_moves", []):
        MATERIALIZER.check_target(ROOT / item["source"], item["sha256"], item["size_bytes"])
        if os.path.lexists(ROOT / item["target"]):
            raise FileExistsError(f"Destino de arte-fonte já existe: {item['target']}")
    for alias in manifest.get("existing_alias_targets", []):
        link = ROOT / alias["path"]
        if not link.is_symlink():
            raise ValueError(f"Alias catalogado não é symlink: {alias['path']}")
    for edit in manifest["xml_edits"]:
        if digest_file(ROOT / edit["path"]) != edit["before_sha256"]:
            raise ValueError(f"XML mudou depois do plano: {edit['path']}")

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_manifest(path, manifest)
    try:
        # Move one verified payload per SHA to its readable canonical location.
        for group in manifest["groups"]:
            source, target = ROOT / group["canonical_source"], ROOT / group["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for item in manifest.get("source_artwork_moves", []):
            source, target = ROOT / item["source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        # Retarget previously cataloged lookup aliases.
        for alias in manifest["existing_alias_targets"]:
            link = ROOT / alias["path"]
            if not link.is_symlink():
                raise ValueError(f"Alias existente deixou de ser symlink: {alias['path']}")
            link.unlink()
            target = ROOT / alias["new_target"]
            link_relative(link, target)
        # Replace every remaining Main/layout regular payload with a relative alias.
        for group in manifest["groups"]:
            target = ROOT / group["target"]
            for item in group["regular_sources"]:
                alias = ROOT / item["path"]
                if alias.exists() or alias.is_symlink():
                    if alias.is_symlink():
                        raise ValueError(f"Fonte virou symlink inesperado: {item['path']}")
                    MATERIALIZER.check_source(alias, group["sha256"], group["size_bytes"])
                    alias.unlink()
                link_relative(alias, target)
        for edit in manifest["xml_edits"]:
            atomic_write(ROOT / edit["path"], expanded(edit["after_zlib_base64"]))
        atomic_write(CATALOG, expanded(manifest["catalog_after_zlib_base64"]))
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_after_zlib_base64"]))
        cleanup_empty(SHARED)
        cleanup_empty(SOURCE_ARTWORK)
        verification = verify_library()
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        manifest["verification"] = verification
        manifest["catalog_after_sha256"] = digest_file(CATALOG)
        write_manifest(path, manifest)
        return manifest
    except Exception:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        write_manifest(path, manifest)
        raise


def cleanup_empty(root: Path) -> None:
    if not root.exists():
        return
    for directory in sorted((p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def verify_library() -> dict[str, Any]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    result = MATERIALIZER.audit_catalog(catalog)
    main_aliases = {item["path"] for item in catalog.get("main_artwork_aliases", [])} | {item["path"] for item in catalog.get("source_artwork_aliases", [])}
    main_regular = MATERIALIZER.main_media_files(known_aliases=main_aliases)
    layout_regular = [path for path in MATERIALIZER.layout_media_files() if not is_game_specific_setup_media(path)]
    if main_regular:
        raise ValueError(f"Main ainda contém mídia regular fora de Assets/shared: {[rel(p) for p in main_regular[:5]]}")
    if layout_regular:
        raise ValueError(f"Layouts ainda contêm mídia regular fora de Assets/shared: {[rel(p) for p in layout_regular[:5]]}")
    for asset in catalog.get("canonical_assets", []):
        if asset.get("hash_bucket") != asset["sha256"][:2]:
            raise ValueError(f"Metadado hash_bucket incorreto: {asset['path']}")
        path = PurePosixPath(asset["path"])
        expected_taxonomy = [asset["media_type"]]
        expected_taxonomy.extend(path_slug(part) for part in asset.get("category", "").split("/") if part)
        expected_taxonomy.extend(path_slug(part) for part in asset.get("style", "standard").split("/") if part and part != "standard")
        if path.parts[:2] != ("Assets", "shared") or list(path.parts[2:-1]) != expected_taxonomy:
            raise ValueError(f"Caminho não corresponde aos metadados de tipo/categoria/estilo: {asset['path']}")
        if re.search(r"--[0-9a-f]{8,}\.", path.name, re.IGNORECASE):
            raise ValueError(f"Hash permanece no nome de arquivo canônico: {asset['path']}")
        if any(re.fullmatch(r"[0-9a-f]{2}", part, re.IGNORECASE) for part in path.parts[3:-1]):
            raise ValueError(f"Pasta de balde hash permanece no caminho: {asset['path']}")
    for asset in catalog.get("source_artwork_assets", []):
        path = ROOT / asset["path"]
        MATERIALIZER.check_target(path, asset["sha256"], asset["size_bytes"])
        if asset.get("hash_bucket") != asset["sha256"][:2]:
            raise ValueError(f"Metadado de hash incorreto no source artwork: {asset['path']}")
        relative_path = PurePosixPath(asset["path"])
        expected_taxonomy = [asset.get("media_type", "images")]
        expected_taxonomy.extend(path_slug(part) for part in asset.get("category", "").split("/") if part)
        expected_taxonomy.extend(path_slug(part) for part in asset.get("style", "standard").split("/") if part and part != "standard")
        if relative_path.parts[:2] != ("Assets", "source-artwork") or list(relative_path.parts[2:-1]) != expected_taxonomy:
            raise ValueError(f"Caminho de source artwork não corresponde aos metadados: {asset['path']}")
        if re.search(r"--[0-9a-f]{8,}\.", PurePosixPath(asset["path"]).name, re.IGNORECASE):
            raise ValueError(f"Hash permanece no nome do source artwork: {asset['path']}")
    for asset in catalog.get("setup_media_assets", []):
        path = ROOT / asset["path"]
        if not path.is_file() or digest_file(path) != asset["sha256"]:
            raise ValueError(f"Material de setup mudou ou desapareceu: {asset['path']}")
    rows = list(csv.DictReader(SEARCH_INDEX.open(newline="", encoding="utf-8"))) if SEARCH_INDEX.exists() else []
    expected_rows = len(catalog.get("canonical_assets", [])) + len(catalog.get("source_artwork_assets", [])) + len(catalog.get("setup_media_assets", []))
    if len(rows) != expected_rows:
        raise ValueError(f"Índice de busca tem {len(rows)} linhas; catálogo informa {expected_rows}")
    result.update({"semantic_schema": catalog.get("schema_version"), "canonical_assets": len(catalog.get("canonical_assets", [])),
                   "main_regular_payloads": len(main_regular), "layout_regular_payloads": len(layout_regular),
                   "source_artwork_assets": len(catalog.get("source_artwork_assets", [])),
                   "theme_setup_document_media": len(catalog.get("setup_media_assets", [])),
                   "game_specific_setup_media_preserved": len([path for path in layout_setup_media_files() if is_game_specific_setup_media(path)]),
                   "search_index_rows": len(rows), "hash_bucket_directories": count_hash_bucket_directories()})
    if result["hash_bucket_directories"]:
        raise ValueError(f"Ainda existem {result['hash_bucket_directories']} diretórios de hash nas bibliotecas de assets")
    return result


def count_hash_bucket_directories() -> int:
    count = 0
    for library in (SHARED, SOURCE_ARTWORK):
        if not library.exists():
            continue
        for path in library.rglob("*"):
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{2}", path.name, re.IGNORECASE):
                count += 1
    return count


def is_game_specific_setup_media(path: Path) -> bool:
    parts = {part.casefold() for part in path.relative_to(LAYOUTS).parts}
    return "_setup" in parts and "collections" in parts


def rollback_manifest(path: Path) -> dict[str, Any]:
    retirement = json.loads(path.read_text(encoding="utf-8"))
    if retirement.get("kind") == "retrofe_unregistered_diagnostic_retirement":
        sys.path.insert(0, str(ROOT.parents[1] / 'tools/roms'))
        import romtool
        with romtool.media_session.write_lock(vars(romtool)):
            return rollback_diagnostic_retirement(path, retirement)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") == "retrofe_main_artwork_alias_addition":
        return rollback_main_artwork_alias_manifest(path)
    if manifest.get("kind") == "retrofe_scoped_common_layout_alias_addition":
        return rollback_scoped_common_layout_alias_manifest(path)
    if manifest.get("kind") == "retrofe_scoped_static_asset_addition":
        return rollback_scoped_static_asset_manifest(path)
    if manifest.get("kind") == "retrofe_asset_reclassification":
        return rollback_reclassification_manifest(path)
    if manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado não reversível: {manifest.get('status')}")
    catalog_after = digest_file(CATALOG) if CATALOG.exists() else ""
    if manifest.get("status") == "completed_verified" and catalog_after != manifest.get("catalog_after_sha256"):
        raise ValueError("Catálogo mudou depois da migração; rollback bloqueado para preservar alterações posteriores")
    for edit in manifest.get("xml_edits", []):
        current = ROOT / edit["path"]
        if current.exists() and digest_file(current) != edit["after_sha256"]:
            raise ValueError(f"XML mudou depois da migração; rollback bloqueado: {edit['path']}")
    before_catalog = json.loads(expanded(manifest["catalog_before_zlib_base64"]).decode("utf-8"))
    before_aliases = []
    for field in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases", "source_artwork_aliases"):
        for item in before_catalog.get(field, []):
            before_aliases.append(item)
    before_alias_by_path = {item["path"]: item for item in before_aliases}
    new_aliases = set(manifest.get("new_alias_paths", []))
    # Restore original regular Main/layout files from the canonical payloads.
    for group in manifest["groups"]:
        source_payload = ROOT / group["target"]
        payload_exists = source_payload.exists()
        if payload_exists:
            MATERIALIZER.check_target(source_payload, group["sha256"], group["size_bytes"])
        for item in group["regular_sources"]:
            target = ROOT / item["path"]
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                MATERIALIZER.check_source(target, group["sha256"], group["size_bytes"])
                continue
            if not payload_exists:
                raise FileNotFoundError(f"Payload de rollback não existe: {group['target']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_payload, target)
            os.chmod(target, item["mode"])
            os.utime(target, ns=(item["mtime_ns"], item["mtime_ns"]))
        # Restore the old canonical payload location for every prior shared asset.
        for old_path in group["old_canonical_paths"]:
            target = ROOT / old_path
            if target.exists():
                MATERIALIZER.check_target(target, group["sha256"], group["size_bytes"])
            elif payload_exists:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_payload, target)
        if payload_exists:
            source_payload.unlink()
    for item in manifest.get("source_artwork_moves", []):
        source, target = ROOT / item["source"], ROOT / item["target"]
        if target.exists():
            MATERIALIZER.check_target(target, item["sha256"], item["size_bytes"])
            if source.exists():
                MATERIALIZER.check_target(source, item["sha256"], item["size_bytes"])
                target.unlink()
            else:
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
    # Restore aliases that existed before the operation to their exact old targets.
    for alias_path, item in before_alias_by_path.items():
        link = ROOT / alias_path
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise ValueError(f"Rollback não sobrescreverá arquivo local: {alias_path}")
        link_relative(link, ROOT / item["target"])
    for edit in manifest.get("xml_edits", []):
        atomic_write(ROOT / edit["path"], expanded(edit["before_zlib_base64"]))
    atomic_write(CATALOG, expanded(manifest["catalog_before_zlib_base64"]))
    previous_index = expanded(manifest["search_index_before_zlib_base64"])
    if previous_index:
        atomic_write(SEARCH_INDEX, previous_index)
    else:
        SEARCH_INDEX.unlink(missing_ok=True)
    cleanup_empty(SHARED)
    cleanup_empty(SOURCE_ARTWORK)
    MATERIALIZER.audit_catalog(before_catalog)
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    write_manifest(path, manifest)
    return manifest


def build_reclassification_plan(manifest_path: Path, asset_paths: list[str], category: str, reason: str, alias_renames: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    if manifest_path.exists():
        raise FileExistsError(f"Manifesto já existe e será preservado: {manifest_path}")
    manifest_rel = rel(manifest_path.resolve())
    normalized_category, normalized_style = split_category_style(category)
    if any(part in {".", ".."} for part in normalized_category.split("/") + normalized_style.split("/")):
        raise ValueError(f"Categoria/estilo contém componente de caminho inválido: {category}")
    if not asset_paths or not reason.strip():
        raise ValueError("A reclassificação exige pelo menos um asset e um motivo registrado")
    if len(asset_paths) != len(set(asset_paths)):
        raise ValueError("O mesmo caminho foi informado mais de uma vez")

    catalog_bytes = CATALOG.read_bytes()
    catalog_original = json.loads(catalog_bytes.decode("utf-8"))
    catalog = json.loads(json.dumps(catalog_original))
    catalog_path_repairs = []
    for old_path, new_path in alias_renames or []:
        for alias_path in (old_path, new_path):
            relative_alias = PurePosixPath(alias_path)
            if relative_alias.is_absolute() or ".." in relative_alias.parts or relative_alias.parts[:3] != ("collections", "Main", "medium_artwork"):
                raise ValueError(f"Reconciliação de alias fora de collections/Main/medium_artwork: {alias_path}")
        matches = [item for item in catalog.get("main_artwork_aliases", []) if item.get("path") == old_path]
        if len(matches) != 1:
            raise ValueError(f"Alias Main a reconciliar precisa existir uma única vez no catálogo: {old_path}")
        alias = matches[0]
        old_link = ROOT / old_path
        new_link = ROOT / new_path
        if os.path.lexists(old_link) or not new_link.is_symlink():
            raise ValueError(f"Reconciliação exige caminho antigo ausente e symlink atual: {old_path} -> {new_path}")
        if any(item.get("path") == new_path for item in catalog.get("main_artwork_aliases", [])):
            raise ValueError(f"O caminho atual já está catalogado como alias Main: {new_path}")
        if new_link.resolve(strict=True) != (ROOT / alias["target"]).resolve(strict=True):
            raise ValueError(f"Alias físico atual aponta para outro payload: {new_path}")
        MATERIALIZER.check_target(ROOT / alias["target"], alias["sha256"], alias["size_bytes"])
        old_name = alias.get("logical_name", Path(old_path).name)
        alias["path"] = new_path
        alias["logical_name"] = Path(new_path).name
        for asset in catalog.get("canonical_assets", []):
            if asset.get("sha256") != alias["sha256"]:
                continue
            asset["source_paths"] = sorted({new_path if source == old_path else source for source in asset.get("source_paths", [])}, key=str.casefold)
            asset["source_names"] = sorted(set(asset.get("source_names", [])) | {Path(new_path).name}, key=str.casefold)
        catalog_path_repairs.append({"field": "main_artwork_aliases", "old_path": old_path, "new_path": new_path,
                                     "target": alias["target"], "sha256": alias["sha256"], "size_bytes": alias["size_bytes"],
                                     "old_logical_name": old_name, "new_logical_name": alias["logical_name"],
                                     "physical_symlink": os.readlink(new_link),
                                     "reason": "O symlink físico atual já usa o nome sem prefixo; mantém o mesmo destino e hash. O registro de catálogo estava obsoleto."})
    # Validate the full current library after reconciling only explicitly reviewed catalog paths.
    MATERIALIZER.audit_catalog(catalog)
    index_before = SEARCH_INDEX.read_bytes()
    catalog_after = json.loads(json.dumps(catalog))
    by_path = {item["path"]: item for item in catalog_after.get("canonical_assets", [])}
    aliases = []
    xml_edits: list[dict[str, Any]] = []
    planned_items = []

    for asset_path in asset_paths:
        relative = PurePosixPath(asset_path)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:3] != ("Assets", "shared", "images"):
            raise ValueError(f"Caminho canônico inválido para reclassificação: {asset_path}")
        asset = by_path.get(asset_path)
        if asset is None:
            raise ValueError(f"Asset não encontrado como payload canônico: {asset_path}")
        if asset.get("media_type") != "images":
            raise ValueError(f"A reclassificação solicitada não é imagem: {asset_path}")
        source = ROOT / asset_path
        MATERIALIZER.check_target(source, asset["sha256"], asset["size_bytes"])
        # Preserve the reviewed, readable canonical filename, including a proven path qualifier.
        target = str(PurePosixPath("Assets", "shared", "images", *normalized_category.split("/"), *([] if normalized_style == "standard" else normalized_style.split("/")), source.name))
        target_path = ROOT / target
        if os.path.lexists(target_path):
            raise FileExistsError(f"Destino canônico já existe; não haverá sobrescrita nem cópia duplicada: {target}")
        nearest = target_path.parent
        while not nearest.exists():
            nearest = nearest.parent
        if nearest.stat().st_dev != source.stat().st_dev:
            raise OSError(f"A reclassificação cruzaria dispositivos: {asset_path} -> {target}")
        if target in by_path or any(item.get("target") == target for field in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases") for item in catalog.get(field, [])):
            raise ValueError(f"O destino já está reservado no catálogo: {target}")

        override = {
            "media_type": "images", "category": normalized_category, "style": normalized_style,
            "reason": reason.strip(), "evidence": "Classificação semântica explícita solicitada pelo usuário; confirmada pelos registros de origem e pelos arquivos de imagem.",
            "operation_manifest": manifest_rel,
        }
        old_record = json.loads(json.dumps(asset))
        asset["path"] = target
        asset["category"] = normalized_category
        asset["style"] = normalized_style
        asset["classification_override"] = override
        tags = [dict(item) for item in asset.get("semantic_tags", [])]
        max_weight = max((int(item.get("weight", 0)) for item in tags), default=0)
        tag = next((item for item in tags if item.get("media_type") == "images" and item.get("category") == normalized_category and item.get("style", "standard") == normalized_style), None)
        if tag is None:
            tags.append({"media_type": "images", "category": normalized_category, "style": normalized_style, "weight": max_weight + 1})
        else:
            tag["weight"] = max_weight + 1
        asset["semantic_tags"] = tags
        snap = MATERIALIZER.snapshot(source)
        planned_items.append({
            "source": asset_path, "target": target, "sha256": asset["sha256"], "size_bytes": asset["size_bytes"],
            "device": snap["device"], "inode": snap["inode"], "mtime_ns": snap["mtime_ns"], "mode": snap["mode"],
            "media_type": "images", "category_before": old_record.get("category", ""), "category_after": normalized_category,
            "style_before": old_record.get("style", "standard"), "style_after": normalized_style,
            "logical_name": asset.get("logical_name", source.stem), "path_qualifier": asset.get("path_qualifier", ""),
            "classification_override": override,
        })

    digest_targets = {item["sha256"]: item["target"] for item in planned_items}
    old_targets = {item["source"]: item["target"] for item in planned_items}
    for field in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases", "source_artwork_aliases"):
        for alias in catalog_after.get(field, []):
            new_target = old_targets.get(alias.get("target", ""))
            if new_target is None:
                continue
            if alias.get("sha256") not in digest_targets:
                raise ValueError(f"Alias aponta para um asset selecionado, mas seu hash não confere: {field}: {alias.get('path')}")
            alias_path = ROOT / alias["path"]
            if not alias_path.is_symlink():
                raise ValueError(f"Alias registrado não é symlink: {alias['path']}")
            resolved = alias_path.resolve(strict=True)
            if resolved != (ROOT / alias["target"]).resolve(strict=True):
                raise ValueError(f"Alias resolve para payload diferente do catálogo: {alias['path']}")
            old_link = os.readlink(alias_path)
            new_link = os.path.relpath(ROOT / new_target, start=alias_path.parent)
            aliases.append({"field": field, "path": alias["path"], "old_target": alias["target"], "new_target": new_target,
                            "sha256": alias["sha256"], "size_bytes": alias["size_bytes"], "old_link": old_link, "new_link": new_link})
            alias["target"] = new_target
            alias["category"] = normalized_category
            alias["style"] = normalized_style
            alias["media_type"] = "images"
            if "semantic_type" in alias:
                alias["semantic_type"] = normalized_category

    # Update literal static XML consumers of these canonical payloads, preserving comments and formatting.
    for xml_path in sorted(LAYOUTS.rglob("*.xml"), key=lambda item: item.as_posix().casefold()):
        if AUDITOR.is_archived_xml(xml_path, LAYOUTS):
            continue
        before = xml_path.read_bytes()
        text = before.decode("utf-8", errors="surrogateescape")
        parts = COMMENT_RE.split(text)
        changes = []
        relative_parts = xml_path.relative_to(LAYOUTS).parts
        theme_root = LAYOUTS / relative_parts[0]

        def replace_attribute(match: re.Match[str]) -> str:
            attribute, quote, raw_value = match.group(1), match.group(2), match.group(3)
            value = html.unescape(raw_value.strip())
            if not value or value.startswith(("/", "http://", "https://", "data:")) or "\\" in value or any(marker in value for marker in DYNAMIC_MARKERS):
                return match.group(0)
            base = LAYOUTS if xml_path.name.casefold().startswith("splash") and attribute.casefold() == "src" else theme_root
            resolved = (base / value).resolve(strict=False)
            try:
                resolved_rel = resolved.relative_to(ROOT).as_posix()
            except ValueError:
                return match.group(0)
            new_target = old_targets.get(resolved_rel)
            if not new_target:
                return match.group(0)
            updated = os.path.relpath(ROOT / new_target, start=base).replace(os.sep, "/")
            if updated == value:
                return match.group(0)
            changes.append({"attribute": attribute, "from": raw_value, "to": updated, "sha256": next(item["sha256"] for item in planned_items if item["source"] == resolved_rel)})
            return f"{attribute}={quote}{html.escape(updated, quote=False)}{quote}"

        for index in range(0, len(parts), 2):
            parts[index] = ATTRIBUTE_RE.sub(replace_attribute, parts[index])
        after = "".join(parts).encode("utf-8", errors="surrogateescape")
        if before != after:
            ET.fromstring(after)
            xml_edits.append({"path": rel(xml_path), "before_sha256": hashlib.sha256(before).hexdigest(), "after_sha256": hashlib.sha256(after).hexdigest(),
                              "before_zlib_base64": compressed(before), "after_zlib_base64": compressed(after), "changes": changes})

    catalog_after["updated_at"] = now()
    previous_operations = list(catalog_after.get("classification_reclassifications", []))
    if manifest_rel not in previous_operations:
        previous_operations.append(manifest_rel)
    catalog_after["classification_reclassifications"] = previous_operations
    category_counts = collections.Counter(
        f"{item.get('media_type', 'images')}/{item.get('category', 'miscellaneous')}/{item.get('style', 'standard')}"
        for item in catalog_after.get("canonical_assets", [])
    )
    catalog_after.setdefault("summary", {}).update({
        "canonical_assets": len(catalog_after.get("canonical_assets", [])),
        "unique_sha256": len({item["sha256"] for item in catalog_after.get("canonical_assets", [])}),
        "relative_layout_aliases": len(catalog_after.get("layout_aliases", [])),
        "main_artwork_aliases": len(catalog_after.get("main_artwork_aliases", [])),
        "static_layout_aliases": len(catalog_after.get("static_layout_aliases", [])),
        "hash_buckets_metadata_only": len({item.get("hash_bucket", item["sha256"][:2]) for item in catalog_after.get("canonical_assets", [])}),
        "classification_review_required": sum(item.get("classification_status") == "review_required" for item in catalog_after.get("canonical_assets", [])),
        "category_counts": dict(sorted(category_counts.items())),
    })
    catalog_after_bytes = (json.dumps(catalog_after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    index_after = make_search_index(catalog_after)
    return {
        "schema_version": 1, "kind": "retrofe_asset_reclassification", "status": "planned", "created_at": now(),
        "operation_id": f"retrofe-asset-reclassification-{dt.datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}",
        "project_root": ".", "manifest_path": manifest_rel, "reason": reason.strip(),
        "method": "Move each selected canonical payload once, retarget its existing relative aliases, update catalog/search metadata and any literal XML consumers; retain compressed before/after snapshots for rollback.",
        "catalog_before_sha256": hashlib.sha256(catalog_bytes).hexdigest(), "catalog_after_sha256": hashlib.sha256(catalog_after_bytes).hexdigest(),
        "catalog_before_zlib_base64": compressed(catalog_bytes), "catalog_after_zlib_base64": compressed(catalog_after_bytes),
        "search_index_before_sha256": hashlib.sha256(index_before).hexdigest(), "search_index_after_sha256": hashlib.sha256(index_after).hexdigest(),
        "search_index_before_zlib_base64": compressed(index_before), "search_index_after_zlib_base64": compressed(index_after),
        "items": planned_items, "aliases": aliases, "xml_edits": xml_edits, "catalog_path_repairs": catalog_path_repairs,
        "summary": {"payloads_reclassified": len(planned_items), "payloads_moved": len(planned_items), "aliases_retargeted": len(aliases),
                    "xml_files_rewritten": len(xml_edits), "xml_reference_rewrites": sum(len(item["changes"]) for item in xml_edits),
                    "duplicate_payloads_created": 0, "source_payloads_preserved_as_rollback": True},
        "rollback": {"command": f"rtk python Tools/materializers/organize-asset-library.py rollback --manifest {manifest_rel}",
                     "method": "Move only the verified destination payloads back, restore each prior relative alias, XML, catalog and search index from this manifest's before snapshots.",
                     "permanent_deletion": False},
    }


def verify_reclassification_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    canonical_by_hash = {item["sha256"]: item for item in catalog.get("canonical_assets", [])}
    for item in manifest["items"]:
        record = canonical_by_hash.get(item["sha256"])
        if not record or record.get("path") != item["target"] or record.get("category") != item["category_after"] or record.get("style") != item["style_after"]:
            raise ValueError(f"Catálogo não reflete a reclassificação: {item['target']}")
        override = record.get("classification_override", {})
        if override.get("operation_manifest") != manifest["manifest_path"]:
            raise ValueError(f"Catálogo não preservou a origem da decisão: {item['target']}")
        MATERIALIZER.check_target(ROOT / item["target"], item["sha256"], item["size_bytes"])
        if os.path.lexists(ROOT / item["source"]):
            raise ValueError(f"O caminho canônico anterior ainda existe após a movimentação: {item['source']}")
    alias_fields = {field: {item["path"]: item for item in catalog.get(field, [])} for field in ("layout_aliases", "main_artwork_aliases", "static_layout_aliases", "source_artwork_aliases")}
    for alias in manifest["aliases"]:
        record = alias_fields[alias["field"]].get(alias["path"])
        link = ROOT / alias["path"]
        if not record or record.get("target") != alias["new_target"] or record.get("category") != manifest["items"][0]["category_after"]:
            raise ValueError(f"Metadados do alias não foram atualizados: {alias['path']}")
        if not link.is_symlink() or link.resolve(strict=True) != (ROOT / alias["new_target"]).resolve(strict=True):
            raise ValueError(f"Alias não resolve para o novo payload: {alias['path']}")
    index_rows = list(csv.DictReader(SEARCH_INDEX.open(newline="", encoding="utf-8")))
    by_path = {row["canonical_path"]: row for row in index_rows}
    for item in manifest["items"]:
        row = by_path.get(item["target"])
        if not row or row.get("category") != item["category_after"] or row.get("sha256") != item["sha256"]:
            raise ValueError(f"Índice de busca não reflete a classificação: {item['target']}")
    library = verify_library()
    return {"library": library, "reclassified_payloads": len(manifest["items"]), "retargeted_aliases": len(manifest["aliases"]),
            "xml_files": len(manifest["xml_edits"]), "classification_paths_verified": True}


def apply_reclassification_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_asset_reclassification" or manifest.get("status") != "planned":
        raise ValueError("Manifesto precisa ser uma reclassificação planejada")
    if digest_file(CATALOG) != manifest["catalog_before_sha256"] or digest_file(SEARCH_INDEX) != manifest["search_index_before_sha256"]:
        raise ValueError("Catálogo ou índice mudou desde o plano; aplicação bloqueada")
    for item in manifest["items"]:
        source = ROOT / item["source"]
        MATERIALIZER.check_source(source, item["sha256"], item["size_bytes"])
        snap = MATERIALIZER.snapshot(source)
        if any(snap[key] != item[key] for key in ("device", "inode", "mtime_ns")):
            raise ValueError(f"A origem mudou desde o plano: {item['source']}")
        target = ROOT / item["target"]
        if os.path.lexists(target):
            raise FileExistsError(f"Destino passou a existir após o plano: {item['target']}")
    for alias in manifest["aliases"]:
        link = ROOT / alias["path"]
        if not link.is_symlink() or os.readlink(link) != alias["old_link"]:
            raise ValueError(f"Alias mudou desde o plano: {alias['path']}")
    for edit in manifest["xml_edits"]:
        if digest_file(ROOT / edit["path"]) != edit["before_sha256"]:
            raise ValueError(f"XML mudou desde o plano: {edit['path']}")

    manifest["status"] = "applying"
    manifest["apply_started_at"] = now()
    write_manifest(path, manifest)
    try:
        for item in manifest["items"]:
            source, target = ROOT / item["source"], ROOT / item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        for alias in manifest["aliases"]:
            link = ROOT / alias["path"]
            link.unlink()
            link_relative(link, ROOT / alias["new_target"])
        for edit in manifest["xml_edits"]:
            atomic_write(ROOT / edit["path"], expanded(edit["after_zlib_base64"]))
        atomic_write(CATALOG, expanded(manifest["catalog_after_zlib_base64"]))
        atomic_write(SEARCH_INDEX, expanded(manifest["search_index_after_zlib_base64"]))
        manifest["verification"] = verify_reclassification_manifest(manifest)
        manifest["status"] = "completed_verified"
        manifest["completed_at"] = now()
        write_manifest(path, manifest)
        return manifest
    except Exception:
        manifest["status"] = "failed_partial"
        manifest["failed_at"] = now()
        write_manifest(path, manifest)
        raise


def rollback_reclassification_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "retrofe_asset_reclassification" or manifest.get("status") not in {"completed_verified", "failed_partial", "applying"}:
        raise ValueError(f"Estado de reclassificação não reversível: {manifest.get('status')}")
    current_catalog = digest_file(CATALOG)
    if current_catalog not in {manifest["catalog_before_sha256"], manifest["catalog_after_sha256"]}:
        raise ValueError("Catálogo mudou após esta operação; rollback bloqueado para preservar alterações posteriores")
    current_index = digest_file(SEARCH_INDEX)
    if current_index not in {manifest["search_index_before_sha256"], manifest["search_index_after_sha256"]}:
        raise ValueError("Índice mudou após esta operação; rollback bloqueado para preservar alterações posteriores")
    for edit in manifest["xml_edits"]:
        current = ROOT / edit["path"]
        if digest_file(current) not in {edit["before_sha256"], edit["after_sha256"]}:
            raise ValueError(f"XML mudou após esta operação: {edit['path']}")
    for item in manifest["items"]:
        source, target = ROOT / item["source"], ROOT / item["target"]
        if source.exists():
            MATERIALIZER.check_target(source, item["sha256"], item["size_bytes"])
        if target.exists():
            MATERIALIZER.check_target(target, item["sha256"], item["size_bytes"])
        if not source.exists() and not target.exists():
            raise FileNotFoundError(f"Nenhum payload verificável para rollback: {item['source']}")
    for alias in manifest["aliases"]:
        link = ROOT / alias["path"]
        if not link.is_symlink() or os.readlink(link) not in {alias["old_link"], alias["new_link"]}:
            raise ValueError(f"Alias mudou após esta operação: {alias['path']}")

    manifest["rollback_started_at"] = now()
    write_manifest(path, manifest)
    for item in manifest["items"]:
        source, target = ROOT / item["source"], ROOT / item["target"]
        if target.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
        elif target.exists():
            target.unlink()
    for alias in manifest["aliases"]:
        link = ROOT / alias["path"]
        link.unlink()
        link_relative(link, ROOT / alias["old_target"])
    for edit in manifest["xml_edits"]:
        current = ROOT / edit["path"]
        if digest_file(current) == edit["after_sha256"]:
            atomic_write(current, expanded(edit["before_zlib_base64"]))
    atomic_write(CATALOG, expanded(manifest["catalog_before_zlib_base64"]))
    atomic_write(SEARCH_INDEX, expanded(manifest["search_index_before_zlib_base64"]))
    manifest["status"] = "rolled_back_verified"
    manifest["rolled_back_at"] = now()
    manifest["verification"] = verify_library()
    write_manifest(path, manifest)
    return manifest


def build_diagnostic_retirement(path: Path, spec_path: Path, reason: str) -> dict[str, Any]:
    """Quarantine explicitly reviewed, unregistered prototypes; do not adopt them as product art."""
    if path.exists() or not reason:
        raise ValueError('Manifesto deve ser novo e motivo é obrigatório')
    spec = json.loads(spec_path.read_text())
    catalog = json.loads(CATALOG.read_text())
    known = {item['path'] for values in catalog.values() if isinstance(values, list)
             for item in values if isinstance(item, dict) and 'path' in item}
    backup = path.with_suffix('') / 'rollback'
    items = []
    for value in spec['payloads']:
        p = ROOT / value
        if PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or value in known:
            raise ValueError(f'Origem registrada ou inválida: {value}')
        p.relative_to(SHARED)
        if p.is_symlink() or not p.is_file() or p.resolve() != p.absolute():
            raise ValueError(f'PNG local regular necessário: {value}')
        validate_external_png(p)
        items.append({'source': value, 'target': rel(backup / value), 'sha256': digest_file(p), 'size_bytes': p.stat().st_size})
    if len({x['source'] for x in items}) != len(items):
        raise ValueError('Payload repetido')
    payloads = {(ROOT / x['source']).resolve() for x in items}
    aliases = []
    for value in spec['aliases']:
        p = ROOT / value
        if PurePosixPath(value).is_absolute() or '..' in PurePosixPath(value).parts or value in known:
            raise ValueError(f'Alias registrado ou inválido: {value}')
        p.relative_to(MAIN_ROOT)
        if p.parent.resolve() != p.parent.absolute():
            raise ValueError('Diretório de alias não pode atravessar symlink')
        if not p.is_symlink() or p.resolve() not in payloads:
            raise ValueError(f'Alias deve apontar exclusivamente a um diagnóstico selecionado: {value}')
        aliases.append({'path': value, 'link_target': os.readlink(p)})
    if not spec.get('visual_evidence') or not spec.get('static_references_checked_none'):
        raise ValueError('Revisão visual e busca de referências estáticas são obrigatórias')
    return {'kind': 'retrofe_unregistered_diagnostic_retirement', 'status': 'planned',
            'operation_id': path.stem, 'manifest_path': rel(path), 'created_at': now(), 'reason': reason,
            'evidence': spec, 'catalog_before_sha256': digest_file(CATALOG),
            'search_index_before_sha256': digest_file(SEARCH_INDEX), 'items': items, 'aliases': aliases,
            'summary': {'diagnostic_payloads_quarantined': len(items), 'unregistered_aliases_retired': len(aliases),
                        'payload_bytes_deleted': 0, 'catalog_changed': False},
            'rollback': {'permanent_deletion': False, 'method': 'Return pinned payloads and reconstruct the exact saved relative aliases, only if their original paths remain free. Catalog/index are unchanged; original unregistered drift is intentionally restored.'}}


def verify_diagnostic_retirement(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if digest_file(CATALOG) != manifest['catalog_before_sha256'] or digest_file(SEARCH_INDEX) != manifest['search_index_before_sha256']:
        raise ValueError('Catálogo/índice alterado desde a operação')
    for item in manifest['items']:
        if os.path.lexists(ROOT / item['source']) or (ROOT / item['target']).is_symlink() or digest_file(ROOT / item['target']) != item['sha256']:
            raise ValueError(f'Quarentena divergente: {item["source"]}')
    if any(os.path.lexists(ROOT / a['path']) for a in manifest['aliases']):
        raise ValueError('Alias reapareceu')
    return {'status': 'verified', 'payloads_preserved': len(manifest['items']), 'global_library': verify_library()}


def rollback_diagnostic_retirement(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT.parents[1] / 'tools/roms'))
    import romtool
    if any(romtool.frontend_processes_running().values()):
        raise ValueError('Encerre os frontends normalmente antes do rollback')
    if digest_file(CATALOG) != manifest['catalog_before_sha256'] or digest_file(SEARCH_INDEX) != manifest['search_index_before_sha256']:
        raise ValueError('Revisar mudanças posteriores antes do rollback')
    for item in manifest['items']:
        if PurePosixPath(item['source']).is_absolute() or '..' in PurePosixPath(item['source']).parts:
            raise ValueError('Origem de rollback inválida')
        original, archived = ROOT / item['source'], ROOT / item['target']
        expected = path.with_suffix('') / 'rollback' / item['source']
        original.relative_to(SHARED)
        if archived != expected or original.resolve(strict=False) != original.absolute() or archived.resolve(strict=False) != archived.absolute() or os.path.lexists(original) or archived.is_symlink() or digest_file(archived) != item['sha256']:
            raise ValueError('Rollback recusa colisão ou payload divergente')
    for alias in manifest['aliases']:
        p = ROOT / alias['path']
        if PurePosixPath(alias['path']).is_absolute() or '..' in PurePosixPath(alias['path']).parts:
            raise ValueError('Alias de rollback inválido')
        p.relative_to(MAIN_ROOT)
        if p.parent.resolve() != p.parent.absolute() or (p.parent / alias['link_target']).resolve(strict=False) not in {(ROOT / x['source']).resolve(strict=False) for x in manifest['items']}:
            raise ValueError('Alvo de alias de rollback fora do escopo')
    if any(os.path.lexists(ROOT / a['path']) for a in manifest['aliases']):
        raise ValueError('Rollback recusa sobrescrever alias')
    for item in manifest['items']:
        os.rename(ROOT / item['target'], ROOT / item['source'])
    for alias in manifest['aliases']:
        (ROOT / alias['path']).symlink_to(alias['link_target'])
    manifest.update(status='rolled_back_to_original_drift', rolled_back_at=now())
    write_manifest(path, manifest)
    return manifest


def apply_diagnostic_retirement(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT.parents[1] / 'tools/roms'))
    import romtool
    if any(romtool.frontend_processes_running().values()):
        raise ValueError('Encerre os frontends normalmente antes da quarentena')
    if manifest['status'] != 'planned' or digest_file(CATALOG) != manifest['catalog_before_sha256'] or digest_file(SEARCH_INDEX) != manifest['search_index_before_sha256']:
        raise ValueError('Plano de quarentena obsoleto')
    # Revalidate the explicit selection against the current catalog and filesystem.
    spec_path = path.with_suffix('.selection.json')
    selection = (json.dumps(manifest['evidence'], ensure_ascii=False, indent=2)+'\n').encode()
    if spec_path.exists() and spec_path.read_bytes() != selection:
        raise ValueError('Seleção anterior divergente; preservada')
    if not spec_path.exists():
        atomic_write(spec_path, selection)
    checked = build_diagnostic_retirement(path.with_name(path.stem+'.validation.json'), spec_path, manifest['reason'])
    if len(checked['items']) != len(manifest['items']):
        raise ValueError('Seleção de payloads divergente')
    for old, current in zip(manifest['items'], checked['items']):
        if any(old[k] != current[k] for k in ('source', 'sha256', 'size_bytes')):
            raise ValueError('Payload mudou desde o plano')
    if checked['aliases'] != manifest['aliases']:
        raise ValueError('Alias mudou desde o plano')
    for item in manifest['items']:
        target = ROOT / item['target']
        expected = path.with_suffix('') / 'rollback' / item['source']
        if target != expected or target.resolve(strict=False) != target.absolute() or os.path.lexists(target):
            raise ValueError('Destino de quarentena ocupado/inválido')
    manifest['status'] = 'applying'
    write_manifest(path, manifest)
    try:
        for item in manifest['items']:
            target = ROOT / item['target']; target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(ROOT / item['source'], target)
        for alias in manifest['aliases']:
            (ROOT / alias['path']).unlink()  # Exact link text is preserved above; no payload is deleted.
        manifest['verification'] = verify_diagnostic_retirement(path, manifest)
    except Exception as exc:
        for item in manifest['items']:
            original, archived = ROOT / item['source'], ROOT / item['target']
            if archived.exists() and not os.path.lexists(original):
                os.rename(archived, original)
        for alias in manifest['aliases']:
            if not os.path.lexists(ROOT / alias['path']):
                (ROOT / alias['path']).symlink_to(alias['link_target'])
        manifest.update(status='failed_restored', error=str(exc))
        write_manifest(path, manifest)
        raise
    manifest.update(status='completed_verified', completed_at=now())
    write_manifest(path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="inventaria e grava um plano reversível")
    plan.add_argument("--manifest", type=Path, required=True)
    plan.add_argument('--retire-unregistered-diagnostics', type=Path, help='spec revisado de protótipos sem registro; preserva payloads em rollback e retira apenas seus aliases não catalogados')
    plan.add_argument("--reclassify-asset", action="append", default=[], metavar="CANONICAL_PATH", help="reclassifica um payload canônico específico; pode ser repetido")
    plan.add_argument("--category", help="nova categoria semântica, por exemplo images/devices/full-setups")
    plan.add_argument("--reason", help="justificativa semântica registrada no catálogo e no manifesto")
    plan.add_argument("--reconcile-main-alias", action="append", default=[], metavar="OLD_PATH=ACTUAL_PATH", help="reconcilia um caminho obsoleto do catálogo com o symlink Main atual; pode ser repetido")
    plan.add_argument("--add-main-logo-alias", action="append", default=[], metavar="COLLECTION=CANONICAL_PATH", help="planeja um alias Main/logo para payload full-set já catalogado; pode ser repetido")
    plan.add_argument("--add-main-logo-variant-alias", action="append", default=[], metavar="ALIAS_TYPE=COLLECTION=CANONICAL_PATH", help="planeja alias Main de estilo full-set em tipo registrado logo_aeon_nox_*; pode ser repetido")
    plan.add_argument("--add-main-artwork-alias", action="append", default=[], metavar="ALIAS_TYPE=COLLECTION=CANONICAL_PATH", help="planeja alias Main para logo, fanart de sistema ou fanart de jogo correspondente usado como fallback de plataforma; pode ser repetido")
    plan.add_argument("--add-layout-common-alias", action="append", default=[], metavar="ALIAS_PATH=CANONICAL_PATH", help="planeja um symlink comum de layout para asset canônico já catalogado; pode ser repetido")
    plan.add_argument("--add-static-layout-asset", action="append", default=[], metavar="LAYOUT_PATH", help="centraliza somente o arquivo estático de layout nomeado, mantendo symlink relativo no layout; pode ser repetido")
    plan.add_argument("--import-static-assets", type=Path, help="spec explícito de PNGs externos revisados; sem aliases externos")
    apply = sub.add_parser("apply", help="aplica o plano após revisão")
    apply.add_argument("--manifest", type=Path, required=True)
    rollback = sub.add_parser("rollback", help="reverte um manifesto verificado")
    rollback.add_argument("--manifest", type=Path, required=True)
    verify = sub.add_parser("verify", help="confere catálogo, aliases, origens e caminhos sem hash")
    verify.add_argument("--manifest", type=Path, help="verifica somente a inclusão estática indicada")
    args = parser.parse_args()
    if args.command == "plan":
        if args.retire_unregistered_diagnostics:
            manifest = build_diagnostic_retirement(args.manifest.resolve(), args.retire_unregistered_diagnostics, args.reason)
        elif args.import_static_assets:
            if args.add_layout_common_alias or args.add_static_layout_asset or args.add_main_logo_alias or args.add_main_logo_variant_alias or args.add_main_artwork_alias or args.reclassify_asset or args.category or args.reconcile_main_alias or not args.reason:
                parser.error("importação externa requer --reason e não pode ser combinada")
            manifest = build_external_static_asset_plan(args.manifest.resolve(), args.import_static_assets, args.reason)
        elif args.add_layout_common_alias:
            if args.add_static_layout_asset or args.add_main_logo_alias or args.add_main_logo_variant_alias or args.add_main_artwork_alias or args.reclassify_asset or args.category or args.reconcile_main_alias:
                parser.error("aliases commonlayout não podem ser combinados com outras operações")
            if not args.reason:
                parser.error("aliases commonlayout requerem --reason")
            alias_specs = []
            for value in args.add_layout_common_alias:
                if "=" not in value:
                    parser.error("--add-layout-common-alias usa ALIAS_PATH=CANONICAL_PATH")
                alias_path, canonical_path = value.split("=", 1)
                alias_specs.append((alias_path, canonical_path))
            manifest = build_scoped_layout_common_alias_plan(args.manifest.resolve(), alias_specs, args.reason)
        elif args.add_static_layout_asset:
            if args.add_layout_common_alias or args.add_main_logo_alias or args.add_main_logo_variant_alias or args.add_main_artwork_alias or args.reclassify_asset or args.category or args.reconcile_main_alias:
                parser.error("inclusões de assets estáticos de layout não podem ser combinadas com outras operações")
            if not args.reason:
                parser.error("inclusões de assets estáticos de layout requerem --reason")
            manifest = build_scoped_static_asset_plan(args.manifest.resolve(), args.add_static_layout_asset, args.reason)
        elif args.add_main_logo_alias or args.add_main_logo_variant_alias or args.add_main_artwork_alias:
            if args.add_layout_common_alias or args.reclassify_asset or args.category or args.reconcile_main_alias:
                parser.error("inclusões de alias Main não podem ser combinadas com reclassificação ou reconciliação")
            if not args.reason:
                parser.error("inclusões de alias Main requerem --reason")
            alias_specs = []
            for value in args.add_main_logo_alias:
                if "=" not in value:
                    parser.error("--add-main-logo-alias usa COLLECTION=CANONICAL_PATH")
                collection, canonical_path = value.split("=", 1)
                alias_specs.append(("logo", collection, canonical_path))
            for value in args.add_main_logo_variant_alias:
                if value.count("=") < 2:
                    parser.error("--add-main-logo-variant-alias usa ALIAS_TYPE=COLLECTION=CANONICAL_PATH")
                alias_type, collection, canonical_path = value.split("=", 2)
                alias_specs.append((alias_type, collection, canonical_path))
            for value in args.add_main_artwork_alias:
                if value.count("=") < 2:
                    parser.error("--add-main-artwork-alias usa ALIAS_TYPE=COLLECTION=CANONICAL_PATH")
                alias_type, collection, canonical_path = value.split("=", 2)
                alias_specs.append((alias_type, collection, canonical_path))
            manifest = build_main_artwork_alias_plan(args.manifest.resolve(), alias_specs, args.reason)
        elif args.reclassify_asset:
            alias_renames = []
            for value in args.reconcile_main_alias:
                if "=" not in value:
                    parser.error("--reconcile-main-alias usa OLD_PATH=ACTUAL_PATH")
                old_path, new_path = value.split("=", 1)
                alias_renames.append((old_path, new_path))
            if not args.category or not args.reason:
                parser.error("--reclassify-asset requer --category e --reason")
            manifest = build_reclassification_plan(args.manifest.resolve(), args.reclassify_asset, args.category, args.reason, alias_renames)
        else:
            if args.category or args.reason or args.reconcile_main_alias:
                parser.error("--category, --reason e --reconcile-main-alias são válidos somente junto com --reclassify-asset")
            manifest = build_plan(args.manifest.resolve())
        write_manifest(args.manifest.resolve(), manifest)
        planned_items = manifest.get("items", manifest.get("groups", []))
        if not planned_items:
            planned_items = manifest.get("aliases", manifest.get("main_artwork_aliases", manifest.get("assets", [])))
        print(json.dumps({"status": manifest["status"], "operation_id": manifest["operation_id"], "manifest": rel(args.manifest.resolve()), "summary": manifest["summary"], "sample_targets": [item["target"] for item in planned_items[:12]]}, ensure_ascii=False, indent=2))
    elif args.command == "apply":
        result = apply_manifest(args.manifest.resolve())
        if result.get("kind") == "retrofe_scoped_common_layout_alias_addition":
            result = {key: result.get(key) for key in ("status", "operation_id", "manifest_path", "reason", "summary", "verification", "completed_at")}
        elif result.get("kind") == "retrofe_scoped_static_asset_addition":
            result = {key: result.get(key) for key in ("status", "operation_id", "manifest_path", "reason", "summary", "verification", "completed_at")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "rollback":
        result = rollback_manifest(args.manifest.resolve())
        if result.get("kind") == "retrofe_scoped_common_layout_alias_addition":
            result = {key: result.get(key) for key in ("status", "operation_id", "manifest_path", "rollback_verification", "rolled_back_at")}
        elif result.get("kind") == "retrofe_scoped_static_asset_addition":
            result = {key: result.get(key) for key in ("status", "operation_id", "manifest_path", "rollback_verification", "rolled_back_at")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.manifest and json.loads(args.manifest.read_text()).get('kind') == 'retrofe_unregistered_diagnostic_retirement':
            result = verify_diagnostic_retirement(args.manifest.resolve(), json.loads(args.manifest.read_text()))
        else:
            result = verify_scoped_static_asset_manifest(args.manifest.resolve()) if args.manifest else verify_library()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
