"""Conservative translation/hack name parsing and reversible metadata registry."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path
from functools import lru_cache

try:
    from . import media_session, portable_emulator
except ImportError:
    import media_session
    import portable_emulator


REGISTRY_RELATIVE = Path("metadata/rom-variants.jsonl")
RECORD_SCHEMA_VERSION = "1.0"
_OPEN_TO_CLOSE = {"[": "]", "(": ")", "{": "}"}
_CLOSE_TO_OPEN = {value: key for key, value in _OPEN_TO_CLOSE.items()}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SYSTEM_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_BANNED_FILENAME_CHARS = set('<>:"/\\|?*')
_RESERVED_WINDOWS_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                           *(f"lpt{i}" for i in range(1, 10))}
_TOOL_ROOT = Path(__file__).resolve().parent


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb", buffering=0) as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _file_snapshot(path: Path) -> dict:
    st = path.lstat()
    return {"device": st.st_dev, "inode": st.st_ino, "size_bytes": st.st_size,
            "mtime_ns": st.st_mtime_ns, "mode": stat.S_IMODE(st.st_mode),
            "is_symlink": stat.S_ISLNK(st.st_mode), "is_regular": stat.S_ISREG(st.st_mode)}


def _snapshot_matches(path: Path, expected: dict) -> bool:
    try:
        actual = _file_snapshot(path)
    except OSError:
        return False
    return all(actual.get(key) == expected.get(key)
               for key in ("device", "inode", "size_bytes", "mtime_ns", "mode", "is_symlink", "is_regular"))


def _extract_groups(name: str) -> tuple[list[dict], list[str]]:
    stack: list[tuple[str, int]] = []
    groups: list[dict] = []
    malformed: list[str] = []
    for index, char in enumerate(name):
        if char in _OPEN_TO_CLOSE:
            stack.append((char, index))
        elif char in _CLOSE_TO_OPEN:
            if stack and stack[-1][0] == _CLOSE_TO_OPEN[char]:
                opener, start = stack.pop()
                if not stack:
                    groups.append({"delimiter": opener + char, "raw": name[start:index + 1],
                                   "content": name[start + 1:index], "complete": True,
                                   "start": start, "end": index + 1})
            else:
                malformed.append(f"unmatched_closing_{char}")
    for opener, start in stack:
        malformed.append(f"unclosed_{opener}")
        groups.append({"delimiter": opener, "raw": name[start:], "content": name[start + 1:],
                       "complete": False, "start": start, "end": len(name)})
    groups.sort(key=lambda item: (item["start"], -item["end"]))
    return groups, sorted(set(malformed))


def _known_language(raw: str, aliases: dict[str, str]) -> str | None:
    value = raw.strip().casefold().replace("_", "-")
    normalized_aliases = sorted(aliases, key=len, reverse=True)
    for alias in normalized_aliases:
        normalized = alias.casefold().replace("_", "-")
        if not value.startswith(normalized):
            continue
        remainder = value[len(normalized):]
        if not remainder or remainder[0] in " ._-+0123456789":
            return aliases[alias]
    return None


def _language_prefix(raw: str, aliases: dict[str, str]) -> tuple[str | None, str, str]:
    value = raw.casefold().replace("_", "-")
    for alias in sorted(aliases, key=len, reverse=True):
        normalized = alias.casefold().replace("_", "-")
        if not value.startswith(normalized):
            continue
        remainder = value[len(normalized):]
        if not remainder or remainder[0] in " ._-+0123456789":
            return aliases[alias], raw[:len(alias)], raw[len(alias):]
    return None, "", raw


def _unique_rows(rows: list[dict]) -> list[dict]:
    unique = []
    seen = set()
    for item in rows:
        marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            unique.append(item)
    return unique


def parse_variant_filename(filename: str, policy: dict | None = None) -> dict:
    """Return candidates from legacy filename markers; none are confirmed facts."""
    policy = policy or {}
    aliases = policy.get("language_aliases", {})
    stem = Path(filename).stem
    groups, malformed = _extract_groups(stem)
    raw_groups = []
    kind_candidates: list[str] = []
    language_candidates: list[dict] = []
    version_candidates: list[dict] = []
    credit_candidates: list[dict] = []
    release_tags: list[dict] = []

    def add_kind(value: str, raw: str) -> None:
        item = {"value": value, "raw": raw, "source": "filename", "status": "candidate_only"}
        if item not in kind_candidates:
            kind_candidates.append(item)

    def add_language(raw_value: str, raw_token: str) -> None:
        candidate = _known_language(raw_value, aliases)
        if candidate:
            item = {"tag_candidate": candidate, "raw_value": raw_value,
                    "raw_token": raw_token, "source": "filename", "status": "candidate_only"}
            if item not in language_candidates:
                language_candidates.append(item)

    for group in groups:
        content = group["content"].strip()
        raw_groups.append({"raw": group["raw"], "complete": group["complete"],
                           "delimiter": group["delimiter"]})
        if not content:
            continue

        # Console release labels are distinct from translation and hack tags.
        # Preserve them as filename-only candidates so later identity checks
        # decide whether a payload belongs in a variant folder.
        for kind, markers in policy.get("variant_kind_markers", {}).items():
            if any(re.search(rf"(?i)(?<![\w]){re.escape(str(marker))}(?![\w])", content)
                   for marker in markers):
                add_kind(str(kind), group["raw"])

        t_match = re.match(r"(?i)^T([+-])\s*(.+)$", content)
        if t_match:
            add_kind("translation", group["raw"])
            rest = t_match.group(2).strip()
            mapped, language_raw, suffix = _language_prefix(rest, aliases)
            if mapped:
                add_language(language_raw, group["raw"])
                suffix = suffix.strip(" _-")
                tagged_version = re.match(r"(?i)^v?(\d+(?:\.\d+)+)(?:[_ -]+(.*))?$", suffix)
                if tagged_version:
                    version_candidates.append({"raw_value": tagged_version.group(1),
                                               "raw_token": group["raw"],
                                               "role_candidate": "release_or_rom_revision",
                                               "source": "filename", "status": "candidate_only"})
                    suffix = (tagged_version.group(2) or "").strip()
                else:
                    tagged_version = re.search(r"(?i)(?:^|[_ -])v?(\d+(?:\.\d+)+)$", suffix)
                    if tagged_version:
                        version_candidates.append({"raw_value": tagged_version.group(1),
                                                   "raw_token": group["raw"],
                                                   "role_candidate": "release_or_rom_revision",
                                                   "source": "filename", "status": "candidate_only"})
                        suffix = suffix[:tagged_version.start()].strip(" _-")
                if suffix:
                    credit_candidates.append({"raw_value": suffix, "raw_token": group["raw"],
                                              "role_candidate": "translator_or_group",
                                              "source": "filename", "status": "candidate_only"})
            else:
                lang_match = re.match(r"(?i)^([a-z]{2,8})(.*)$", rest)
                if lang_match:
                    language_raw, suffix = lang_match.group(1), lang_match.group(2)
                    if language_raw.casefold() in {"br", "bra"}:
                        add_language(language_raw, group["raw"])
                        if suffix:
                            credit_candidates.append({"raw_value": suffix.strip(" _-"), "raw_token": group["raw"],
                                                      "role_candidate": "translator_or_group",
                                                      "source": "filename", "status": "candidate_only"})
                    elif suffix:
                        credit_candidates.append({"raw_value": rest, "raw_token": group["raw"],
                                                  "role_candidate": "translator_or_group",
                                                  "source": "filename", "status": "candidate_only"})
                    else:
                        credit_candidates.append({"raw_value": rest, "raw_token": group["raw"],
                                                  "role_candidate": "translator_or_group",
                                                  "source": "filename", "status": "candidate_only"})
                else:
                    credit_candidates.append({"raw_value": rest, "raw_token": group["raw"],
                                              "role_candidate": "translator_or_group",
                                              "source": "filename", "status": "candidate_only"})

        if re.fullmatch(r"(?i)h[! ]?", content) or re.search(r"(?i)\bhack\b", content):
            add_kind("hack", group["raw"])
        if re.search(r"(?i)\b(?:translated|translation|tradu[cç][aã]o|traduzid[oa]s?)\b", content):
            add_kind("translation", group["raw"])
        translation_language = re.search(
            r"(?i)\b(?:translated|translation|tradu[cç][aã]o|traduzid[oa]s?)\b"
            r"(?:\s+(?:into|to))?[\s:_-]+([a-z]{2,8}(?:[-_][a-z0-9]{2,8})?)",
            content,
        )
        if translation_language:
            raw_language = translation_language.group(1)
            if _known_language(raw_language, aliases):
                add_language(raw_language, group["raw"])
                # Only an explicit "by <name>" phrase is retained as a credit
                # candidate; surrounding title text is never guessed to be a person.
                credit_match = re.search(r"(?i)\bby\s+(.+)$", content)
                if credit_match:
                    credit_value = credit_match.group(1).strip(" _-")
                    credit_value = re.sub(
                        r"(?i)\s+(?:v|ver(?:sion)?)[ _.-]*\d+(?:\.\d+)+(?:[a-z])?\s*$",
                        "", credit_value,
                    ).strip(" _-")
                    if credit_value:
                        credit_candidates.append({"raw_value": credit_value, "raw_token": group["raw"],
                                                  "role_candidate": "translator_or_group",
                                                  "source": "filename", "status": "candidate_only"})
        if re.search(r"(?i)\b(?:dublado|dubbed|voice[ _-]*over)\b", content):
            add_kind("dubbed_or_audio_modification", group["raw"])

        for language_word, language_tag in policy.get("language_words", {}).items():
            if re.search(rf"(?i)(?<![\w]){re.escape(language_word)}(?![\w])", content):
                add_language(language_word, group["raw"])
                if re.search(r"(?i)\b(?:v|ver(?:sion)?)[ _.-]*\d+(?:\.\d+)+(?:[a-z])?", content):
                    add_kind("translation", group["raw"])
                break

        explicit_lang = re.search(r"(?i)(?<![\w])(?:pt[-_ ]?br|ptbr)(?![\w])", content)
        if explicit_lang:
            add_kind("translation", group["raw"])
            add_language(explicit_lang.group(0), group["raw"])

        for marker in re.finditer(
                r"(?i)(?:^|[\s,;])([GVAP])\s*-\s*(.*?)(?=(?:\s+[GVAP]\s*-)|$)", content):
            key, value = marker.group(1).upper(), marker.group(2)
            value = value.strip()
            if not value:
                continue
            release_tags.append({"key": key, "raw_value": value, "raw_token": group["raw"],
                                 "source": "filename", "status": "candidate_only"})
            if key == "V":
                version_candidates.append({"raw_value": value, "raw_token": group["raw"],
                                           "role_candidate": "release_or_rom_revision",
                                           "source": "filename", "status": "candidate_only"})
            elif key == "G":
                credit_candidates.append({"raw_value": value, "raw_token": group["raw"],
                                          "role_candidate": "release_group_or_credit",
                                          "source": "filename", "status": "candidate_only"})

        for version_match in re.finditer(r"(?i)(?:\b(?:v|ver(?:sion)?)[ _.-]*)(\d+(?:\.\d+)+(?:[a-z])?)", content):
            version_candidates.append({"raw_value": version_match.group(0).strip(),
                                       "raw_token": group["raw"], "role_candidate": "release_or_rom_revision",
                                       "source": "filename", "status": "candidate_only"})

    if malformed:
        for issue in malformed:
            if issue.startswith("unclosed_") and re.search(r"(?i)(?:^|[^A-Z0-9])T[+-]|\bhack\b|\btranslated\b", stem):
                add_kind("incomplete_variant_marker", issue)

    # Only explicit marker groups count as variant evidence. This avoids treating
    # ordinary title words such as “Patch” in “Cabbage Patch Kids” as a mod.
    unique_kind_candidates = []
    seen_kinds = set()
    for item in kind_candidates:
        marker = (item["value"], item["raw"])
        if marker not in seen_kinds:
            seen_kinds.add(marker)
            unique_kind_candidates.append(item)
    language_candidates = _unique_rows(language_candidates)
    version_candidates = _unique_rows(version_candidates)
    credit_candidates = _unique_rows(credit_candidates)
    release_tags = _unique_rows(release_tags)
    candidate = bool(unique_kind_candidates)
    return {
        "candidate_by_name": candidate,
        "classification_status": "candidate_by_name_only" if candidate else "no_variant_marker_detected",
        "kind_candidates": unique_kind_candidates,
        "language_candidates": language_candidates,
        "version_candidates": version_candidates,
        "credit_candidates": credit_candidates,
        "release_tags": release_tags,
        "raw_groups": raw_groups,
        "malformed_delimiters": malformed,
        "semantic_confirmation": "none_filename_is_not_proof",
    }


def portable_filename_issues(filename: str) -> list[str]:
    issues = []
    if any(ord(char) < 32 or ord(char) == 127 for char in filename):
        issues.append("control_character")
    if any(char in _BANNED_FILENAME_CHARS for char in filename):
        issues.append("not_portable_to_windows_ntfs")
    if "%" in filename:
        issues.append("retrofe_percent_token_requires_runtime_validation")
    if filename.endswith((" ", ".")):
        issues.append("trailing_space_or_dot")
    if unicodedata.normalize("NFC", filename) != filename:
        issues.append("not_unicode_nfc")
    stem = Path(filename).stem.rstrip(" .").casefold()
    if stem in _RESERVED_WINDOWS_NAMES:
        issues.append("reserved_windows_basename")
    if len(filename.encode("utf-8")) > 255:
        issues.append("basename_exceeds_255_utf8_bytes")
    return sorted(set(issues))


def build_variant_filename(title: str, kind: str, language_tag: str | None = None,
                           version: str | None = None, author: str | None = None,
                           extension: str = "") -> str:
    """Build a candidate basename only from caller-confirmed fields."""
    if kind not in {"translation", "translation_and_hack", "hack", "homebrew", "unlicensed",
                    "bootleg", "aftermarket", "prototype", "demo", "beta", "sample"}:
        raise ValueError(f"Categoria de variante não suportada: {kind}")
    if kind in {"translation", "translation_and_hack"} and not language_tag:
        raise ValueError("Tradução confirmada exige language_tag explícita")
    if language_tag and not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?", language_tag):
        raise ValueError(f"language_tag não está em forma BCP 47 suportada: {language_tag}")
    if version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}", version):
        raise ValueError("Versão deve ser uma string curta, sem separadores de caminho")
    pieces = [unicodedata.normalize("NFC", title).strip()]
    if kind in {"translation", "translation_and_hack"}:
        display_language = language_tag[0].upper() + language_tag[1:]
        pieces.append(f"(Translated {display_language})")
        if kind == "translation_and_hack":
            pieces.append("(Hack)")
    else:
        pieces.append(f"({kind.title()})")
    if version:
        pieces.append(f"(Patch v{version}")
        if author:
            pieces[-1] += f" by {author}"
        pieces[-1] += ")"
    elif author:
        pieces.append(f"(by {author})")
    ext = extension if not extension or extension.startswith(".") else f".{extension}"
    result = " ".join(pieces) + ext
    issues = portable_filename_issues(result)
    if issues:
        raise ValueError("Nome proposto não é portátil: " + ", ".join(issues))
    return result


def variant_directory(kind: str, language_tag: str | None = None, policy: dict | None = None) -> str:
    """Return one-level, platform-local category directory for confirmed variants."""
    policy = policy or _load_policy(_TOOL_ROOT)
    templates = policy.get("variant_directories", {})
    if kind in {"translation", "translation_and_hack"}:
        if not language_tag:
            raise ValueError("Organizar tradução exige language_tag confirmada")
        if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|[0-9]{3}))?", language_tag):
            raise ValueError(f"language_tag não está em forma BCP 47 suportada: {language_tag}")
        slug = language_tag.casefold().replace("_", "-")
        key = "translation_and_hack" if kind == "translation_and_hack" else "translation"
        template = templates.get(key)
        if not isinstance(template, str):
            raise ValueError(f"Política não define diretório para {key}")
        return template.format(language_slug=slug)
    template = templates.get(kind)
    if isinstance(template, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", template):
        return template
    raise ValueError(f"Categoria de variante não suportada: {kind}")


def build_variant_relative_path(system_id: str, kind: str, filename: str,
                                language_tag: str | None = None) -> str:
    if not _SAFE_SYSTEM_RE.fullmatch(system_id):
        raise ValueError(f"system_id inválido: {system_id}")
    if Path(filename).name != filename:
        raise ValueError("filename deve ser apenas basename")
    issues = portable_filename_issues(filename)
    if issues:
        raise ValueError("Basename não portável: " + ", ".join(issues))
    return (Path("roms") / system_id / variant_directory(kind, language_tag) / filename).as_posix()


def _format_signature(path: Path) -> tuple[str, bool | None]:
    with path.open("rb") as stream:
        head = stream.read(0x8040)
    ext = path.suffix.casefold()
    signature = "unknown"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        signature = "7z"
    elif head.startswith(b"Rar!\x1a\x07\x00"):
        signature = "rar4"
    elif head.startswith(b"Rar!\x1a\x07\x01\x00"):
        signature = "rar5"
    elif head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        signature = "zip"
    elif head.startswith(b"MComprHD"):
        signature = "chd"
    elif head.startswith(b"CISO"):
        signature = "cso"
    elif len(head) >= 0x8006 and head[0x8001:0x8006] == b"CD001":
        signature = "iso9660"
    elif head.startswith(b"NES\x1a"):
        signature = "ines"
    elif len(head) >= 0x104 and head[0x100:0x104] == b"NCSD":
        signature = "3ds_ncsd"
    elif len(head) >= 0x104 and head[0x100:0x104] == b"NCCH":
        signature = "3ds_ncch"
    extension_map = {".7z": {"7z"}, ".zip": {"zip"}, ".rar": {"rar4", "rar5"},
                     ".lha": {"lha"}, ".chd": {"chd"}, ".cso": {"cso"},
                     ".3ds": {"3ds_ncsd", "3ds_ncch"}}
    expected = extension_map.get(ext)
    matches = None if not expected or signature == "unknown" else signature in expected
    return signature, matches


def _read_catalog(project_root: Path) -> tuple[dict, list[dict]]:
    path = project_root / "frontend/RetroFE/Config/platform-catalog.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    if str(doc.get("schema_version")) not in {"1", "1.0"} or not isinstance(doc.get("systems"), list):
        raise ValueError(f"Catálogo de plataformas inválido: {path}")
    root_decl = Path(str(doc.get("rom_root", "roms")))
    expected_root = project_root / "roms"
    if root_decl.is_absolute():
        if os.path.abspath(root_decl) != os.path.abspath(expected_root):
            raise ValueError(f"rom_root fora da raiz oficial: {root_decl}")
    else:
        retrofe_root = path.parent.parent
        resolved_root = (retrofe_root / root_decl).resolve(strict=False)
        if os.path.abspath(resolved_root) != os.path.abspath(expected_root):
            raise ValueError(f"rom_root não resolve para a raiz oficial a partir de RetroFE: {root_decl}")
    systems = doc["systems"]
    ids = [str(row.get("id", "")) for row in systems]
    if any(not _SAFE_SYSTEM_RE.fullmatch(item) for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("O catálogo contém system_id ausente, inseguro ou duplicado")
    return doc, systems


def _note_index(project_root: Path, system_id: str, *, hash_notes: bool) -> dict[str, list[dict]]:
    base = project_root / "metadata" / system_id / "patch"
    result: dict[str, list[dict]] = {}
    if not base.is_dir() or base.is_symlink():
        return result
    for path in sorted(base.iterdir(), key=lambda item: item.name.casefold()):
        if path.suffix.casefold() != ".txt":
            continue
        if path.is_symlink() or not path.is_file():
            continue
        entry = {"path": path.relative_to(project_root).as_posix(), "sha256": ""}
        if hash_notes:
            entry["sha256"] = sha256_file(path)
        result.setdefault(path.stem.casefold(), []).append(entry)
    return result


def _registry_location_allowed(relative: Path, system_id: str) -> bool:
    """ROM locations stay under roms/<id>. Review copies may stay under mods/pending/<id>."""
    parts = relative.parts
    if not parts or relative.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        return False
    if len(parts) >= 3 and parts[:2] == ("roms", system_id):
        return True
    return len(parts) >= 4 and parts[:3] == ("mods", "pending", system_id)


def _managed_launch_alias(path: Path, project_root: Path, system_id: str) -> Path | None:
    """Reuse the dispatcher's declared alias contract without creating a second payload record."""
    profile = project_root / 'frontend/RetroFE/Config/profiles/host-linux.json'
    if not profile.is_file() or profile.is_symlink():
        return None
    try:
        data = json.loads(profile.read_text())
        systems = [row for row in data.get('collections', {}).values() if row.get('rom_subdir') == system_id]
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    contracts = {(row.get('runtime_id'), tuple(row.get('launch_inputs', []))) for row in systems}
    if len(contracts) != 1:
        return None
    target, _ = portable_emulator.validate_managed_launch_alias(path, project_root / 'roms' / system_id, systems[0])
    return target


