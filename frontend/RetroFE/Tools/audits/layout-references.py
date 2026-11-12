#!/usr/bin/env python3
"""Audit layout asset paths, menu directions, and control-profile conflicts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from pathlib import PurePosixPath
from xml.sax.saxutils import unescape


def find_project_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "layouts").is_dir() and (candidate / "Manifests").is_dir():
            return candidate
    raise RuntimeError("Não encontrei a raiz do RetroFE acima da pasta do auditor.")


ROOT = find_project_root()
REFERENCE_REPORT = ROOT / "Manifests" / "layout-reference-audit.csv"
NAVIGATION_REPORT = ROOT / "Manifests" / "layout-navigation-audit.csv"
STRUCTURE_REPORT = ROOT / "Manifests" / "layout-xml-structure-audit.csv"
LAYOUT_VARIANT_RE = re.compile(
    r"^layout(?:\s+[0-9]+x[0-9]+)?(?:\s+-\s+[0-9]+)?\.xml$",
    re.IGNORECASE,
)
ATTRIBUTE_RE = re.compile(r"\b(src|font)\s*=\s*([\"'])(.*?)\2", re.IGNORECASE)
MENU_RE = re.compile(r"<menu\b([^>]*)>", re.IGNORECASE | re.DOTALL)
ORIENTATION_RE = re.compile(r"\borientation\s*=\s*([\"'])(.*?)\1", re.IGNORECASE)
CONTROL_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$")
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
DYNAMIC_MARKERS = ("%", "$", "{{", "}}")
FIELDS = ["xml_path", "classification", "line", "attribute", "reference", "expected_path", "status"]
NAV_FIELDS = [
    "scope",
    "theme",
    "path",
    "active",
    "menu_count",
    "horizontal_menus",
    "vertical_menus",
    "implicit_menus",
    "required_controls",
    "missing_controls",
    "control_collisions",
    "status",
]
ASSET_CATALOG = ROOT / "Manifests" / "asset-catalog.json"
STRUCTURE_FIELDS = [
    "xml_path",
    "classification",
    "runtime_selected",
    "raw_xml_status",
    "comment_normalized_status",
    "parse_error",
]


def is_entrypoint(xml_path: Path, layouts_root: Path) -> bool:
    relative = xml_path.relative_to(layouts_root)
    if len(relative.parts) == 2:
        theme_root = layouts_root / relative.parts[0]
        if xml_path.parent != theme_root:
            return False
        return relative.name.casefold() == "splash.xml" or bool(LAYOUT_VARIANT_RE.fullmatch(relative.name))
    if len(relative.parts) == 5:
        return (
            relative.parts[1].casefold() == "collections"
            and relative.parts[3].casefold() == "layout"
            and bool(LAYOUT_VARIANT_RE.fullmatch(relative.name))
        )
    return False


def is_archived_xml(xml_path: Path, layouts_root: Path) -> bool:
    relative = xml_path.relative_to(layouts_root)
    parts = {part.casefold() for part in relative.parts}
    name = relative.name.casefold()
    return (
        "_layout_bkp" in parts
        or "copyofxml" in parts
        or name.endswith(("_old.xml", "_backup.xml", "_bacup.xml"))
        or any(marker in name for marker in ("copia", "copy", "bkp", "backup", "bacup", "ultimo"))
    )


def xml_classification(xml_path: Path, layouts_root: Path) -> str:
    if is_entrypoint(xml_path, layouts_root):
        return "runtime_entrypoint"
    if is_archived_xml(xml_path, layouts_root):
        return "archived_backup"
    relative = xml_path.relative_to(layouts_root)
    if "autochanger" in {part.casefold() for part in relative.parts}:
        return "supplemental_autochanger"
    if "2ndmonitors" in {part.casefold() for part in relative.parts} or any(
        part.casefold().startswith("2ndscreen") for part in relative.parts
    ):
        return "supplemental_second_display"
    if "collections" in {part.casefold() for part in relative.parts}:
        return "supplemental_collection"
    return "supplemental_variant"


def normalized_comment_xml(contents: str) -> str:
    def normalize(match: re.Match[str]) -> str:
        body = match.group()[4:-3]
        while "--" in body:
            body = body.replace("--", "- -")
        if body.endswith("-"):
            body += " "
        return "<!--" + body + "-->"

    return COMMENT_RE.sub(normalize, contents)


def layout_structure_rows() -> list[dict[str, str]]:
    layouts_root = ROOT / "layouts"
    rows: list[dict[str, str]] = []
    for xml_path in sorted(layouts_root.rglob("*.xml")):
        contents = xml_path.read_text(encoding="utf-8", errors="replace")
        try:
            ET.fromstring(contents)
            raw_status = "valid"
            normalized_status = "valid"
            error = ""
        except ET.ParseError as raw_error:
            raw_status = "invalid"
            try:
                ET.fromstring(normalized_comment_xml(contents))
                normalized_status = "valid_after_comment_normalization"
                error = f"comment syntax: {raw_error}"
            except ET.ParseError as normalized_error:
                normalized_status = "invalid"
                error = str(normalized_error)
        rows.append(
            {
                "xml_path": xml_path.relative_to(ROOT).as_posix(),
                "classification": xml_classification(xml_path, layouts_root),
                "runtime_selected": "yes" if is_entrypoint(xml_path, layouts_root) else "no",
                "raw_xml_status": raw_status,
                "comment_normalized_status": normalized_status,
                "parse_error": error,
            }
        )
    return rows


def unresolved_references() -> list[dict[str, str]]:
    layouts_root = ROOT / "layouts"
    rows: list[dict[str, str]] = []

    for xml_path in sorted(layouts_root.rglob("*.xml")):
        if is_archived_xml(xml_path, layouts_root):
            continue

        relative_xml = xml_path.relative_to(ROOT).as_posix()
        path_parts = Path(relative_xml).parts
        if len(path_parts) < 3 or path_parts[0] != "layouts":
            continue
        layout_root = layouts_root / path_parts[1]
        # Runtime traces on the current Linux build show that splash `src`
        # media and `<video src>` use the layouts directory. Font and other
        # regular layout references use the theme directory.
        contents = xml_path.read_text(encoding="utf-8", errors="replace")
        contents = COMMENT_RE.sub(
            lambda match: re.sub(r"[^\n]", " ", match.group()), contents
        )

        for match in ATTRIBUTE_RE.finditer(contents):
            attribute = match.group(1).lower()
            is_splash = xml_path.name.lower().startswith("splash")
            element_start = contents.rfind("<", 0, match.start())
            element_match = re.match(
                r"<\s*([A-Za-z_][A-Za-z0-9_.:-]*)",
                contents[element_start:match.start()],
            )
            is_video_source = (
                attribute == "src"
                and element_match is not None
                and element_match.group(1).casefold() == "video"
            )
            reference_root = (
                layouts_root
                if attribute == "src" and (is_splash or is_video_source)
                else layout_root
            )
            reference = unescape(match.group(3).strip())
            if not reference or reference.startswith(("/", "http://", "https://", "data:")):
                continue
            if "\\" in reference or any(marker in reference for marker in DYNAMIC_MARKERS):
                continue

            reference_name = PurePosixPath(reference).name
            if re.fullmatch(r"\.[A-Za-z0-9]+", reference_name):
                expected = (reference_root / reference).resolve(strict=False)
                rows.append(
                    {
                        "xml_path": relative_xml,
                        "classification": xml_classification(xml_path, layouts_root),
                        "line": str(contents.count("\n", 0, match.start()) + 1),
                        "attribute": attribute,
                        "reference": reference,
                        "expected_path": expected.relative_to(ROOT).as_posix(),
                        "status": "invalid_reference",
                    }
                )
                continue

            expected = (reference_root / reference).resolve(strict=False)
            try:
                expected_relative = expected.relative_to(ROOT).as_posix()
                status = "missing_in_package"
            except ValueError:
                expected_relative = str(expected)
                status = "outside_project"

            if status == "missing_in_package" and expected.is_file():
                continue

            line = contents.count("\n", 0, match.start()) + 1
            rows.append(
                {
                    "xml_path": relative_xml,
                    "classification": xml_classification(xml_path, layouts_root),
                    "line": str(line),
                    "attribute": attribute,
                    "reference": reference,
                    "expected_path": expected_relative,
                    "status": status,
                }
            )

    return sorted(rows, key=lambda row: (row["xml_path"], int(row["line"]), row["attribute"]))


def read_settings() -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in (ROOT / "settings.conf").read_text(encoding="utf-8", errors="replace").splitlines():
        value = line.split("#", 1)[0].strip()
        match = CONTROL_RE.match(value)
        if match:
            settings[match.group(1).lower()] = match.group(2).strip()
    return settings


def read_controls(path: Path) -> dict[str, list[str]]:
    controls: dict[str, list[str]] = {}
    if not path.is_file():
        return controls
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = line.split("#", 1)[0].strip()
        match = CONTROL_RE.match(value)
        if not match:
            continue
        controls[match.group(1).lower()] = [
            token.strip() for token in match.group(2).split(",") if token.strip()
        ]
    return controls


def control_collisions(controls: dict[str, list[str]]) -> list[str]:
    bound_actions: dict[str, set[str]] = {}
    display_names: dict[str, str] = {}
    for action, tokens in controls.items():
        for token in tokens:
            normalized = token.casefold()
            bound_actions.setdefault(normalized, set()).add(action)
            display_names[normalized] = token
    return [
        f"{display_names[key]}:{'|'.join(sorted(actions))}"
        for key, actions in sorted(bound_actions.items())
        if len(actions) > 1
    ]


def active_layout_path(theme: str, settings: dict[str, str]) -> Path | None:
    if settings.get("layout") != theme:
        return None
    try:
        width = int(settings.get("horizontal", "0"))
        height = int(settings.get("vertical", "0"))
    except ValueError:
        width = height = 0
    ratio = ""
    if width > 0 and height > 0:
        divisor = math.gcd(width, height)
        ratio = f"{width // divisor}x{height // divisor}"
    monitor = settings.get("screennum0", "0")
    candidates = []
    if ratio:
        candidates.append(f"layout {ratio} - {monitor}.xml")
    candidates.extend([f"layout - {monitor}.xml"])
    if ratio:
        candidates.append(f"layout {ratio}.xml")
    candidates.append("layout.xml")
    theme_root = ROOT / "layouts" / theme
    for name in candidates:
        candidate = theme_root / name
        if candidate.is_file():
            return candidate
    return None


def layout_navigation_rows() -> list[dict[str, str]]:
    layouts_root = ROOT / "layouts"
    settings = read_settings()
    global_controls = read_controls(ROOT / "controls.conf")
    global_collisions = control_collisions(global_controls)
    rows: list[dict[str, str]] = []
    theme_dirs = sorted(path for path in layouts_root.iterdir() if path.is_dir())

    for theme_root in theme_dirs:
        theme = theme_root.name
        active_path = active_layout_path(theme, settings)
        candidates = [
            path for path in sorted(theme_root.rglob("*.xml"))
            if is_entrypoint(path, layouts_root) and path.name.casefold().startswith("layout")
        ]
        for xml_path in candidates:
            contents = xml_path.read_text(encoding="utf-8", errors="replace")
            contents = COMMENT_RE.sub("", contents)
            orientations: list[str] = []
            for match in MENU_RE.finditer(contents):
                orientation = ORIENTATION_RE.search(match.group(1))
                value = orientation.group(2).strip().lower() if orientation else ""
                orientations.append(value if value in {"horizontal", "vertical"} else "")
            horizontal = orientations.count("horizontal")
            vertical = orientations.count("vertical")
            implicit = orientations.count("")
            required = set()
            if horizontal:
                required.update({"left", "right"})
            if vertical:
                required.update({"up", "down"})
            if implicit:
                required.update({"left", "right", "up", "down"})
            missing = sorted(action for action in required if not global_controls.get(action))
            is_active = active_path == xml_path
            if not orientations:
                status = "no_menus"
            elif missing:
                status = "missing_direction_controls"
            elif global_collisions:
                status = "global_control_collisions"
            elif implicit:
                status = "implicit_orientation"
            else:
                status = "ok"
            rows.append(
                {
                    "scope": "layout",
                    "theme": theme,
                    "path": xml_path.relative_to(ROOT).as_posix(),
                    "active": "yes" if is_active else ("conditional" if "collections" in xml_path.parts or "autochanger" in xml_path.parts else "no"),
                    "menu_count": str(len(orientations)),
                    "horizontal_menus": str(horizontal),
                    "vertical_menus": str(vertical),
                    "implicit_menus": str(implicit),
                    "required_controls": ",".join(sorted(required)),
                    "missing_controls": ",".join(missing),
                    "control_collisions": ";".join(global_collisions),
                    "status": status,
                }
            )

    profile_paths = [ROOT / "controls.conf"] + sorted(layouts_root.rglob("controls.conf"))
    for control_path in profile_paths:
        controls = read_controls(control_path)
        collisions = control_collisions(controls)
        relative = control_path.relative_to(ROOT).as_posix()
        theme = control_path.parent.name if control_path != ROOT / "controls.conf" else "*"
        if control_path == ROOT / "controls.conf":
            role = "active_global"
        elif "_setup" in control_path.parts:
            role = "setup_template"
        elif "autochanger" in control_path.parts:
            role = "rotation_profile"
        else:
            role = "theme_profile"
        required = {"left", "right", "up", "down"}
        missing = sorted(action for action in required if not controls.get(action))
        if role == "setup_template" and collisions:
            status = "template_with_collisions"
        elif collisions:
            status = "control_collisions"
        elif missing:
            status = "missing_direction_controls"
        else:
            status = "ok"
        rows.append(
            {
                "scope": "control_profile",
                "theme": theme,
                "path": relative,
                "active": role,
                "menu_count": "",
                "horizontal_menus": "",
                "vertical_menus": "",
                "implicit_menus": "",
                "required_controls": "down,left,right,up",
                "missing_controls": ",".join(missing),
                "control_collisions": ";".join(collisions),
                "status": status,
            }
        )
    return sorted(rows, key=lambda row: (row["scope"], row["theme"], row["path"]))


def shared_asset_integrity() -> tuple[dict[str, int], list[str]]:
    catalog = json.loads(ASSET_CATALOG.read_text(encoding="utf-8"))
    entries = catalog.get("canonical_assets", [])
    expected = {entry["path"]: entry for entry in entries if entry.get("path")}
    actual: dict[str, Path] = {}
    symlinks: list[str] = []
    for path in (ROOT / "Assets" / "shared").rglob("*"):
        if path.is_symlink():
            symlinks.append(path.relative_to(ROOT).as_posix())
        elif path.is_file():
            actual[path.relative_to(ROOT).as_posix()] = path

    issues: list[str] = []
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    issues.extend(f"asset ausente: {path}" for path in missing)
    issues.extend(f"asset sem manifesto: {path}" for path in extra)
    issues.extend(f"symlink não catalogado em Assets/shared: {path}" for path in symlinks)

    digests: dict[str, list[str]] = {}
    for relative, entry in expected.items():
        path = actual.get(relative)
        if path is None:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_digest = digest.hexdigest()
        digests.setdefault(actual_digest, []).append(relative)
        if actual_digest != entry.get("sha256") or path.stat().st_size != entry.get("size_bytes"):
            issues.append(f"hash/tamanho divergente: {relative}")
    for digest, paths in digests.items():
        if len(paths) > 1:
            issues.append(f"hash duplicado {digest}: {', '.join(sorted(paths))}")

    aliases = catalog.get("layout_aliases", [])
    expected_aliases = {entry["path"]: entry for entry in aliases if entry.get("path")}
    if len(expected_aliases) != len(aliases):
        issues.append("catálogo contém caminhos de alias repetidos")

    actual_aliases: set[str] = set()
    common_regular_files: list[str] = []
    for theme_path in (path for path in (ROOT / "layouts").iterdir() if path.is_dir()):
        common_root = theme_path / "collections" / "_common" / "medium_artwork"
        if not common_root.exists():
            continue
        for path in common_root.rglob("*"):
            relative_path = path.relative_to(ROOT).as_posix()
            if path.is_symlink():
                actual_aliases.add(relative_path)
            elif path.is_file():
                common_regular_files.append(relative_path)

    if catalog.get("mode") == "deduplicated":
        for path in common_regular_files:
            issues.append(f"artwork comum ainda é arquivo regular: {path}")
        for path in sorted(set(expected_aliases) - actual_aliases):
            issues.append(f"alias do layout ausente: {path}")
        for path in sorted(actual_aliases - set(expected_aliases)):
            issues.append(f"alias do layout sem catálogo: {path}")
        for alias_path, entry in expected_aliases.items():
            alias = ROOT / alias_path
            target_rel = entry.get("target", "")
            target = ROOT / target_rel
            if target_rel not in expected:
                issues.append(f"alias aponta para asset fora do catálogo: {alias_path} -> {target_rel}")
                continue
            if not alias.is_symlink():
                issues.append(f"alias não é symlink: {alias_path}")
                continue
            literal = alias.readlink()
            if literal.is_absolute():
                issues.append(f"symlink absoluto não portável: {alias_path} -> {literal}")
                continue
            expected_literal = Path(os.path.relpath(target, start=alias.parent))
            if literal != expected_literal:
                issues.append(f"alvo relativo inesperado: {alias_path} -> {literal}; esperado {expected_literal}")
                continue
            try:
                resolved = alias.resolve(strict=True)
                resolved.relative_to(ROOT.resolve())
                if resolved != target.resolve(strict=True):
                    issues.append(f"alias resolve para destino incorreto: {alias_path}")
                if entry.get("sha256") != expected[target_rel].get("sha256") or entry.get("size_bytes") != expected[target_rel].get("size_bytes"):
                    issues.append(f"hash/tamanho do alias diverge do canônico: {alias_path}")
            except (OSError, ValueError):
                issues.append(f"alias quebrado ou fora do projeto: {alias_path}")

        root_images = [path for path in (ROOT / "Assets" / "shared" / "images").iterdir() if path.is_file() and not path.is_symlink()]
        issues.extend(f"imagem sem categoria em Assets/shared/images/: {path.name}" for path in root_images)

    return {
        "manifest_entries": len(entries),
        "canonical_paths": len(expected),
        "actual_files": len(actual),
        "unique_hashes": len(digests),
        "catalogued_layout_aliases": len(expected_aliases),
        "actual_layout_aliases": len(actual_aliases),
        "regular_common_artwork_files": len(common_regular_files),
    }, issues


def serialize(rows: list[dict[str, str]]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def serialize_structure(rows: list[dict[str, str]]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=STRUCTURE_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare the generated report with the committed manifest without writing",
    )
    args = parser.parse_args()

    generated = serialize(unresolved_references())
    navigation = serialize_navigation(layout_navigation_rows())
    structure = serialize_structure(layout_structure_rows())
    asset_summary, asset_issues = shared_asset_integrity()
    if asset_issues:
        for issue in asset_issues:
            print(f"[FALTA] {issue}", file=sys.stderr)
        return 1
    print(
        "[OK] Assets compartilhados conferidos: "
        f"{asset_summary['actual_files']}/{asset_summary['canonical_paths']} arquivos, "
        f"{asset_summary['unique_hashes']} hashes únicos, "
        f"{asset_summary['actual_layout_aliases']} aliases relativos conferidos."
    )
    if args.check:
        reports = [
            (REFERENCE_REPORT, generated),
            (NAVIGATION_REPORT, navigation),
            (STRUCTURE_REPORT, structure),
        ]
        stale = [path for path, contents in reports if not path.is_file() or path.read_text(encoding="utf-8") != contents]
        if stale:
            for path in stale:
                print(f"[FALTA] Relatório desatualizado: {path}", file=sys.stderr)
            return 1
        print(f"[OK] Auditorias de referências e navegação atualizadas: {REFERENCE_REPORT}, {NAVIGATION_REPORT}")
        return 0

    REFERENCE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    REFERENCE_REPORT.write_text(generated, encoding="utf-8")
    NAVIGATION_REPORT.write_text(navigation, encoding="utf-8")
    STRUCTURE_REPORT.write_text(structure, encoding="utf-8")
    print(f"[OK] Auditorias de estrutura XML, referências e navegação registradas em {REFERENCE_REPORT.parent}")
    return 0


def serialize_navigation(rows: list[dict[str, str]]) -> str:
    from io import StringIO

    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=NAV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
