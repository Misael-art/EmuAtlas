"""Reviewable reconciliation of files that landed in roms/media instead of media/.

Identical bytes at the same relative path are renamed into the operation
rollback. Nothing is deleted, no symlink is left behind, and different or
unidentified files stay where they are. Retained rollback bytes are not
freed disk space.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_+-]{0,63}$")
QUARANTINE_ACTION = "quarantine_duplicate"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(path: Path) -> str:
    with path.open("rb") as stream:
        head = stream.read(16)
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "png" if head.startswith(b"\x89PNG\r\n\x1a\n") else "mismatch"
    if suffix in {".jpg", ".jpeg"}:
        return "jpeg" if head.startswith(b"\xff\xd8\xff") else "mismatch"
    if suffix == ".pdf":
        return "pdf" if head.startswith(b"%PDF-") else "mismatch"
    if suffix == ".mp4":
        return "mp4" if len(head) >= 8 and head[4:8] == b"ftyp" else "mismatch"
    return "unknown"


def _system_ids(project: Path) -> set[str]:
    catalog = project / "frontend/RetroFE/Config/platform-catalog.json"
    if not catalog.is_file() or catalog.is_symlink():
        return set()
    data = json.loads(catalog.read_text(encoding="utf-8"))
    return {str(row["id"]) for row in data.get("systems", []) if row.get("id")}


def _references(project: Path) -> list[str]:
    roots = [
        project / "frontend/RetroFE/settings.conf",
        project / "frontend/RetroFE/Config",
        project / "frontend/RetroFE/collections",
        project / "frontend/ES-DE/UserData/settings",
    ]
    hits = []
    for root in roots:
        candidates = [root] if root.is_file() else list(root.rglob("*")) if root.is_dir() else []
        for path in candidates:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            if path.suffix.lower() not in {".conf", ".xml", ".json", ".cfg", ".txt", ".md"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "roms/media" in text.replace("\\", "/"):
                hits.append(path.relative_to(project).as_posix())
    return sorted(set(hits))


VARIANT_DIR = re.compile(r"translation-[a-z]{2}(?:-[a-z]{2})?(?:-hack)?|hack|homebrew|unlicensed|bootleg|aftermarket|prototype|demo|beta|sample")


def _classify(project: Path, stray_root: Path, path: Path, systems: set[str], referenced: set[str],
              nested_variants: bool = False, nested_game_paths: bool = False) -> dict:
    relative = path.relative_to(stray_root).as_posix()
    project_relative = path.relative_to(project).as_posix()
    parts = Path(relative).parts
    variant_subdir = ""
    nested_parent = project / "roms" / parts[0] / Path(*parts[2:-1]) if len(parts) >= 4 else None
    verified_nested = False
    if nested_game_paths and nested_parent is not None and nested_parent.is_dir():
        verified_nested = not any(p.is_symlink() for p in [nested_parent, *nested_parent.parents] if p.is_relative_to(project))
    if ((nested_variants and len(parts) == 4 and VARIANT_DIR.fullmatch(parts[2])) or verified_nested) and not path.is_symlink():
        # ES-DE nested-game layout; canonical media is flat (AGENTS §Sessões gerenciadas).
        variant_subdir = "/".join(parts[2:-1])
        flat = "/".join((parts[0], parts[1], parts[-1]))
        row = _classify(project, stray_root, path, systems, referenced)  # bytes/signature once
        row = {**row, "relative_path": flat, "variant_subdir": variant_subdir, "nested_relative_path": relative}
        if row["action"] == "preserve_unidentified":
            row.update(system_id=parts[0], category=parts[1])
            if parts[0] not in systems or not SAFE_SEGMENT.fullmatch(parts[1]):
                return {**row, "reason": "plataforma ou categoria fora do catálogo conhecido"}
            if project_relative in referenced:
                return {**row, "action": "preserve_referenced", "reason": "há referência explícita a este caminho"}
            canonical = project / "media" / flat
            if canonical.is_symlink() or (canonical.exists() and not canonical.is_file()):
                return {**row, "action": "preserve_different", "reason": "destino canônico não é arquivo regular"}
            if not canonical.is_file():
                return {**row, "action": "preserve_absent", "reason": "não há o caminho achatado em media/"}
            canonical_sha = _sha256(canonical)
            row.update(canonical_relative=canonical.relative_to(project).as_posix(), canonical_sha256=canonical_sha)
            if canonical_sha == row["sha256"]:
                return {**row, "action": QUARANTINE_ACTION, "reason": "SHA-256 igual ao caminho canônico achatado"}
            return {**row, "action": "preserve_different", "reason": "o caminho achatado tem bytes diferentes; ambos permanecem"}
        return row
    if path.is_symlink():
        return {"relative_path": relative, "project_relative": project_relative, "size_bytes": 0,
                "signature": "symlink", "sha256": "", "status": "planned",
                "action": "preserve_symlink", "reason": "symlink não seguido"}
    base = {
        "relative_path": relative, "project_relative": project_relative,
        "size_bytes": path.stat().st_size, "signature": _signature(path),
        "sha256": _sha256(path), "status": "planned",
    }
    if len(parts) != 3 or any(part in {"", ".", ".."} for part in parts):
        return {**base, "action": "preserve_unidentified", "reason": "caminho relativo não é sistema/categoria/arquivo"}
    system_id, category, _name = parts
    base.update(system_id=system_id, category=category)
    if not SAFE_SEGMENT.fullmatch(system_id) or not SAFE_SEGMENT.fullmatch(category) or system_id not in systems:
        return {**base, "action": "preserve_unidentified", "reason": "plataforma ou categoria fora do catálogo conhecido"}
    if project_relative in referenced:
        return {**base, "action": "preserve_referenced", "reason": "há referência explícita a este caminho"}
    canonical = project / "media" / relative
    if canonical.is_symlink() or (canonical.exists() and not canonical.is_file()):
        return {**base, "action": "preserve_different", "reason": "destino canônico não é arquivo regular",
                "canonical_relative": canonical.relative_to(project).as_posix()}
    if not canonical.is_file():
        return {**base, "action": "preserve_absent", "reason": "não há o mesmo caminho relativo em media/"}
    canonical_sha = _sha256(canonical)
    same_inode = path.stat().st_dev == canonical.stat().st_dev and path.stat().st_ino == canonical.stat().st_ino
    row = {**base, "canonical_relative": canonical.relative_to(project).as_posix(), "canonical_sha256": canonical_sha}
    if same_inode:
        return {**row, "action": "preserve_hardlink", "reason": "o stray e o canônico são o mesmo inode; renomear moveria o payload ativo"}
    if canonical_sha == base["sha256"] and canonical.stat().st_size == base["size_bytes"]:
        return {**row, "action": QUARANTINE_ACTION, "reason": "SHA-256 e tamanho iguais no mesmo caminho relativo"}
    return {**row, "action": "preserve_different", "reason": "o mesmo caminho relativo tem bytes diferentes; ambos permanecem"}


# --- F09: reviewed promotion of absent/different strays into canonical media ---------------
PROMOTABLE_CATEGORIES = {"background", "booklet", "box", "clearlogo", "cover-back", "cover-front", "cover-full",
                         "cover-spine", "manual", "map", "marquee", "miximage", "physicalmedia", "screenshot",
                         "screentitle", "video"}
PROMOTABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".mp4", ".pdf"}
PROMOTE_ACTIONS = {"promote_to_canonical", "promote_as_alternate"}


def _rom_stems(project: Path, system_id: str) -> dict[str, list[str]]:
    root = project / "roms" / system_id
    stems: dict[str, list[str]] = {}
    if root.is_symlink() or not root.is_dir():
        return stems
    catalog = json.loads((project / "frontend/RetroFE/Config/platform-catalog.json").read_text())
    record = next((item for item in catalog.get("systems", []) if item.get("id") == system_id), {})
    extensions = {str(value).casefold().lstrip(".") for value in record.get("extensions", [])}
    for current, dirs, files in os.walk(root, followlinks=False):
        here = Path(current)
        dirs[:] = [name for name in dirs if not (here / name).is_symlink()]
        for name in files + dirs:
            path = here / name
            if not path.is_dir() and path.suffix.casefold().lstrip(".") not in extensions:
                continue
            stem = path.stem if path.is_file() or path.is_symlink() else path.name
            stems.setdefault(stem, []).append(path.relative_to(project).as_posix())
    return stems


def _alternate_target(canonical: Path, sha: str, reserved: set[str]) -> tuple[Path | None, Path | None]:
    """Return (free Alt path, identical existing file). Alt numbering starts at 2 (AGENTS §3)."""
    if canonical.is_file() and _sha256(canonical) == sha:
        return None, canonical
    index = 2
    while True:
        candidate = canonical.with_name(f"{canonical.stem} (Alt {index}){canonical.suffix}")
        if candidate.is_file() and not candidate.is_symlink():
            if _sha256(candidate) == sha:
                return None, candidate
        elif not os.path.lexists(candidate) and str(candidate) not in reserved:
            return candidate, None
        index += 1


def _promotion(project: Path, item: dict, api, stems_cache: dict, reserved: set[str],
               create_categories: set[str] | None = None, pdf_linearization_review: bool = False) -> dict:
    if item["action"] not in {"preserve_absent", "preserve_different"}:
        return item
    if item.get("category") not in PROMOTABLE_CATEGORIES:
        return {**item, "promotion_blocker": "categoria sem regra de promoção (ex.: story/patch exigem revisão própria)"}
    source = project / item["project_relative"]
    if source.suffix.lower() not in PROMOTABLE_EXTENSIONS:
        return {**item, "promotion_blocker": "extensão sem validação de decodificação configurada"}
    system_id = item["system_id"]
    stems = stems_cache.setdefault(system_id, _rom_stems(project, system_id))
    matches = stems.get(source.stem, [])
    if item.get("variant_subdir"):
        scope_parent = project / "roms" / system_id / item["variant_subdir"]
        scoped = [match for match in matches if (project / match).parent == scope_parent]
        if matches and not scoped:
            return {**item, "promotion_blocker": "a ROM com este basename não está na pasta de variante/escopo da mídia aninhada"}
        other_games = [match for match in matches if match not in scoped
                       and not ((project / match).is_dir() and scope_parent.is_relative_to(project / match))]
        if other_games:
            return {**item, "promotion_blocker": "basename associado a mais de uma ROM fora do escopo aninhado"}
        matches = scoped
    descriptor_evidence = None
    cues = [value for value in matches if Path(value).suffix.casefold() == ".cue"]
    if len(cues) == 1 and api.get("verify_cue"):
        cue = project / cues[0]
        verification = api["verify_cue"](cue)
        tracks = {(cue.parent / ref["name"]).resolve() for ref in verification.get("references", [])}
        if verification.get("status") == "valid" and all(value == cues[0] or (project / value).resolve() in tracks for value in matches):
            matches = cues
            descriptor_evidence = verification
    if len(matches) != 1:
        return {**item, "promotion_blocker": "sem ROM com basename exato" if not matches else "basename associado a mais de uma ROM"}
    if item.get("variant_subdir") and Path(matches[0]).parent.as_posix() != f"roms/{system_id}/{item['variant_subdir']}":
        return {**item, "promotion_blocker": "a ROM com este basename não está na pasta de variante da mídia aninhada"}
    try:
        validation = api["media_session"]._validate(source, api, pdf_linearization_review=pdf_linearization_review)
    except Exception as exc:  # noqa: BLE001 - recorded as evidence, the file stays in place
        return {**item, "promotion_blocker": f"validação integral falhou: {type(exc).__name__}: {str(exc)[:200]}"}
    canonical = project / "media" / item["relative_path"]
    category_relative = f"{system_id}/{item['category']}"
    current = project / "media"
    if current.is_symlink() or not current.is_dir():
        return {**item, "promotion_blocker": "raiz canônica ausente ou symlink"}
    for part in (system_id, item["category"]):
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            return {**item, "promotion_blocker": "raiz/categoria canônica atravessa symlink ou arquivo"}
    if not canonical.parent.is_dir() and category_relative not in (create_categories or set()):
        return {**item, "promotion_blocker": "categoria canônica ausente; criação de pasta exige revisão"}
    evidence = {"rom_association": matches[0], "validation": "full_decode_ok"}
    if source.suffix.casefold() == ".pdf":
        evidence["pdf_validation"] = validation
    if descriptor_evidence:
        evidence["disc_set_structure"] = descriptor_evidence
    if item["action"] == "preserve_absent":
        if os.path.lexists(canonical) or str(canonical) in reserved:
            return {**item, "promotion_blocker": "destino reservado/ocupado desde a classificação"}
        reserved.add(str(canonical))
        return {**item, **evidence, "action": "promote_to_canonical", "destination_relative": canonical.relative_to(project).as_posix(),
                "reason": "ausente no canônico; formato decodificado e basename único de ROM"}
    target, identical = _alternate_target(canonical, item["sha256"], reserved)
    if identical is not None:
        return {**item, **evidence, "action": QUARANTINE_ACTION, "canonical_relative": identical.relative_to(project).as_posix(),
                "canonical_sha256": item["sha256"], "reason": "bytes idênticos a uma alternativa canônica existente"}
    reserved.add(str(target))
    return {**item, **evidence, "action": "promote_as_alternate", "destination_relative": target.relative_to(project).as_posix(),
            "reason": "conteúdo diferente do canônico; preservado como alternativa numerada por hash"}


def _operation_id() -> str:
    return "media-stray-" + dt.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _save(api, manifest: dict) -> None:
    path = Path(api["operation_dir"](manifest["operation_id"])) / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    api["atomic_json"](path, manifest)


def _counts(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    bytes_by_action: dict[str, int] = {}
    for item in items:
        action = item.get("action", "")
        counts[action] = counts.get(action, 0) + 1
        bytes_by_action[action] = bytes_by_action.get(action, 0) + int(item.get("size_bytes") or 0)
    return {"actions": counts, "bytes_by_action": bytes_by_action, "items": len(items),
            "retained_rollback_is_not_freed_space": True}


def plan(api, promote: bool = False, system_id: str | None = None, nested_variants: bool = False,
         create_categories: list[str] | None = None, nested_game_paths: bool = False,
         pdf_linearization_review: bool = False) -> dict:
    project = Path(api["PROJECT_ROOT"])
    reviewed_categories = set(create_categories or [])
    known_systems = _system_ids(project)
    for category in reviewed_categories:
        parts = Path(category).parts
        if (not promote or len(parts) != 2 or Path(category).is_absolute()
                or parts[0] not in known_systems or parts[1] not in PROMOTABLE_CATEGORIES
                or (system_id and parts[0] != system_id)):
            raise ValueError(f"Categoria revisada inválida; use --promote --create-category SYSTEM/TYPE: {category}")
    stray_root = project / "roms" / "media"
    if stray_root.is_symlink():
        raise ValueError("roms/media é um symlink; preservado sem migração")
    items = []
    if stray_root.is_dir():
        systems = _system_ids(project)
        referenced = set()
        for ref in _references(project):
            text_path = project / ref
            try:
                text = text_path.read_text(encoding="utf-8", errors="ignore").replace("\\", "/")
            except OSError:
                continue
            for line in text.splitlines():
                if "roms/media/" in line:
                    start = line.find("roms/media/")
                    token = line[start:].split()[0].strip("\"'`,")
                    referenced.add(token)
        for path in sorted(stray_root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            if system_id and path.relative_to(stray_root).parts[0] != system_id:
                continue
            if path.is_symlink() or path.is_file():
                items.append(_classify(project, stray_root, path, systems, referenced, nested_variants, nested_game_paths))
        if promote:
            stems_cache: dict = {}
            reserved: set[str] = set()
            items = [_promotion(project, item, api, stems_cache, reserved, reviewed_categories, pdf_linearization_review) for item in items]
    operation_id = _operation_id()
    manifest = {
        "schema_version": 1, "operation_id": operation_id, "kind": "media_stray",
        "created_at": api["now"](), "status": "prepared", "read_only": True,
        "source_root": "roms/media", "canonical_root": "media", "promote": promote, "scope_system_id": system_id, "nested_variants": nested_variants,
        "reviewed_create_categories": sorted(reviewed_categories), "created_category_directories": [],
        "nested_game_paths": nested_game_paths,
        "tool_source_sha256": _sha256(Path(__file__)),
        "validator_source_sha256": _sha256(Path(api["media_session"].__file__)) if promote else None,
        "pdf_linearization_review": pdf_linearization_review,
        "permanent_deletion": False, "symlink_left_behind": False,
        "references": _references(project), "summary": _counts(items), "items": items,
        "limits": [
            "Decodificação integral de vídeo/PDF não é refeita aqui: só duplicatas de bytes idênticos saem da árvore stray.",
            "Arquivo diferente, ausente ou sem identidade permanece em roms/media.",
            "Originais retidos no rollback continuam ocupando disco.",
        ],
    }
    _save(api, manifest)
    api["append_event"](operation_id, "media_stray_planned", action="media_stray_plan", status="prepared",
                        details={"summary": manifest["summary"]})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "media_stray", "status": "prepared",
            "manifest": str(Path(api["operation_dir"](operation_id)) / "manifest.json"),
            "summary": manifest["summary"]}


def _load(api, operation_id: str) -> tuple[dict, Path]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", operation_id):
        raise ValueError("ID de operação inválido")
    path = Path(api["operation_dir"](operation_id)) / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "media_stray" or manifest.get("operation_id") != operation_id:
        raise ValueError("Operação não é media_stray")
    return manifest, path


def _snapshot_lists(api, operation: Path) -> None:
    output = Path(api["OUTPUT_ROOT"])
    rollback = operation / "rollback"
    rollback.mkdir(parents=True, exist_ok=True)
    for name in ("catalogo-arquivos.csv", "pendencias-integridade-dependencias.csv", "estado-final-verificacao.json"):
        source = output / name
        target = rollback / name
        if source.is_file() and not source.is_symlink() and not target.exists():
            os.link(source, target) if source.stat().st_dev == rollback.stat().st_dev else target.write_bytes(source.read_bytes())


def _update_lists(api, manifest: dict) -> None:
    if "OUTPUT_ROOT" not in api or "write_csv_atomic" not in api:
        return
    output = Path(api["OUTPUT_ROOT"])
    completed = {item["project_relative"]: item for item in manifest["items"]
                 if item["action"] in {"promoted", "quarantined"}}
    catalog_path = output / "catalogo-arquivos.csv"
    if completed and catalog_path.is_file():
        import csv
        with catalog_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = list(reader.fieldnames or []), list(reader)
        seen = set()
        project = Path(api["PROJECT_ROOT"])
        for row in rows:
            for relative, item in completed.items():
                source = str(project / relative)
                if (row.get("current_absolute_path") or row.get("absolute_path")) != source:
                    continue
                target = item.get("destination_relative") if item["action"] == "promoted" else item["rollback_relative"]
                row.update(current_absolute_path=str(project / target), current_sha256=item["sha256"],
                           current_size_bytes=item["size_bytes"], current_operation_id=manifest["operation_id"],
                           current_path_status="mídia canônica verificada" if item["action"] == "promoted" else "quarentena reversível")
                seen.add(relative)
        for relative, item in completed.items():
            if relative in seen or item["action"] != "promoted":
                continue
            source = project / relative
            target = project / item["destination_relative"]
            data = {"system_id": item["system_id"], "source": "roms-media-stray",
                    "source_root": str(project / "roms/media"), "relative_path": item["relative_path"],
                    "absolute_path": str(source), "file_name": source.name, "inventory_category": "media",
                    "function_hint": item["category"], "extension": source.suffix,
                    "size_bytes": item["size_bytes"], "sha256": item["sha256"],
                    "hash_status": "SHA-256 integral verificado", "hash_evidence": manifest["operation_id"],
                    "current_absolute_path": str(target), "current_sha256": item["sha256"],
                    "current_size_bytes": item["size_bytes"], "current_operation_id": manifest["operation_id"],
                    "current_path_status": "mídia canônica verificada"}
            rows.append({key: value for key, value in data.items() if key in fields})
        api["write_csv_atomic"](catalog_path, rows, fields)
    pending_path = output / "pendencias-integridade-dependencias.csv"
    if pending_path.is_file():
        import csv
        with pending_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
        for field in ("case_sha256", "case_source_relative"):
            if field not in fields:
                fields.append(field)
        existing = {(row.get("source"), row.get("case_source_relative"), row.get("case_sha256")) for row in rows}
        for row in rows:
            relative = row.get("case_source_relative") or row.get("path")
            item = completed.get(relative)
            if item is not None and row.get("source") == "roms-media-stray":
                target = item.get("destination_relative") if item["action"] == "promoted" else item["rollback_relative"]
                row.update(path=target, status="resolvido: " + item["action"],
                           next_step="Resultado verificado; rollback pelo manifesto " + manifest["operation_id"])
        for item in manifest["items"]:
            if item["action"] in {QUARANTINE_ACTION, "quarantined", "promoted"}:
                continue
            key = ("roms-media-stray", item["project_relative"], item["sha256"])
            if key in existing:
                continue
            rows.append({
                "type": "stray_media", "source": "roms-media-stray", "path": item["project_relative"],
                "status": item["action"], "dependency_or_impact": item.get("reason", ""),
                "next_step": "Preservar até haver identidade e destino canônico comprovados. Não escolher por tamanho ou resolução.",
                "case_sha256": item["sha256"], "case_source_relative": item["project_relative"],
            })
        api["write_csv_atomic"](pending_path, rows, fields)
    state_path = output / "estado-final-verificacao.json"
    if state_path.is_file() and not state_path.is_symlink():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["media_stray"] = {"as_of": api["now"](), "operation_id": manifest["operation_id"],
                                "status": manifest["status"], "summary": manifest["summary"],
                                "freed_bytes": 0, "reason": "duplicatas idênticas foram para o rollback e continuam no disco"}
        api["atomic_json"](state_path, state)


def apply(operation_id: str, confirmed: bool, api) -> dict:
    if not confirmed:
        raise ValueError("Aplicação exige --apply")
    manifest, _path = _load(api, operation_id)
    if manifest.get("tool_source_sha256") and manifest["tool_source_sha256"] != _sha256(Path(__file__)):
        raise ValueError("A ferramenta mudou desde o plano; prepare outro plano de mídia stray")
    if manifest.get("validator_source_sha256") and manifest["validator_source_sha256"] != _sha256(Path(api["media_session"].__file__)):
        raise ValueError("O validador mudou desde o plano; prepare outro plano de mídia stray")
    if manifest["status"] not in {"prepared", "applying"}:
        raise ValueError(f"Plano não aplicável: {manifest['status']}")
    project = Path(api["PROJECT_ROOT"])
    operation = Path(api["operation_dir"](operation_id))
    _snapshot_lists(api, operation)
    manifest["status"] = "applying"
    _save(api, manifest)
    rollback_root = operation / "rollback" / "originals"
    running = []
    for probe in ("retrofe_processes_running", "esde_processes_running"):
        if api.get(probe):
            running += list(api[probe]() or [])
    if running and any(item["action"] in PROMOTE_ACTIONS for item in manifest["items"]):
        raise RuntimeError("Feche RetroFE/ES-DE antes de promover mídia")
    for index, item in enumerate(manifest["items"]):
        if item["action"] in PROMOTE_ACTIONS:
            source = project / item["project_relative"]
            destination = project / item["destination_relative"]
            if os.path.lexists(destination):
                raise ValueError(f"Destino ocupado desde o plano; nada sobrescrito: {item['destination_relative']}")
            if not source.is_file() or source.is_symlink() or _sha256(source) != item["sha256"]:
                raise ValueError(f"Bytes mudaram desde o plano: {item['relative_path']}")
            current = project / "media"
            if current.is_symlink() or not current.is_dir():
                raise ValueError("A raiz canônica de mídia mudou")
            missing = []
            for part in (item["system_id"], item["category"]):
                current /= part
                if current.is_symlink() or (current.exists() and not current.is_dir()):
                    raise ValueError(f"Categoria atravessa symlink/arquivo: {current}")
                if not current.exists():
                    missing.append(current)
            if missing:
                category = f"{item['system_id']}/{item['category']}"
                if category not in manifest.get("reviewed_create_categories", []):
                    raise ValueError(f"Criação de categoria não revisada: {category}")
                for directory in missing:
                    directory.mkdir()
                    relative = directory.relative_to(project).as_posix()
                    manifest["created_category_directories"].append(relative)
                    _save(api, manifest)
                    api["append_event"](operation_id, "media_category_created", action="mkdir", status="created",
                                        details={"project_relative": relative})
            if source.stat().st_dev != destination.parent.stat().st_dev:
                raise OSError(f"Movimento cross-device bloqueado: {item['relative_path']}")
            os.rename(source, destination)
            item.update(action="promoted", promoted_as=item["action"], status="promoted")
            if index % 50 == 0:
                _save(api, manifest)
            continue
        if item["action"] != QUARANTINE_ACTION:
            continue
        source = project / item["project_relative"]
        canonical = project / item["canonical_relative"]
        destination = rollback_root / item["relative_path"]
        if destination.exists():
            raise ValueError(f"Rollback já contém {item['relative_path']}")
        if not source.is_file() or source.is_symlink() or not canonical.is_file() or canonical.is_symlink():
            raise ValueError(f"Precondição ausente: {item['relative_path']}")
        if _sha256(source) != item["sha256"] or _sha256(canonical) != item["canonical_sha256"]:
            raise ValueError(f"Bytes mudaram desde o plano: {item['relative_path']}")
        if source.stat().st_ino == canonical.stat().st_ino and source.stat().st_dev == canonical.stat().st_dev:
            raise ValueError(f"Hardlink detectado na aplicação: {item['relative_path']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)
        item.update(action="quarantined", status="quarantined",
                    rollback_relative=destination.relative_to(project).as_posix())
        if index % 100 == 0:
            _save(api, manifest)
    for directory in sorted((project / "roms" / "media").rglob("*"), reverse=True):
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    manifest.update(status="completed_verified", completed_at=api["now"](), summary=_counts(manifest["items"]),
                    symlink_left_behind=False, permanent_deletion=False)
    _save(api, manifest)
    _update_lists(api, manifest)
    api["append_event"](operation_id, "media_stray_applied", action="media_stray_apply", status=manifest["status"],
                        details={"summary": manifest["summary"]})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "media_stray", "status": manifest["status"],
            "manifest": str(operation / "manifest.json"), "summary": manifest["summary"]}


def verify(operation_id: str, api) -> dict:
    manifest, _path = _load(api, operation_id)
    project = Path(api["PROJECT_ROOT"])
    failed = []
    for item in manifest["items"]:
        source = project / item["project_relative"]
        if item["action"] == "quarantined":
            rollback = project / item["rollback_relative"]
            canonical = project / item["canonical_relative"]
            if source.exists() or source.is_symlink() or not rollback.is_file() or rollback.is_symlink():
                failed.append(item["relative_path"])
                continue
            if _sha256(rollback) != item["sha256"] or _sha256(canonical) != item["canonical_sha256"]:
                failed.append(item["relative_path"])
            continue
        if item["action"] == "promoted":
            destination = project / item["destination_relative"]
            if source.exists() or not destination.is_file() or destination.is_symlink() or _sha256(destination) != item["sha256"]:
                failed.append(item["relative_path"])
            continue
        if item["action"] == "preserve_symlink":
            if not source.is_symlink():
                failed.append(item["project_relative"])
            continue
        if not source.is_file() or source.is_symlink() or _sha256(source) != item["sha256"]:
            failed.append(item["project_relative"])
    status = "verified" if not failed else "verify_failed"
    return {"operation_id": operation_id, "kind": "media_stray", "status": status,
            "failed": failed[:20], "failed_count": len(failed), "summary": manifest["summary"]}


def rollback(operation_id: str, confirmed: bool, api) -> dict:
    if not confirmed:
        raise ValueError("Rollback exige --rollback")
    manifest, _path = _load(api, operation_id)
    if manifest["status"] != "completed_verified":
        raise ValueError("Rollback exige operação verificada")
    project = Path(api["PROJECT_ROOT"])
    for item in reversed(manifest["items"]):
        if item["action"] == "promoted":
            source, destination = project / item["destination_relative"], project / item["project_relative"]
            if os.path.lexists(destination) or not source.is_file() or source.is_symlink() or _sha256(source) != item["sha256"]:
                raise ValueError(f"Estado mudou desde a promoção; rollback recusado: {item['relative_path']}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, destination)
            item.update(action=item["promoted_as"], status="rolled_back")
            continue
        if item["action"] != "quarantined":
            continue
        source = project / item["rollback_relative"]
        destination = project / item["project_relative"]
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"Destino stray reapareceu; rollback recusado: {item['project_relative']}")
        if not source.is_file() or source.is_symlink() or _sha256(source) != item["sha256"]:
            raise ValueError(f"Pré-imagem divergente: {item['relative_path']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)
        item.update(action=QUARANTINE_ACTION, status="rolled_back")
    for relative in reversed(manifest.get("created_category_directories", [])):
        directory = project / relative
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()
    output = Path(api.get("OUTPUT_ROOT", ""))
    operation = Path(api["operation_dir"](operation_id))
    for name in ("catalogo-arquivos.csv", "pendencias-integridade-dependencias.csv", "estado-final-verificacao.json"):
        preimage = operation / "rollback" / name
        if preimage.is_file() and (output / name).is_file():
            os.replace(preimage, output / name)
    manifest.update(status="rolled_back", summary=_counts(manifest["items"]))
    _save(api, manifest)
    api["append_event"](operation_id, "media_stray_rolled_back", action="media_stray_rollback", status="rolled_back",
                        details={"permanent_deletion": False})
    api["rebuild_history_index"]()
    return {"operation_id": operation_id, "kind": "media_stray", "status": "rolled_back", "summary": manifest["summary"]}


def dispatch(args, api) -> dict:
    command = args.media_stray_command
    if command == "plan":
        return plan(api, getattr(args, "promote", False), getattr(args, "system_id", None),
                    getattr(args, "nested_variants", False), getattr(args, "create_category", None),
                    getattr(args, "nested_game_paths", False), getattr(args, "pdf_linearization_review", False))
    if command == "apply":
        return apply(args.operation_id, args.apply, api)
    if command == "verify":
        return verify(args.operation_id, api)
    if command == "rollback":
        return rollback(args.operation_id, args.rollback, api)
    raise ValueError(f"Comando stray desconhecido: {command}")