def _scan_review_origin(project_root: Path, system_id: str, extensions: set[str], policy: dict,
                        notes: dict, hash_files: bool, reusable_hashes: dict | None,
                        reuse_operation_id: str, candidate_rows: list[dict],
                        filename_issue_rows: list[dict], coverage: dict) -> None:
    """Count mods/pending/<id> and record name candidates without moving them.

    Files without a variant name marker stay out of the candidate list. The
    coverage count is what makes that gap visible. Nested files are counted
    and are not placement candidates.
    """
    review_root = project_root / "mods" / "pending" / system_id
    if review_root.is_symlink():
        coverage["review_origin_status"] = "symlink_refused"
        return
    if not review_root.is_dir():
        coverage["review_origin_status"] = "absent"
        return
    coverage["review_origin_status"] = "scanned"

    def consider(path: Path, directly_placeable: bool) -> None:
        coverage["review_origin_file_count"] += 1
        if path.is_symlink() or not path.is_file():
            filename_issue_rows.append({
                "system_id": system_id, "path": path.relative_to(project_root).as_posix(),
                "filename": path.name, "issues": ["review_origin_not_regular"],
                "action": "preserve; do not follow or classify"})
            return
        parsed = parse_variant_filename(path.name, policy)
        if not parsed["candidate_by_name"]:
            return
        coverage["review_origin_name_candidates"] += 1
        if not directly_placeable:
            filename_issue_rows.append({
                "system_id": system_id, "path": path.relative_to(project_root).as_posix(),
                "filename": path.name, "issues": ["review_origin_nested"],
                "action": "preserve; organization accepts only mods/pending/<system>/<file>"})
        rel_path = path.relative_to(project_root).as_posix()
        extension_supported = path.suffix.casefold() in extensions
        non_rom_exts = policy.get("non_rom_name_candidate_extensions", {})
        unprofiled_rom_exts = {str(ext).casefold() for ext in policy.get("unprofiled_rom_extensions", [])}
        if path.suffix.casefold() in non_rom_exts:
            artifact_role = non_rom_exts[path.suffix.casefold()]
        elif extension_supported:
            artifact_role = "catalog_supported_content_candidate"
        elif path.suffix.casefold() in unprofiled_rom_exts:
            artifact_role = "unprofiled_rom_candidate"
        else:
            artifact_role = "unclassified_payload_candidate"
        try:
            snapshot = _file_snapshot(path)
            if snapshot["is_symlink"] or not snapshot["is_regular"]:
                raise ValueError("review candidate is not a regular file")
            signature, signature_match = _format_signature(path)
            prior = (reusable_hashes or {}).get(rel_path)
            prior_stat = prior.get("source_stat", {}) if prior else {}
            same_snapshot = bool(prior) and all(
                snapshot.get(key) == prior_stat.get(key)
                for key in ("device", "inode", "size_bytes", "mtime_ns", "mode"))
            prior_digest = str(prior.get("source_file_sha256", "")) if prior else ""
            if same_snapshot and _SHA256_RE.fullmatch(prior_digest):
                digest, hash_status, hash_origin = prior_digest, "reused_prior_inventory", reuse_operation_id
            elif hash_files:
                digest, hash_status, hash_origin = sha256_file(path), "hashed", "current_inventory"
            elif prior:
                raise RuntimeError("snapshot não confere com o inventário de hash reutilizado")
            else:
                digest, hash_status, hash_origin = "", "not_requested", ""
            if snapshot != _file_snapshot(path):
                raise RuntimeError("candidate changed during scan/hash")
            error = ""
        except Exception as exc:
            snapshot = _file_snapshot(path) if os.path.lexists(path) else {}
            digest, signature, signature_match = "", "unknown", None
            hash_status, hash_origin = "failed", ""
            error = f"{type(exc).__name__}: {exc}"
        note_matches = notes.get(Path(path.name).stem.casefold(), [])
        name_issues = portable_filename_issues(path.name)
        candidate_rows.append({
            "system_id": system_id, "path": rel_path, "filename": path.name,
            "extension": path.suffix.casefold(), "extension_supported": extension_supported,
            "artifact_role": artifact_role, "size_bytes": snapshot.get("size_bytes", 0),
            "source_file_sha256": digest, "source_hash_scope": "entire_stored_file_bytes",
            "hash_status": hash_status, "hash_origin_operation_id": hash_origin, "hash_error": error,
            "device": snapshot.get("device", ""), "inode": snapshot.get("inode", ""),
            "mtime_ns": snapshot.get("mtime_ns", ""), "mode": snapshot.get("mode", ""),
            "format_signature": signature, "extension_matches_signature": signature_match,
            "name_evidence": parsed, "name_safety_issues": name_issues,
            "patch_note_refs": [{**note, "link_status": "exact_basename_only_unverified"}
                                for note in note_matches],
            "classification_status": "candidate_by_name_only",
            "content_identity_status": "not_checked",
            "review_origin": "mods/pending",
            "directly_placeable": directly_placeable,
        })

    for path in sorted(review_root.iterdir(), key=lambda item: item.name):
        if path.is_dir() and not path.is_symlink():
            for nested in path.rglob("*"):
                if nested.is_file() or nested.is_symlink():
                    consider(nested, False)
            continue
        consider(path, True)


def scan_library(project_root: Path, *, hash_files: bool, policy: dict,
                 reusable_hashes: dict[str, dict] | None = None,
                 reuse_operation_id: str = "") -> tuple[list[dict], list[dict], list[dict], dict]:
    catalog, systems = _read_catalog(project_root)
    rom_root = project_root / "roms"
    if rom_root.is_symlink() or not rom_root.is_dir():
        raise ValueError(f"Raiz ROM oficial ausente ou simbólica: {rom_root}")
    candidate_rows: list[dict] = []
    coverage_rows: list[dict] = []
    filename_issue_rows: list[dict] = []
    collision_index: dict[tuple[str, str], list[str]] = {}
    name_checks: dict[str, tuple[dict, list[str]]] = {}
    for system in systems:
        system_id = system["id"]
        subdir = Path(str(system.get("rom_subdir", "")))
        root = rom_root / subdir
        coverage = {"system_id": system_id, "display_name": system.get("display_name", system_id),
                    "catalog_kind": system.get("kind", "platform"), "rom_root_present": root.is_dir() and not root.is_symlink(),
                    "total_file_count": 0, "supported_file_count": 0, "unsupported_file_count": 0,
                    "variant_name_candidates": 0, "unsupported_extension_name_candidates": 0,
                    "name_safety_issue_count": 0, "structured_registry_present": True,
                    "review_origin_file_count": 0, "review_origin_name_candidates": 0,
                    "review_origin_status": "not_scanned",
                    "managed_launch_alias_count": 0,
                    "scan_status": "pending", "excluded_reason": ""}
        if system.get("kind") != "platform":
            coverage.update(scan_status="excluded_non_platform", excluded_reason="catalog entry kind is not platform")
            coverage_rows.append(coverage)
            continue
        if (not subdir.parts or subdir.is_absolute() or ".." in subdir.parts
                or len(subdir.parts) != 1 or root.is_symlink()):
            coverage.update(scan_status="blocked_unsafe_platform_root", excluded_reason=str(subdir))
            coverage_rows.append(coverage)
            continue
        if not root.is_dir():
            coverage.update(scan_status="platform_root_missing")
            extensions = {("." + str(ext).lstrip(".")).casefold() for ext in system.get("extensions", [])}
            notes = _note_index(project_root, system_id, hash_notes=hash_files)
            _scan_review_origin(
                project_root, system_id, extensions, policy, notes, hash_files, reusable_hashes,
                reuse_operation_id, candidate_rows, filename_issue_rows, coverage)
            coverage_rows.append(coverage)
            continue
        extensions = {("." + str(ext).lstrip(".")).casefold() for ext in system.get("extensions", [])}
        notes = _note_index(project_root, system_id, hash_notes=hash_files)
        symlink_dirs: list[str] = []
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            kept = []
            for dirname in dirs:
                child = current_path / dirname
                if child.is_symlink():
                    symlink_dirs.append(child.relative_to(project_root).as_posix())
                else:
                    kept.append(dirname)
            dirs[:] = kept
            for filename in files:
                path = current_path / filename
                coverage["total_file_count"] += 1
                extension_supported = path.suffix.casefold() in extensions
                if extension_supported:
                    coverage["supported_file_count"] += 1
                else:
                    coverage["unsupported_file_count"] += 1
                rel_path = path.relative_to(project_root).as_posix()
                name_issues = portable_filename_issues(filename)
                if path.is_symlink():
                    name_issues.append("symlink_file_not_followed")
                    target = _managed_launch_alias(path, project_root, system_id)
                    if target is not None:
                        coverage['managed_launch_alias_count'] += 1
                        filename_issue_rows.append({
                            'system_id': system_id, 'path': rel_path, 'filename': filename,
                            'issues': ['managed_relative_launch_alias'],
                            'action': 'preserve launch relation; payload registry excludes the alias itself',
                            'managed_alias_target': target.relative_to(project_root).as_posix()})
                        continue
                parsed = parse_variant_filename(filename, policy)
                if parsed["malformed_delimiters"]:
                    name_issues.extend(parsed["malformed_delimiters"])
                name_issues = sorted(set(name_issues))
                collision_key = (path.parent.relative_to(project_root).as_posix(),
                                 unicodedata.normalize("NFC", filename).casefold())
                collision_index.setdefault(collision_key, []).append(rel_path)
                name_checks[rel_path] = (parsed, name_issues)
                if name_issues:
                    coverage["name_safety_issue_count"] += 1
                    filename_issue_rows.append({"system_id": system_id, "path": rel_path,
                                                "filename": filename, "issues": name_issues,
                                                "action": "preserve existing basename; block generated rename until reviewed"})
                if not parsed["candidate_by_name"]:
                    continue
                coverage["variant_name_candidates"] += 1
                if not extension_supported:
                    coverage["unsupported_extension_name_candidates"] += 1
                non_rom_exts = policy.get("non_rom_name_candidate_extensions", {})
                unprofiled_rom_exts = {str(ext).casefold() for ext in policy.get("unprofiled_rom_extensions", [])}
                if path.suffix.casefold() in non_rom_exts:
                    artifact_role = non_rom_exts[path.suffix.casefold()]
                elif extension_supported:
                    artifact_role = "catalog_supported_content_candidate"
                elif path.suffix.casefold() in unprofiled_rom_exts:
                    artifact_role = "unprofiled_rom_candidate"
                else:
                    artifact_role = "unclassified_payload_candidate"
                try:
                    snapshot = _file_snapshot(path)
                    if snapshot["is_symlink"]:
                        raise ValueError("candidate symlink is not followed or hashed")
                    if not snapshot["is_regular"]:
                        raise ValueError("candidate is not a regular file")
                    signature, signature_match = _format_signature(path)
                    prior = (reusable_hashes or {}).get(rel_path)
                    prior_stat = prior.get("source_stat", {}) if prior else {}
                    same_snapshot = bool(prior) and all(
                        snapshot.get(key) == prior_stat.get(key)
                        for key in ("device", "inode", "size_bytes", "mtime_ns", "mode"))
                    prior_digest = str(prior.get("source_file_sha256", "")) if prior else ""
                    if same_snapshot and _SHA256_RE.fullmatch(prior_digest):
                        digest = prior_digest
                        hash_status = "reused_prior_inventory"
                        hash_origin = reuse_operation_id
                    elif hash_files:
                        digest = sha256_file(path)
                        hash_status = "hashed"
                        hash_origin = "current_inventory"
                    elif prior:
                        raise RuntimeError("snapshot não confere com o inventário de hash reutilizado")
                    else:
                        digest = ""
                        hash_status = "not_requested"
                        hash_origin = ""
                    after = _file_snapshot(path)
                    if snapshot != after:
                        raise RuntimeError("candidate changed during scan/hash")
                    error = ""
                except Exception as exc:
                    snapshot = _file_snapshot(path) if os.path.lexists(path) else {}
                    digest, signature, signature_match = "", "unknown", None
                    hash_status = "failed"
                    hash_origin = ""
                    error = f"{type(exc).__name__}: {exc}"
                note_matches = notes.get(Path(filename).stem.casefold(), [])
                candidate_rows.append({
                    "system_id": system_id, "path": rel_path, "filename": filename,
                    "extension": path.suffix.casefold(), "extension_supported": extension_supported,
                    "artifact_role": artifact_role, "size_bytes": snapshot.get("size_bytes", 0),
                    "source_file_sha256": digest, "source_hash_scope": "entire_stored_file_bytes",
                    "hash_status": hash_status, "hash_origin_operation_id": hash_origin, "hash_error": error,
                    "device": snapshot.get("device", ""), "inode": snapshot.get("inode", ""),
                    "mtime_ns": snapshot.get("mtime_ns", ""), "mode": snapshot.get("mode", ""),
                    "format_signature": signature, "extension_matches_signature": signature_match,
                    "name_evidence": parsed, "name_safety_issues": name_issues,
                    "patch_note_refs": [{**note, "link_status": "exact_basename_only_unverified"}
                                        for note in note_matches],
                    "classification_status": "candidate_by_name_only",
                    "content_identity_status": "not_checked",
                    "review_origin": "roms",
                    "directly_placeable": True,
                })
        _scan_review_origin(
            project_root, system_id, extensions, policy, notes, hash_files, reusable_hashes,
            reuse_operation_id, candidate_rows, filename_issue_rows, coverage)
        coverage.update(scan_status="scanned", symlink_directories_skipped=len(symlink_dirs),
                        symlink_directory_paths=symlink_dirs)
        coverage_rows.append(coverage)
    for (parent, _normalized_name), paths in collision_index.items():
        if len(paths) < 2:
            continue
        for rel_path in paths:
            parsed, issues = name_checks[rel_path]
            filename = Path(rel_path).name
            issue = "casefold_nfc_collision"
            if issue not in issues:
                filename_issue_rows.append({"system_id": rel_path.split("/", 2)[1], "path": rel_path,
                                            "filename": filename, "issues": [issue],
                                            "collision_paths": sorted(paths),
                                            "action": "block rename/apply until collision is reviewed"})
    summary = {"catalog_entry_count": len(systems),
               "platform_count": sum(row.get("kind") == "platform" for row in systems),
               "non_platform_entry_count": sum(row.get("kind") != "platform" for row in systems),
               "total_file_count": sum(row.get("total_file_count", 0) for row in coverage_rows),
               "supported_file_count": sum(row["supported_file_count"] for row in coverage_rows),
               "candidate_count": len(candidate_rows),
               "unsupported_extension_candidate_count": sum(not row["extension_supported"] for row in candidate_rows),
               "non_rom_name_candidate_count": sum(row["artifact_role"] in {"documentation_or_patch_note", "save_payload"}
                                                   for row in candidate_rows),
               "artifact_role_counts": dict(__import__("collections").Counter(row["artifact_role"] for row in candidate_rows)),
               "candidate_bytes": sum(int(row["size_bytes"]) for row in candidate_rows),
               "hashed_bytes": sum(int(row["size_bytes"]) for row in candidate_rows
                                    if row["hash_status"] == "hashed"),
               "reused_hashed_bytes": sum(int(row["size_bytes"]) for row in candidate_rows
                                          if row["hash_status"] == "reused_prior_inventory"),
               "hash_failures": sum(row["hash_status"] == "failed" for row in candidate_rows),
               "filename_issue_rows": len(filename_issue_rows),
               "variant_candidates_by_platform": sum(bool(row["variant_name_candidates"]) for row in coverage_rows),
               "review_origin_file_count": sum(row.get("review_origin_file_count", 0) for row in coverage_rows),
               "managed_launch_alias_count": sum(row.get('managed_launch_alias_count', 0) for row in coverage_rows),
               "review_origin_name_candidates": sum(row.get("review_origin_name_candidates", 0) for row in coverage_rows)}
    return candidate_rows, coverage_rows, filename_issue_rows, summary


def _csv_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0]) if rows else []
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with temp.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _write_bytes_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    try:
        from .romtool import atomic_write_bytes
    except ImportError:
        from romtool import atomic_write_bytes
    atomic_write_bytes(path, data, mode=mode)


def _validate_registry_bytes(data: bytes) -> list[dict]:
    rows = []
    seen = set()
    for line_number, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            raise ValueError(f"Linha JSONL vazia na linha {line_number}")
        row = json.loads(line.decode("utf-8"))
        required = {"schema_version", "record_id", "system_id", "artifact_sha256", "artifact_hash_scope",
                    "artifact_role", "profile_support_status",
                    "locations", "classification_status", "variant_kind", "language_tag",
                    "language_candidates", "release_version", "version_candidates", "contributors",
                    "credit_candidates", "name_evidence", "patch_note_refs", "format_evidence", "validation"}
        if not isinstance(row, dict) or not required.issubset(row):
            raise ValueError(f"Campos do registry ausentes na linha {line_number}")
        if row["schema_version"] != RECORD_SCHEMA_VERSION:
            raise ValueError(f"Versão de schema inválida na linha {line_number}")
        if not _SAFE_SYSTEM_RE.fullmatch(str(row["system_id"])):
            raise ValueError(f"system_id inválido na linha {line_number}")
        if not _SHA256_RE.fullmatch(str(row["artifact_sha256"])):
            raise ValueError(f"SHA-256 inválido na linha {line_number}")
        if row["record_id"] in seen:
            raise ValueError(f"record_id duplicado na linha {line_number}")
        expected_record_id = _registry_record_id(row["system_id"], row["artifact_sha256"])
        if row["record_id"] != expected_record_id:
            raise ValueError(f"record_id não corresponde ao system_id e SHA-256 na linha {line_number}")
        if row["artifact_role"] not in {"catalog_supported_content_candidate", "unprofiled_rom_candidate",
                                         "unclassified_payload_candidate"}:
            raise ValueError(f"artifact_role não é um payload candidato a ROM na linha {line_number}")
        if row["profile_support_status"] not in {"catalog_supported", "extension_not_in_catalog"}:
            raise ValueError(f"profile_support_status inválido na linha {line_number}")
        seen.add(row["record_id"])
        if not isinstance(row["locations"], list) or not isinstance(row["name_evidence"], dict):
            raise ValueError(f"Estrutura inválida na linha {line_number}")
        rows.append(row)
    return rows


@lru_cache(maxsize=8)
def _load_policy(tool_root: Path) -> dict:
    policy_path = tool_root / "config/variant-policy.json"
    data = json.loads(policy_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "1.0":
        raise ValueError(f"Versão de política de variantes não suportada: {policy_path}")
    return data


def command_inventory(args, api: dict) -> dict:
    project_root = Path(api["PROJECT_ROOT"])
    tool_root = Path(api["TOOL_ROOT"])
    policy = _load_policy(tool_root)
    reusable = {}
    reuse_operation_id = str(getattr(args, "reuse_hashes_from", "") or "")
    if reuse_operation_id:
        previous_path = Path(api["operation_dir"](reuse_operation_id)) / "manifest.json"
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("kind") != "rom_variant_inventory" or not previous.get("hash_requested"):
            raise ValueError("Reuso exige operação rom_variant_inventory com hashes integrais")
        if previous.get("summary", {}).get("hash_failures"):
            raise ValueError("Inventário fonte possui falhas de hash; reuso bloqueado")
        for row in previous.get("items", []):
            reusable[row["path"]] = {
                "source_file_sha256": row.get("source_file_sha256", ""),
                "source_stat": {key: row.get(key) for key in ("device", "inode", "size_bytes", "mtime_ns", "mode")},
            }
    candidates, coverage, safety, summary = scan_library(
        project_root, hash_files=args.hash, policy=policy,
        reusable_hashes=reusable, reuse_operation_id=reuse_operation_id)
    hashed_statuses = {"hashed", "reused_prior_inventory"}
    hash_complete = ((args.hash or bool(reuse_operation_id)) and summary["hash_failures"] == 0
                     and all(row["hash_status"] in hashed_statuses
                             and _SHA256_RE.fullmatch(row["source_file_sha256"] or "") for row in candidates))
    if args.hash and reuse_operation_id:
        hash_mode = "refresh_with_stat_verified_reuse"
    elif args.hash:
        hash_mode = "full"
    elif reuse_operation_id:
        hash_mode = "stat_verified_reuse"
    else:
        hash_mode = "none"
    operation_id = f"rom-variant-inventory-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    opdir = Path(api["operation_dir"](operation_id))
    opdir.mkdir(parents=True, exist_ok=False)
    _write_csv(opdir / "variant-candidates.csv", candidates)
    _write_csv(opdir / "platform-coverage.csv", coverage)
    _write_csv(opdir / "filename-safety.csv", safety)
    status = "completed_with_blockers" if summary["candidate_count"] or summary["hash_failures"] else "completed"
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "rom_variant_inventory",
        "title": "Variant name, file identity and portability inventory", "created_at": api["now"](),
        "completed_at": api["now"](), "status": status, "read_only": True,
        "root": str(project_root), "registry_path": str(project_root / REGISTRY_RELATIVE),
        "scope": {"scanned": "canonical roms/ roots for catalog kind=platform, plus mods/pending/<id> as review origin only",
                  "excluded_catalog_entry": "steam (kind=unclassified; not a platform)",
                  "source_roots_not_scanned": ["legacy_roms", "sdcard", "gngeo", "blastem", "downloads"],
                  "review_origin": "mods/pending/<system_id> is counted and name-candidates are recorded; files are not moved"},
        "hash_requested": bool(hash_complete), "hash_mode": hash_mode,
        "hash_source_operation_id": reuse_operation_id or None, "summary": summary,
        "limits": ["Name markers are candidates, not proof of applied patches or game identity.",
                   "Declared and validated relative launch aliases are reported in coverage; the alias itself is excluded from payload candidates.",
                   "No archives were extracted; file hashes cover stored file bytes.",
                   "Save files and documentation with variant-like names appear in the inventory but are excluded from the ROM variant registry.",
                   "Runtime launch behavior is not tested by this inventory."],
        "items": candidates,
    }
    api["atomic_json"](opdir / "manifest.json", manifest)
    report = [
        "# Inventário de nomes de variantes",
        "",
        f"Operação: {operation_id}",
        f"Estado: {status}; somente leitura da biblioteca.",
        f"Catálogo: {summary['catalog_entry_count']} entradas ({summary['platform_count']} plataformas; {summary['non_platform_entry_count']} não plataforma explicitamente excluída).",
        f"Arquivos nas raízes de plataforma: {summary['total_file_count']}; extensões catalogadas: {summary['supported_file_count']}; candidatos por nome: {summary['candidate_count']} em {summary['variant_candidates_by_platform']} plataformas, incluindo {summary['unsupported_extension_candidate_count']} com extensão fora do perfil.",
        f"Bytes dos candidatos: {summary['candidate_bytes']}; bytes lidos para hash nesta execução: {summary['hashed_bytes']}; bytes com hash anterior reutilizado após conferência de snapshot: {summary['reused_hashed_bytes']}; erros de hash: {summary['hash_failures']}.",
        f"Linhas de exceção de nome/portabilidade: {summary['filename_issue_rows']}.",
        f"Candidatos que são documentação ou saves e ficam fora do registry de ROMs: {summary['non_rom_name_candidate_count']}; papéis: {json.dumps(summary['artifact_role_counts'], ensure_ascii=False, sort_keys=True)}.",
        "Escopo da biblioteca ativa: roms/. mods/pending/<plataforma> entra só como origem de revisão: arquivos sem marcador de nome são contados e ficam fora da lista de candidatos. Nenhum arquivo dessa origem foi movido.",
        "Fontes legacy_roms, sdcard, gngeo, blastem e downloads não foram examinadas nem alteradas.",
        f"Origem de revisão: {summary['review_origin_file_count']} arquivos vistos; {summary['review_origin_name_candidates']} candidatos por nome.",
        "",
        "Candidato por nome não confirma idioma, autoria, patch aplicado, integridade lógica, dependências ou inicialização. Os hashes deste inventário são dos bytes do arquivo armazenado, não do membro de um arquivo compactado.",
        "",
        "Nenhum ROM foi renomeado, movido, extraído, compactado ou removido.",
        "",
        "Artefatos: manifest.json, variant-candidates.csv, platform-coverage.csv e filename-safety.csv.",
    ]
    _write_bytes_atomic(opdir / "report.md", ("\n".join(report) + "\n").encode("utf-8"))
    api["append_event"](operation_id, "variant_inventory_completed", action="variants_inventory", status=status,
                         details={"summary": summary, "hash_requested": bool(hash_complete),
                                  "hash_mode": manifest["hash_mode"],
                                  "report": str(opdir / "report.md")})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": manifest["kind"], "status": status,
            "summary": summary, "manifest": str(opdir / "manifest.json"),
            "report": str(opdir / "report.md"), "platform_coverage": str(opdir / "platform-coverage.csv"),
            "candidate_csv": str(opdir / "variant-candidates.csv"),
            "filename_safety": str(opdir / "filename-safety.csv")}


def _registry_record_id(system_id: str, artifact_sha256: str) -> str:
    return hashlib.sha256(f"{system_id}\0{artifact_sha256}".encode("utf-8")).hexdigest()


def _registry_record(row: dict, old: dict | None, timestamp: str) -> dict:
    parsed = row["name_evidence"]
    record_id = _registry_record_id(row["system_id"], row["source_file_sha256"])
    if old:
        record = dict(old)
        record["last_seen_at"] = timestamp
        record["artifact_size_bytes"] = int(row["size_bytes"])
        record["artifact_role"] = row["artifact_role"]
        record["profile_support_status"] = "catalog_supported" if row["extension_supported"] else "extension_not_in_catalog"
        record["format_evidence"] = {"extension": row["extension"], "signature": row["format_signature"],
                                      "extension_supported": row["extension_supported"],
                                      "extension_matches_signature": row["extension_matches_signature"]}
        record["name_evidence"] = parsed
        record["patch_note_refs"] = row["patch_note_refs"]
        if record.get("classification_status") == "candidate_by_name_only":
            record["language_candidates"] = parsed.get("language_candidates", [])
            record["version_candidates"] = parsed.get("version_candidates", [])
            record["credit_candidates"] = parsed.get("credit_candidates", [])
    else:
        record = {
            "schema_version": RECORD_SCHEMA_VERSION, "record_id": record_id,
            "system_id": row["system_id"], "artifact_sha256": row["source_file_sha256"],
            "artifact_hash_scope": "entire_stored_file_bytes",
            "artifact_size_bytes": int(row["size_bytes"]),
            "artifact_role": row["artifact_role"],
            "profile_support_status": "catalog_supported" if row["extension_supported"] else "extension_not_in_catalog",
            "classification_status": "candidate_by_name_only", "variant_kind": None,
            "language_tag": None, "language_candidates": parsed.get("language_candidates", []),
            "release_version": None, "version_candidates": parsed.get("version_candidates", []),
            "contributors": [], "credit_candidates": parsed.get("credit_candidates", []),
            "name_evidence": parsed, "patch_note_refs": row["patch_note_refs"],
            "format_evidence": {"extension": row["extension"], "extension_supported": row["extension_supported"],
                                 "signature": row["format_signature"],
                                 "extension_matches_signature": row["extension_matches_signature"]},
            "validation": {"container": "not_integrity_tested", "content_identity": "not_checked",
                           "runtime_launch": "not_tested"},
            "created_at": timestamp, "last_seen_at": timestamp, "locations": [],
        }
    current = {"path": row["path"], "filename_original": row["filename"],
               "first_seen_at": timestamp, "last_seen_at": timestamp,
               "file_stat": {key: row[key] for key in ("device", "inode", "size_bytes", "mtime_ns", "mode")}}
    existing = {item.get("path"): item for item in record.get("locations", [])}
    if current["path"] in existing:
        prior = existing[current["path"]]
        current["first_seen_at"] = prior.get("first_seen_at", timestamp)
        existing[current["path"]] = current
        record["locations"] = list(existing.values())
    else:
        record.setdefault("locations", []).append(current)
    record["record_id"] = record_id
    record["schema_version"] = RECORD_SCHEMA_VERSION
    record["artifact_sha256"] = row["source_file_sha256"]
    return record


def _reconcile_registry_locations(record: dict, current_rows: list[dict],
                                  project_root: Path) -> tuple[dict, list[dict]]:
    """Keep only registry locations that still identify the recorded bytes.

    Current inventory paths are refreshed by ``_registry_record`` after this
    function. Other historical paths are retained when their saved stat is
    unchanged, or after a changed snapshot is rehashed and still matches.
    Missing, redirected, or different-content paths are removed from current
    registry state and returned for the operation manifest.
    """
    system_id = str(record["system_id"])
    digest = str(record["artifact_sha256"])
    inventory_by_path = {str(row["path"]): row for row in current_rows}
    inventory_paths = set(inventory_by_path)
    retained = []
    removed = []
    for location in record.get("locations", []):
        relative_text = str(location.get("path", ""))
        relative = Path(relative_text)
        if not _registry_location_allowed(relative, system_id):
            removed.append({"path": relative_text, "reason": "unsafe_registry_location"})
            continue
        if relative_text in inventory_paths:
            current_row = inventory_by_path[relative_text]
            if current_row.get("source_file_sha256") != digest:
                removed.append({"path": relative_text, "reason": "path_now_contains_different_bytes"})
                continue
            refreshed = dict(location)
            refreshed["file_stat"] = {key: current_row[key] for key in
                                      ("device", "inode", "size_bytes", "mtime_ns", "mode")}
            retained.append(refreshed)
            continue
        candidate = project_root / relative
        cursor = project_root
        unsafe_component = False
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                unsafe_component = True
                break
        if unsafe_component:
            removed.append({"path": relative_text, "reason": "symlink_in_registry_path"})
            continue
        try:
            actual = _file_snapshot(candidate)
        except OSError:
            removed.append({"path": relative_text, "reason": "path_missing"})
            continue
        if actual["is_symlink"] or not actual["is_regular"]:
            removed.append({"path": relative_text, "reason": "not_a_regular_file"})
            continue
        saved = location.get("file_stat") or {}
        same_snapshot = all(actual.get(key) == saved.get(key)
                            for key in ("device", "inode", "size_bytes", "mtime_ns", "mode"))
        if not same_snapshot and sha256_file(candidate) != digest:
            removed.append({"path": relative_text, "reason": "path_now_contains_different_bytes"})
            continue
        refreshed = dict(location)
        refreshed["file_stat"] = {key: actual[key] for key in
                                  ("device", "inode", "size_bytes", "mtime_ns", "mode")}
        retained.append(refreshed)
    record = dict(record)
    record["locations"] = sorted(retained, key=lambda item: str(item.get("path", "")).casefold())
    return record, removed


def _serialize_registry(rows: list[dict]) -> bytes:
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
             for row in sorted(rows, key=lambda item: (item["system_id"], item["record_id"]))]
    data = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    _validate_registry_bytes(data)
    return data


def _lossless_storage_inheritance(existing_rows: list[dict],current_rows: list[dict],api: dict) -> tuple[dict,list[dict]]:
    """Carry reviewed metadata across an independently verified archive encoding."""
    old={(r['system_id'],r['artifact_sha256']):r for r in existing_rows}
    wanted={(r['system_id'],r['source_file_sha256']) for r in current_rows if (r['system_id'],r['source_file_sha256']) not in old}
    matches={};proofs=[]
    if not wanted:return matches,proofs
    operations=Path(api['operation_dir']('storage-proof')).parent
    for path in sorted(operations.glob('compression-*/manifest.json')):
        manifest=json.loads(path.read_text())
        if manifest.get('kind')!='compression' or manifest.get('status') not in {'completed_verified','failed_partial'}:continue
        for item in manifest.get('items',[]):
            target=(item.get('system_id'),item.get('after',{}).get('sha256'))
            origin=(item.get('system_id'),item.get('source_sha256'))
            if target not in wanted or origin not in old or item.get('status')!='completed_verified':continue
            if not str(item.get('profile_id','')).startswith('7z-') or not item.get('logical_digest') or item.get('logical_digest')!=item.get('logical_digest_after'):continue
            candidates=matches.setdefault(target,[])
            candidates.append((old[origin],{'operation_id':manifest['operation_id'],'source_artifact_sha256':origin[1],'target_artifact_sha256':target[1],'logical_digest':item['logical_digest'],'source_record_id':old[origin]['record_id']}))
    inherited={}
    for key,values in matches.items():
        record,_=values[0];copied=json.loads(json.dumps(record));copied['locations']=[]
        derived={'schema_version','record_id','system_id','artifact_sha256','artifact_size_bytes','artifact_hash_scope',
                 'artifact_role','profile_support_status','format_evidence','name_evidence','language_candidates',
                 'version_candidates','credit_candidates','patch_note_refs','locations','created_at','last_seen_at',
                 'validation','storage_provenance'}
        confirmed={}
        for prior,_ in values:
            for field,value in prior.items():
                if field in derived or value in (None,[],{},'') or (field=='classification_status' and value=='candidate_by_name_only'):continue
                encoded=json.dumps(value,sort_keys=True,ensure_ascii=False)
                if field in confirmed and confirmed[field][0]!=encoded:
                    raise ValueError(f'Metadados revisados conflitantes após deduplicação lossless: {key[0]} {key[1]} campo {field}; preserve o registry e revise as origens')
                confirmed[field]=(encoded,value)
        copied.update({field:value for field,(_,value) in confirmed.items()})
        copied['validation']={**copied.get('validation',{}),'container':'verified_by_lossless_storage_transition','runtime_launch':'not_retested_after_storage_transition'}
        seen=set()
        for prior,proof in values:
            identity=(proof['operation_id'],proof['source_artifact_sha256'])
            if identity in seen:continue
            seen.add(identity);proof['prior_validation']=prior.get('validation',{})
            copied.setdefault('storage_provenance',[]).append(proof);proofs.append(proof)
        inherited[key]=copied
    return inherited,proofs


def command_plan(args, api: dict) -> dict:
    project_root = Path(api["PROJECT_ROOT"])
    opdir = Path(api["operation_dir"](args.inventory_operation_id))
    inventory_path = opdir / "manifest.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    if inventory.get("kind") != "rom_variant_inventory" or not inventory.get("hash_requested"):
        raise ValueError("Plan exige inventário rom_variant_inventory com --hash")
    all_rows = inventory.get("items", [])
    non_rom_extensions = set(_load_policy(Path(api["TOOL_ROOT"])).get("non_rom_name_candidate_extensions", {}))
    excluded_non_rom_rows = [row for row in all_rows if row["extension"].casefold() in non_rom_extensions]
    rows = [row for row in all_rows if row["extension"].casefold() not in non_rom_extensions]
    if not rows:
        raise ValueError("Nenhum candidato a ROM variante resta após excluir saves e documentação")
    if any(row.get("hash_status") not in {"hashed", "reused_prior_inventory"}
                       or not _SHA256_RE.fullmatch(row.get("source_file_sha256", ""))
                       for row in all_rows):
        raise ValueError("Plano bloqueado: todo candidato precisa ter SHA-256 estável")
    for row in rows:
        path = project_root / row["path"]
        expected = {"device": row["device"], "inode": row["inode"], "size_bytes": row["size_bytes"],
                    "mtime_ns": row["mtime_ns"], "mode": row["mode"], "is_symlink": False, "is_regular": True}
        if not path.is_relative_to(project_root) or not _snapshot_matches(path, expected):
            raise ValueError(f"Arquivo candidato mudou desde o inventário; refaça a varredura: {path}")
    registry_path = project_root / REGISTRY_RELATIVE
    if registry_path.is_symlink():
        raise ValueError(f"Registry simbólico recusado: {registry_path}")
    registry_exists = registry_path.exists()
    before_bytes = registry_path.read_bytes() if registry_exists else b""
    before_sha = hashlib.sha256(before_bytes).hexdigest() if registry_exists else None
    existing_rows = _validate_registry_bytes(before_bytes) if registry_exists else []
    storage_inherited,storage_proofs=_lossless_storage_inheritance(existing_rows,rows,api)
    by_id = {row["record_id"]: row for row in existing_rows}
    timestamp = api["now"]()
    current_rows_by_id: dict[str, list[dict]] = {}
    for row in rows:
        record_id = _registry_record_id(row["system_id"], row["source_file_sha256"])
        current_rows_by_id.setdefault(record_id, []).append(row)
    stale_locations = []
    removed_records = []
    old_location_count = sum(len(row.get("locations", [])) for row in existing_rows)
    retained_historical_locations = 0
    for record_id, record in list(by_id.items()):
        reconciled, removed = _reconcile_registry_locations(
            record, current_rows_by_id.get(record_id, []), project_root)
        replacements = sorted(row["path"] for row in current_rows_by_id.get(record_id, []))
        for item in removed:
            stale_locations.append({"record_id": record_id, "system_id": record["system_id"],
                                    "artifact_sha256": record["artifact_sha256"], **item,
                                    "replacement_inventory_paths": replacements})
        retained_historical_locations += len(reconciled.get("locations", []))
        if not reconciled.get("locations") and not current_rows_by_id.get(record_id):
            removed_records.append({"record_id": record_id, "system_id": record["system_id"],
                                    "artifact_sha256": record["artifact_sha256"],
                                    "reason": "no_current_location_matches_recorded_hash"})
            by_id.pop(record_id)
        else:
            by_id[record_id] = reconciled
    for row in rows:
        record_id = _registry_record_id(row["system_id"], row["source_file_sha256"])
        prior=by_id.get(record_id)
        if prior is None:
            prior=storage_inherited.get((row['system_id'],row['source_file_sha256']))
            if prior is not None:prior={**prior,'created_at':timestamp}
        by_id[record_id] = _registry_record(row,prior,timestamp)
    new_bytes = _serialize_registry(list(by_id.values()))
    operation_id = f"rom-variant-registry-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    change_dir = Path(api["operation_dir"](operation_id))
    change_dir.mkdir(parents=True, exist_ok=False)
    stage_path = change_dir / "registry-proposed.jsonl"
    _write_bytes_atomic(stage_path, new_bytes)
    rollback_dir = change_dir / "rollback"
    rollback_dir.mkdir()
    if registry_exists:
        _write_bytes_atomic(rollback_dir / "registry-before.jsonl", before_bytes,
                            stat.S_IMODE(registry_path.stat().st_mode))
    candidate_snapshots = [{key: row[key] for key in ("system_id", "path", "filename", "size_bytes",
                                                       "source_file_sha256", "artifact_role", "extension_supported",
                                                       "device", "inode", "mtime_ns", "mode")}
                           for row in rows]
    manifest = {
        "schema_version": "1.0", "operation_id": operation_id, "kind": "rom_variant_registry",
        "title": "Prepare structured metadata for name-derived variant candidates",
        "created_at": timestamp, "completed_at": None, "status": "prepared", "read_only": False,
        "registry_path": str(registry_path), "registry_relative_path": REGISTRY_RELATIVE.as_posix(),
        "registry_before_exists": registry_exists, "registry_before_sha256": before_sha,
        "registry_after_sha256": hashlib.sha256(new_bytes).hexdigest(),
        "registry_before_size_bytes": len(before_bytes), "registry_after_size_bytes": len(new_bytes),
        "staged_registry": str(stage_path), "inventory_operation_id": args.inventory_operation_id,
        "candidate_count": len(rows), "inventory_candidate_count": len(all_rows),
        "excluded_non_rom_candidates": [{"system_id": row["system_id"], "path": row["path"],
                                          "artifact_role": row["artifact_role"],
                                          "source_file_sha256": row["source_file_sha256"]}
                                         for row in excluded_non_rom_rows],
        "registry_record_count_before": len(existing_rows),
        "registry_record_count_after": len(by_id), "items": candidate_snapshots,
        "lossless_storage_inheritance":storage_proofs,
        "location_reconciliation": {
            "historical_location_count_before": old_location_count,
            "historical_locations_retained_after_snapshot_or_hash_check": retained_historical_locations,
            "stale_locations_removed": len(stale_locations),
            "records_removed_without_live_locations": len(removed_records),
            "stale_locations": stale_locations,
            "removed_records": removed_records,
            "rule": "keep locations only when inventory finds the path or its saved snapshot/hash still identifies the registry bytes",
        },
        "apply_scope": "write metadata/rom-variants.jsonl only; no ROM/media paths are modified",
        "rollback": {"method": "restore byte-exact registry preimage or preserve newly-created registry in operation rollback/"},
    }
    api["atomic_json"](change_dir / "manifest.json", manifest)
    api["append_event"](operation_id, "variant_registry_plan_prepared", action="variants_plan", status="prepared",
                        source=str(inventory_path), destination=str(registry_path),
                        sha256_before=before_sha or "", sha256_after=manifest["registry_after_sha256"],
                        size_before=len(before_bytes), size_after=len(new_bytes),
                        details={"candidate_count": len(rows), "inventory_operation_id": args.inventory_operation_id,
                                 "staged_registry": str(stage_path),
                                 "location_reconciliation": {
                                     "stale_locations_removed": len(stale_locations),
                                     "records_removed_without_live_locations": len(removed_records)}})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "status": "prepared", "candidate_count": len(rows),
            "registry_path": str(registry_path), "registry_before_sha256": before_sha,
            "registry_after_sha256": manifest["registry_after_sha256"],
            "manifest": str(change_dir / "manifest.json"), "staged_registry": str(stage_path)}


def _safe_registry_path(path: Path, project_root: Path) -> None:
    if not path.is_relative_to(project_root) or path != project_root / REGISTRY_RELATIVE:
        raise ValueError(f"Caminho de registry não permitido: {path}")
    cursor = project_root
    for part in REGISTRY_RELATIVE.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"Componente simbólico no caminho do registry: {cursor}")
        if cursor.exists() and not cursor.is_dir():
            raise ValueError(f"Componente não é diretório: {cursor}")


def command_apply(args, api: dict) -> dict:
    with media_session.write_lock(api):
        return _apply_registry(args, api)


def _apply_registry(args, api: dict) -> dict:
    if not args.apply:
        raise ValueError("Gravação do registry exige --apply explícito")
    project_root = Path(api["PROJECT_ROOT"])
    change_dir = Path(api["operation_dir"](args.operation_id))
    manifest_path = change_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "rom_variant_registry" or manifest.get("status") not in {'prepared', 'applying'}:
        raise ValueError("Operação não está preparada para gravar o registry")
    registry_path = Path(manifest["registry_path"])
    _safe_registry_path(registry_path, project_root)
    if registry_path.is_symlink():
        raise ValueError("Registry simbólico apareceu depois do plano")
    expected_before = manifest.get("registry_before_sha256")
    if registry_path.exists():
        if registry_path.is_symlink() or not registry_path.is_file():
            raise ValueError("Registry atual não é arquivo regular")
        actual_before = sha256_file(registry_path)
        allowed = {expected_before}
        if manifest['status'] == 'applying' and manifest.get('publication_intent') == manifest['registry_after_sha256']:
            allowed.add(manifest['registry_after_sha256'])
        if actual_before not in allowed:
            raise ValueError("Registry mudou depois do plano; gere novo plano antes de aplicar")
        before_mode = int(manifest.get('applied_mode', stat.S_IMODE(registry_path.stat().st_mode)))
        if stat.S_IMODE(registry_path.stat().st_mode) != before_mode:
            raise ValueError('Permissões do registry mudaram durante a publicação')
    else:
        if expected_before is not None:
            raise ValueError("Registry desapareceu depois do plano")
        before_mode = 0o644
    for row in manifest["items"]:
        path = project_root / row["path"]
        expected = {"device": row["device"], "inode": row["inode"], "size_bytes": row["size_bytes"],
                    "mtime_ns": row["mtime_ns"], "mode": row["mode"], "is_symlink": False, "is_regular": True}
        if not path.is_relative_to(project_root) or not _snapshot_matches(path, expected):
            raise ValueError(f"Arquivo mudou depois do plano; nenhum registry foi gravado: {path}")
    stage_path = Path(manifest["staged_registry"])
    if not stage_path.is_relative_to(change_dir) or stage_path.is_symlink() or not stage_path.is_file():
        raise ValueError("Registry proposto ausente ou fora da operação")
    proposed = stage_path.read_bytes()
    if hashlib.sha256(proposed).hexdigest() != manifest["registry_after_sha256"]:
        raise ValueError("SHA-256 do registry proposto diverge do manifesto")
    _validate_registry_bytes(proposed)
    rollback_dir = change_dir / "rollback"
    backup_path = rollback_dir / "registry-before.jsonl"
    if expected_before is not None:
        if not backup_path.is_file() or sha256_file(backup_path) != expected_before:
            raise ValueError("Backup de rollback não confere com a pré-condição do registry")
    manifest.update(status='applying', publication_intent=manifest['registry_after_sha256'], applied_mode=before_mode)
    api['atomic_json'](manifest_path, manifest)
    _write_bytes_atomic(registry_path, proposed, before_mode)
    actual_after = sha256_file(registry_path)
    if actual_after != manifest["registry_after_sha256"]:
        raise RuntimeError("SHA-256 do registry gravado diverge")
    _validate_registry_bytes(registry_path.read_bytes())
    manifest.update(status="completed_verified", completed_at=api["now"](),
                    registry_after_sha256=actual_after, applied_mode=before_mode)
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "variant_registry_applied", action="variants_apply",
                        status="completed_verified", source=str(stage_path), destination=str(registry_path),
                        sha256_before=expected_before or "", sha256_after=actual_after,
                        size_before=manifest["registry_before_size_bytes"], size_after=manifest["registry_after_size_bytes"],
                        details={"candidate_count": manifest["candidate_count"],
                                 "registry_record_count": manifest["registry_record_count_after"],
                                 "rom_paths_modified": 0})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["status"],
            "registry_path": str(registry_path), "registry_sha256": actual_after,
            "records": manifest["registry_record_count_after"], "manifest": str(manifest_path)}


def command_cancel(args, api: dict) -> dict:
    if not args.reason.strip():
        raise ValueError("Cancelar plano exige --reason")
    change_dir = Path(api["operation_dir"](args.operation_id))
    manifest_path = change_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "rom_variant_registry" or manifest.get("status") != "prepared":
        raise ValueError("Só um plano de registry preparado pode ser cancelado")
    stage_path = Path(manifest["staged_registry"])
    if not stage_path.is_relative_to(change_dir) or stage_path.is_symlink() or not stage_path.is_file():
        raise ValueError("Artefato proposto do plano ausente ou fora da operação")
    if sha256_file(stage_path) != manifest.get("registry_after_sha256"):
        raise ValueError("Artefato proposto mudou; cancelamento preserva o estado sem reescrevê-lo")
    preserved = change_dir / "rollback" / "registry-proposed-superseded.jsonl"
    if preserved.exists():
        raise FileExistsError(f"Preservação de plano cancelado já existe: {preserved}")
    os.replace(stage_path, preserved)
    manifest.update(status="superseded", completed_at=api["now"](), superseded_reason=args.reason,
                    superseded_registry_artifact=str(preserved))
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "variant_registry_plan_superseded", action="variants_cancel",
                        status="superseded", source=str(stage_path), destination=str(preserved),
                        sha256_before=manifest["registry_after_sha256"], sha256_after=manifest["registry_after_sha256"],
                        details={"reason": args.reason, "registry_path_modified": False})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": "superseded",
            "reason": args.reason, "manifest": str(manifest_path), "preserved_artifact": str(preserved)}


def command_verify(args, api: dict) -> dict:
    with media_session.write_lock(api):
        return _verify_registry(args, api)


def _verify_registry(args, api: dict) -> dict:
    project_root = Path(api["PROJECT_ROOT"])
    change_dir = Path(api["operation_dir"](args.operation_id))
    manifest_path = change_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get('kind') != 'rom_variant_registry' or
            manifest.get('status') not in {'completed_verified', 'rolled_back_verified'}):
        raise ValueError('Retome a aplicação ou reversão antes de verificar o registry')
    registry_path = Path(manifest["registry_path"])
    _safe_registry_path(registry_path, project_root)
    if manifest.get("status") == "rolled_back_verified" and not manifest.get("registry_before_exists"):
        if os.path.lexists(registry_path):
            raise ValueError("Registry deveria estar ausente depois do rollback")
        actual_registry_sha = None
        records = []
    else:
        if not registry_path.is_file() or registry_path.is_symlink():
            raise ValueError("Registry canônico ausente ou simbólico")
        data = registry_path.read_bytes()
        actual_registry_sha = hashlib.sha256(data).hexdigest()
        expected_registry_sha = (manifest.get("registry_after_sha256") if manifest.get("status") != "rolled_back_verified"
                                 else manifest.get("registry_before_sha256"))
        if actual_registry_sha != expected_registry_sha:
            raise ValueError("Hash do registry canônico diverge da operação")
        records = _validate_registry_bytes(data)
    changed = []
    rehashed = 0
    for item in manifest.get("items", []):
        path = project_root / item["path"]
        expected = {"device": item["device"], "inode": item["inode"], "size_bytes": item["size_bytes"],
                    "mtime_ns": item["mtime_ns"], "mode": item["mode"], "is_symlink": False, "is_regular": True}
        if not _snapshot_matches(path, expected):
            changed.append(item["path"])
            continue
        if args.rehash:
            actual = sha256_file(path)
            rehashed += int(item["size_bytes"])
            if actual != item["source_file_sha256"]:
                changed.append(item["path"])
    result = {"operation_id": args.operation_id, "status": "verified" if not changed else "failed",
              "registry_sha256": actual_registry_sha, "registry_records": len(records),
              "candidate_files_rehashed_bytes": rehashed, "changed_or_missing_paths": changed,
              "rom_paths_modified": 0}
    manifest.setdefault("verification_runs", []).append({**result, "at": api["now"](), "rehash": bool(args.rehash)})
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "variant_registry_verification", action="variants_verify",
                        status=result["status"], source=str(registry_path), sha256_before=actual_registry_sha,
                        details={key: value for key, value in result.items() if key != "operation_id"})
    api["rebuild_history_index"]()
    if changed:
        raise ValueError(f"Verificação encontrou caminhos alterados/ausentes: {len(changed)}; consulte o manifesto")
    return {**result, "manifest": str(manifest_path)}


def command_rollback(args, api: dict) -> dict:
    with media_session.write_lock(api):
        return _rollback_registry(args, api)


def _rollback_registry(args, api: dict) -> dict:
    if not args.rollback:
        raise ValueError("Restauração do registry exige --rollback explícito")
    project_root = Path(api["PROJECT_ROOT"])
    change_dir = Path(api["operation_dir"](args.operation_id))
    manifest_path = change_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("kind") != "rom_variant_registry" or
            manifest.get("status") not in {'completed_verified', 'applying', 'rolling_back'}):
        raise ValueError("Rollback permitido somente para registry aplicado e verificado")
    registry_path = Path(manifest["registry_path"])
    _safe_registry_path(registry_path, project_root)
    preserved = change_dir / 'rollback/registry-created-by-operation.jsonl'
    already_preserved = (manifest['status'] == 'rolling_back' and not manifest['registry_before_exists']
                         and not os.path.lexists(registry_path) and preserved.is_file() and not preserved.is_symlink())
    if (not already_preserved and not registry_path.is_file()) or registry_path.is_symlink():
        raise ValueError("Registry mudou; rollback não pode sobrescrever estado ausente/simbólico")
    current_sha = sha256_file(preserved if already_preserved else registry_path)
    allowed = {manifest['registry_after_sha256']}
    if (manifest['status'] == 'applying' and manifest.get('publication_intent') == manifest['registry_after_sha256']
            or manifest['status'] == 'rolling_back' and manifest.get('rollback_intent') == manifest['registry_before_sha256']):
        allowed.add(manifest.get('registry_before_sha256'))
    if current_sha not in allowed:
        raise ValueError("Registry foi alterado após a operação; rollback automático bloqueado")
    rollback_dir = change_dir / "rollback"
    if not already_preserved and stat.S_IMODE(registry_path.stat().st_mode) != int(manifest.get('applied_mode', 0o644)):
        raise ValueError('Permissões do registry mudaram; rollback recusado')
    if not manifest['registry_before_exists'] and os.path.lexists(preserved):
        if (manifest.get('preservation_intent') != manifest['registry_after_sha256'] or
                preserved.is_symlink() or not preserved.is_file() or sha256_file(preserved) != current_sha):
            raise ValueError('Preservação de rollback ocupada ou divergente')
    manifest['status'] = 'rolling_back'
    manifest['rollback_intent'] = manifest['registry_before_sha256']
    if not manifest['registry_before_exists']:
        manifest['preservation_intent'] = manifest['registry_after_sha256']
    api['atomic_json'](manifest_path, manifest)
    if manifest["registry_before_exists"]:
        backup = rollback_dir / "registry-before.jsonl"
        before = backup.read_bytes()
        if hashlib.sha256(before).hexdigest() != manifest["registry_before_sha256"]:
            raise ValueError("Backup de rollback diverge do hash prévio")
        _validate_registry_bytes(before)
        _write_bytes_atomic(registry_path, before, int(manifest.get("applied_mode", 0o644)))
        restored_sha = sha256_file(registry_path)
    else:
        if not already_preserved:
            # Keep an interrupted publication's sole created registry as recovery evidence.
            if not preserved.exists():
                from_registry = registry_path.read_bytes()
                _write_bytes_atomic(preserved, from_registry, int(manifest.get('applied_mode', 0o644)))
            if sha256_file(registry_path) != current_sha:
                raise ValueError('Registry mudou durante a preservação de rollback')
            registry_path.unlink()
        directory_fd = os.open(registry_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        restored_sha = None
    manifest.update(status="rolled_back_verified", rolled_back_at=api["now"](),
                    rollback_registry_sha256=restored_sha)
    api["atomic_json"](manifest_path, manifest)
    api["append_event"](args.operation_id, "variant_registry_rolled_back", action="variants_rollback",
                        status="rolled_back_verified", source=str(registry_path),
                        destination=str(rollback_dir), sha256_before=current_sha,
                        sha256_after=restored_sha or "",
                        details={"registry_before_exists": manifest["registry_before_exists"],
                                 "created_registry_preserved": not manifest["registry_before_exists"]})
    api["rebuild_history_index"]()
    return {"operation_id": args.operation_id, "status": manifest["status"],
            "registry_restored_sha256": restored_sha, "rollback_directory": str(rollback_dir),
            "manifest": str(manifest_path)}


def dispatch(args, api: dict) -> dict:
    command = args.variants_command
    if command == "inventory":
        return command_inventory(args, api)
    if command == "plan":
        return command_plan(args, api)
    if command == "apply":
        return command_apply(args, api)
    if command == "cancel":
        return command_cancel(args, api)
    if command == "verify":
        return command_verify(args, api)
    if command == "rollback":
        return command_rollback(args, api)
    raise ValueError(f"Comando variants desconhecido: {command}")
