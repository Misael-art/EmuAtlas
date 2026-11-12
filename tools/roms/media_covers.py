"""Cover layout diagnosis and reviewed, reversible separation. Entry point: romtool.

Geometry proposes review, never proves a cover's semantic role or crop boundary.
Full originals retain their exact bytes; derived fronts retain exact decoded pixels.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import statistics
import uuid
import warnings

TYPES = {"cover", "cover-front", "cover-back", "cover-spine", "cover-full"}
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MAX_PIXELS = 32_000_000
SOURCE = Path(__file__).resolve()


def _media(api, relative):
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 3 or p.parts[1] not in TYPES:
        raise ValueError("Caminho precisa ser <plataforma>/<tipo de capa>/<arquivo>")
    result = api["MEDIA_DEST_ROOT"] / p
    api["reject_symlink_components"](result.parent)
    return result


def _id(api, prefix):
    return prefix + api["dt"].datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]


def _save(api, manifest, event):
    api["atomic_json"](api["operation_dir"](manifest["operation_id"]) / "manifest.json", manifest)
    api["append_event"](manifest["operation_id"], event, action="media_covers", status=manifest["status"])
    api["rebuild_history_index"]()


@contextmanager
def _pixels(path):
    from PIL import Image, ImageOps
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise ValueError("Imagem excede limite de 32 megapixels; preservada para revisão")
            if getattr(probe, "n_frames", 1) != 1:
                raise ValueError("Imagem animada/multipágina preservada; não separar uma única frame")
            if probe.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
                raise ValueError("Profundidade/espaço de cor exige revisão específica")
            with path.open("rb") as f:
                header = f.read(32)
            if header.startswith(b"\x89PNG\r\n\x1a\n") and header[24] > 8:
                raise ValueError("PNG com mais de 8 bits preservado")
            fmt = probe.format
            probe.verify()
        with Image.open(path) as source:
            source.load()
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert("RGBA")
            try:
                yield rgba, dict(format=fmt, stored_dimensions=list(source.size), dimensions=list(rgba.size),
                                 exif_orientation=source.getexif().get(274, 1),
                                 icc_profile=source.info.get("icc_profile"))
            finally:
                rgba.close()
                if oriented is not source:
                    oriented.close()


def _pixel_hash(image):
    h = hashlib.sha256()
    h.update(f"RGBA:{image.width}:{image.height}:".encode())
    # Bounded stripes avoid another full image-sized byte buffer.
    for y in range(0, image.height, 128):
        h.update(image.crop((0, y, image.width, min(y + 128, image.height))).tobytes())
    return h.hexdigest()


def _content_bbox(image):
    """Describe padding only; never use these bounds as an approved crop."""
    from PIL import Image, ImageChops
    alpha=image.getchannel("A").getbbox()
    if alpha and alpha != (0,0,image.width,image.height):return list(alpha)
    rgb=image.convert("RGB")
    corners=[rgb.getpixel(p) for p in [(0,0),(rgb.width-1,0),(0,rgb.height-1),(rgb.width-1,rgb.height-1)]]
    if all(max(abs(a-b) for a,b in zip(corners[0],color))<=6 for color in corners):
        background=Image.new("RGB",rgb.size,corners[0])
        difference=ImageChops.difference(rgb,background).convert("L").point(lambda v:255 if v>8 else 0)
        bbox=difference.getbbox()
        if bbox and (bbox[2]-bbox[0])*(bbox[3]-bbox[1])>=image.width*image.height*.2:return list(bbox)
    return [0,0,image.width,image.height]


def _front(path, box):
    from PIL import __version__
    if not isinstance(box, list) or len(box) != 4 or any(type(v) is not int for v in box):
        raise ValueError("front_box exige quatro coordenadas inteiras, sem escala automática")
    with _pixels(path) as (image, metadata):
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height):
            raise ValueError("Retângulo fora da imagem orientada")
        fraction = (x1-x0)*(y1-y0)/(image.width*image.height)
        if not .20 <= fraction <= .80:
            raise ValueError("Recorte de frente precisa de revisão: área fora de 20–80% do encarte")
        cropped = image.crop(tuple(box))
        try:
            buffer = io.BytesIO()
            cropped.save(buffer, format="PNG", compress_level=9, icc_profile=metadata.get("icc_profile"))
            data = buffer.getvalue()
            return data, dict(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
                              pixel_sha256=_pixel_hash(cropped), dimensions=list(cropped.size),
                              pillow_version=__version__, front_box=box)
        finally:
            cropped.close()


def _review_file(api, raw):
    path = Path(raw)
    if not path.is_absolute():
        path = api["PROJECT_ROOT"] / path
    path = Path(os.path.abspath(path))
    api["reject_symlink_components"](path)
    if not path.is_relative_to(api["PROJECT_ROOT"]):
        raise ValueError("Revisão deve estar dentro da estrutura oficial")
    value = json.loads(path.read_text())
    entries = value.get("items", [])
    if not entries or len({i["source"] for i in entries}) != len(entries):
        raise ValueError("Revisão vazia ou com caminhos repetidos")
    return path, entries


def review(args,api):
    """Record semantic observations by content hash; no new registry or payload change."""
    audit_path=api["operation_dir"](args.audit_operation_id)/"manifest.json"
    audit_manifest=json.loads(audit_path.read_text())
    if audit_manifest.get("kind") != "media_cover_audit":raise ValueError("Auditoria de capas exigida")
    known={r["source"]:r for r in audit_manifest["items"]}
    path,entries=_review_file(api,args.reviews_json)
    for entry in entries:
        source=_media(api,entry["source"])
        if entry.get("classification") not in {"front-cover","full-cover","not-cover","uncertain"} or len(entry.get("evidence",""))<20:
            raise ValueError("Classificação/evidência visual insuficiente")
        _check_file(api,source,entry["sha256"])
        if known.get(entry["source"],{}).get("sha256") != entry["sha256"]:
            raise ValueError("Revisão diverge da auditoria")
    opid=_id(api,"media-cover-review-")
    manifest=dict(schema_version="1.0",operation_id=opid,kind="media_cover_review",created_at=api["now"](),
                  status="completed_verified",read_only=True,audit_operation_id=args.audit_operation_id,
                  reviews=str(path.relative_to(api["PROJECT_ROOT"])),reviews_sha256=api["sha256_file"](path),items=entries)
    _save(api,manifest,"cover_semantics_reviewed")
    return dict(operation_id=opid,status=manifest["status"],reviewed=len(entries))


def _known_reviews(api):
    path=api["OPS_ROOT"]/"history-index.csv"
    if not path.is_file():return {}
    with path.open(encoding="utf-8-sig",newline="") as f:history=list(csv.DictReader(f))
    result={}
    for row in sorted(history,key=lambda r:r.get("created_at","")):
        if row.get("kind") != "media_cover_review" or row.get("status") != "completed_verified":continue
        opid=row.get("operation_id","")
        if not re.fullmatch(r"[A-Za-z0-9._-]+",opid):continue
        manifest=json.loads((api["operation_dir"](opid)/"manifest.json").read_text())
        for item in manifest.get("items",[]):
            if item.get("classification") in {"front-cover","full-cover","not-cover","uncertain"} and item.get("sha256"):
                result[(Path(item["source"]).parts[0],item["sha256"])]=dict(item,review_operation_id=opid)
    return result


def audit(args, api):
    rows, systems = [], set(args.system_id or [])
    for system in sorted(api["MEDIA_DEST_ROOT"].iterdir()):
        if not system.is_dir() or system.is_symlink() or systems and system.name not in systems:
            continue
        for kind in sorted(TYPES - {"cover-spine"}):
            folder = system / kind
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if path.suffix.lower() not in EXTENSIONS or not path.is_file() and not path.is_symlink():
                    continue
                rel = path.relative_to(api["MEDIA_DEST_ROOT"]).as_posix()
                row = dict(source=rel, system_id=system.name, media_type=kind, status="preserved",
                           classification="unconfirmed", reason="", needs_review=False)
                if path.is_symlink():
                    target = path.resolve()
                    row.update(status="alias", reason="Alias relativo; payload auditado uma única vez",
                               target=str(target.relative_to(api["PROJECT_ROOT"])) if target.is_relative_to(api["PROJECT_ROOT"]) else "outside_project")
                else:
                    try:
                        before = api["path_snapshot"](path)
                        row.update(sha256=before["sha256"],size_bytes=before["size_bytes"],precondition=before)
                        with _pixels(path) as (image, metadata):
                            # Transparent margins must not disguise a landscape payload in a square canvas.
                            bbox = _content_bbox(image)
                            row.update({k:v for k,v in metadata.items() if k != "icc_profile"})
                            row.update(content_bbox=list(bbox), ratio=round((bbox[2]-bbox[0])/(bbox[3]-bbox[1]), 5))
                        after = api["path_snapshot"](path, include_hash=False)
                        if any(before[k] != after[k] for k in ("size_bytes", "mtime_ns", "inode", "device")):
                            raise ValueError("Arquivo mudou durante diagnóstico")
                        row.update(sha256=before["sha256"], size_bytes=before["size_bytes"], precondition=before)
                        row.update(classification="single_panel_compatible", reason="Geometria compatível; orientação não comprova função")
                        if kind == "cover-full":
                            row.update(classification="stored_full_cover", reason="Categoria de encarte completo; consultar sua evidência oficial")
                    except Exception as exc:
                        row.update(status="blocked_preserved", needs_review=True, reason=str(exc))
                rows.append(row)
    if systems and systems - {r["system_id"] for r in rows}:
        raise ValueError("Plataforma solicitada não contém capas auditáveis")
    ratios = defaultdict(list)
    for row in rows:
        if row["media_type"] == "cover-front" and "ratio" in row:
            ratios[row["system_id"]].append(row["ratio"])
    baselines = {system:statistics.median(values) for system,values in ratios.items() if len(values) >= 8}
    for row in rows:
        if "ratio" not in row or row["media_type"] not in {"cover", "cover-front"}:
            continue
        median = baselines.get(row["system_id"])
        factor = row["ratio"] / median if median else None
        row["platform_median_front_ratio"] = median
        if factor and 1.75 <= factor <= 2.80:
            row.update(classification="full_cover_candidate", needs_review=True,
                       reason="Proporção compatível com dois painéis/lombada diante da mediana local; exige confirmação visual")
        elif row["media_type"] == "cover":
            row.update(classification="legacy_cover_needs_review", needs_review=True,
                       reason="cover legado pode ser frente, flyer, arte ou encarte; geometria não distingue conteúdo")
        elif row["ratio"] >= 1.1 and not (median and .75 <= factor <= 1.25):
            row.update(classification="landscape_front_needs_review", needs_review=True,
                       reason="Frente horizontal pode ser legítima; não mover nem impor orientação vertical")
    reviewed=_known_reviews(api)
    for row in rows:
        decision=reviewed.get((row["system_id"],row.get("sha256")))
        if not decision or row["status"] == "blocked_preserved":continue
        label={"front-cover":"confirmed_front_cover","full-cover":"confirmed_full_cover", "not-cover":"confirmed_other_art","uncertain":"unconfirmed"}[decision["classification"]]
        row.update(classification=label,needs_review=decision["classification"] not in {"front-cover"} and row["media_type"]!="cover-full",
                   reason=decision["evidence"],review_operation_id=decision["review_operation_id"])
    opid = _id(api, "media-cover-audit-")
    opdir = api["operation_dir"](opid)
    opdir.mkdir(parents=True)
    manifest = dict(schema_version="1.0", operation_id=opid, kind="media_cover_audit", created_at=api["now"](),
                    status="completed_verified", read_only=True, items=rows,
                    summary=dict(files=sum(r["status"] != "alias" for r in rows), aliases=sum(r["status"] == "alias" for r in rows),
                                 needs_review=sum(r["needs_review"] for r in rows),
                                 classifications=dict(Counter(r["classification"] for r in rows))),
                    limits=["Geometria é triagem, não prova semântica", "Frentes horizontais/quadradas são preservadas", "Nenhum recorte ou movimento automático"])
    fields = ["source", "system_id", "media_type", "classification", "status", "needs_review", "ratio", "platform_median_front_ratio", "sha256", "size_bytes", "reason"]
    api["write_csv_atomic"](opdir / "cover-audit.csv", rows, fields)
    _save(api, manifest, "cover_layout_audited")
    _pending(api, manifest)
    return {k:v for k,v in manifest.items() if k != "items"}


def plan(args, api):
    audit_dir = api["operation_dir"](args.audit_operation_id)
    audit_manifest = json.loads((audit_dir / "manifest.json").read_text())
    if audit_manifest.get("kind") != "media_cover_audit":
        raise ValueError("Auditoria de capas não comprovada")
    review_path, reviews = _review_file(api, args.reviews_json)
    known = {r["source"]:r for r in audit_manifest["items"]}
    opid = _id(api, "media-cover-plan-")
    items, fulls, fronts = [], {}, {}
    for review in reviews:
        rel = review["source"]
        source = _media(api, rel)
        if Path(rel).parts[1] not in {"cover", "cover-front"}:
            raise ValueError("Separar somente encarte completo confirmado em cover/cover-front")
        if len(review.get("evidence", "")) < 20:
            raise ValueError("Evidência visual precisa explicar frente, verso e limite do recorte")
        row = known.get(rel, {})
        api["reject_symlink_components"](source)
        before = api["path_snapshot"](source)
        if before["sha256"] != review.get("sha256") or row.get("sha256") != before["sha256"]:
            raise ValueError("Revisão, auditoria e bytes atuais não coincidem")
        if review.get("classification") == "front-cover":continue
        if review.get("classification") != "full-cover":raise ValueError("Classificação não autoriza separar")
        data, front = _front(source, review.get("front_box"))
        system, _, name = Path(rel).parts
        full_rel = f"{system}/cover-full/{name}"
        front_rel = f"{system}/cover-front/{Path(name).stem}.png"
        for target in (full_rel, front_rel):
            dest = _media(api, target)
            if os.path.lexists(dest) and dest != source:
                raise FileExistsError(f"Destino ocupado; preservar e revisar: {dest}")
        full_payload = fulls.setdefault((system, before["sha256"]), full_rel)
        front_payload = fronts.setdefault((system, front["sha256"]), front_rel)
        itemid = f"{opid}-{len(items)+1:04d}"
        items.append(dict(item_id=itemid, status="prepared", source=rel, system_id=system,
                          source_sha256=before["sha256"], source_size_bytes=before["size_bytes"], precondition=before,
                          full=full_rel, full_payload=full_payload, front=front_rel, front_payload=front_payload,
                          original_storage=full_rel if full_rel == full_payload else f"@rollback/{itemid}/{name}",
                          front_proof=front, review=review, events=[],
                          reason="Encarte completo confirmado visualmente; preservar original e disponibilizar somente a frente"))
    # Logical targets must be unique, even when distinct source extensions share a basename.
    if not items:raise ValueError("Nenhum encarte completo revisado para separar; use review para classificar sem mover")
    for key in ("full", "front"):
        if len({i[key] for i in items}) != len(items):
            raise ValueError("Dois itens disputam o mesmo nome de saída")
    manifest = dict(schema_version="1.0", operation_id=opid, kind="media_cover_plan", status="prepared",
                    created_at=api["now"](), title="Separar frentes de encartes completos visualmente confirmados",
                    tool_source=str(SOURCE.relative_to(api["PROJECT_ROOT"])), tool_source_sha256=api["sha256_file"](SOURCE),
                    audit_operation_id=args.audit_operation_id, audit_sha256=api["sha256_file"](audit_dir/"manifest.json"),
                    reviews=str(review_path.relative_to(api["PROJECT_ROOT"])), reviews_sha256=api["sha256_file"](review_path),
                    items=items, policy=dict(permanent_delete=False, automatic_crop=False, original_bytes_preserved=True),
                    rollback="Restaurar em ordem inversa, preservando frentes geradas no rollback; hashes, aliases e frontends são rechecados")
    _save(api, manifest, "reviewed_cover_plan_prepared")
    return dict(operation_id=opid, status="prepared", item_count=len(items), manifest=str(api["operation_dir"](opid)/"manifest.json"))


def _storage(api, opdir, item):
    value = item["original_storage"]
    if value.startswith("@rollback/"):
        path = opdir / "rollback/originals" / value.removeprefix("@rollback/")
        if not path.is_relative_to(opdir / "rollback/originals") or ".." in path.parts:
            raise ValueError("Backup fora da operação")
        api["reject_symlink_components"](path)
        return path
    return _media(api, value)


def _load(api, opid):
    folder = api["operation_dir"](opid)
    api["reject_symlink_components"](folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("kind") != "media_cover_plan" or manifest.get("operation_id") != opid:
        raise ValueError("Não é plano de capas")
    for item in manifest["items"]:
        if not re.fullmatch(re.escape(opid)+r"-\d{4}",item["item_id"]):
            raise ValueError("ID de item inválido")
        source=_media(api,item["source"])
        system,kind,name=Path(item["source"]).parts
        if kind not in {"cover","cover-front"} or system != item["system_id"]:
            raise ValueError("Origem não corresponde à plataforma e função revisadas")
        expected_full=f"{system}/cover-full/{name}"
        expected_front=f"{system}/cover-front/{Path(name).stem}.png"
        if item["full"] != expected_full or item["front"] != expected_front:
            raise ValueError("Destino lógico diverge da origem revisada")
        for key,category in [("full_payload","cover-full"),("front_payload","cover-front")]:
            path=_media(api,item[key])
            if Path(item[key]).parts[:2] != (system,category):
                raise ValueError("Payload compartilhado fora da plataforma/função")
        allowed_storage={item["full"],f"@rollback/{item['item_id']}/{name}"}
        if item["original_storage"] not in allowed_storage:
            raise ValueError("Destino do original diverge da operação")
        paths={str(_media(api,item[k]).relative_to(api["PROJECT_ROOT"])) for k in ["source","front","full"]}
        for event in item.get("events",[]):
            if event["kind"] == "create_alias":
                if event["path"] not in paths or Path(event["target"]).is_absolute():
                    raise ValueError("Alias do histórico fora dos destinos permitidos")
                p=api["PROJECT_ROOT"]/event["path"]
                target=Path(os.path.abspath(p.parent/event["target"]))
                allowed={_media(api,item[k]) for k in ["front","front_payload","full_payload"]}
                if target not in allowed:raise ValueError("Alvo de alias fora do plano")
            elif event["kind"] == "publish_front":
                if event["path"] != str(_media(api,item["front_payload"]).relative_to(api["PROJECT_ROOT"])):
                    raise ValueError("Publicação fora do plano")
            elif event["kind"] == "move_original":
                if event["source"] != item["source"] or event["destination"] != str(_storage(api,folder,item).relative_to(api["PROJECT_ROOT"])):
                    raise ValueError("Movimento fora do plano")
            else:raise ValueError("Evento de mutação desconhecido")
    return folder, manifest


def _binding(api, manifest):
    if api["sha256_file"](SOURCE) != manifest["tool_source_sha256"]:
        approved=False
        index=api["OPS_ROOT"]/"history-index.csv"
        if index.is_file():
            with index.open(encoding="utf-8-sig",newline="") as f:history=list(csv.DictReader(f))
            for row in history:
                if row.get("kind")!="media_cover_attestation" or row.get("status")!="completed_verified":continue
                opid=row.get("operation_id","")
                if not re.fullmatch(r"[A-Za-z0-9._-]+",opid):continue
                record=json.loads((api["operation_dir"](opid)/"manifest.json").read_text())
                if record.get("plan_operation_id")==manifest["operation_id"] and record.get("plan_contract_sha256")==_contract_hash(manifest) and record.get("tool_source_sha256")==api["sha256_file"](SOURCE):
                    approved=True;break
        if not approved:raise ValueError("Ferramenta mudou; revisar e executar covers attest ou criar plano atual")
    active = api["frontend_processes_running"]()
    if any(active.values()):
        raise RuntimeError("Feche RetroFE e ES-DE normalmente antes de separar/reverter capas")


def _contract_hash(manifest):
    keys=("item_id","source","system_id","source_sha256","source_size_bytes","precondition","full","full_payload","front","front_payload","original_storage","front_proof","review")
    contract=dict(operation_id=manifest["operation_id"],tool_source_sha256=manifest["tool_source_sha256"],
                  items=[{k:i[k] for k in keys} for i in manifest["items"]])
    return hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()


def attest(args,api):
    """Bind updated code only after reproducing every crop from unchanged originals."""
    if len(args.reason)<20:raise ValueError("Explicar a mudança de código e a evidência de compatibilidade")
    opdir,manifest=_load(api,args.operation_id)
    if manifest["status"]=="rolled_back":raise ValueError("Plano revertido; não atestar para reaplicação")
    if any(api["frontend_processes_running"]().values()):raise RuntimeError("Feche os frontends antes de atestar")
    checked=[]
    for item in manifest["items"]:
        storage=_storage(api,opdir,item)
        source=storage if storage.is_file() else _media(api,item["source"])
        _check_file(api,source,item["source_sha256"])
        _,proof=_front(source,item["front_proof"]["front_box"])
        if proof!=item["front_proof"]:raise ValueError("Código/encoder atual não reproduz o contrato da frente")
        if item["status"]=="verified":_verify_item(api,opdir,item)
        checked.append(dict(item_id=item["item_id"],source_sha256=item["source_sha256"],front_sha256=proof["sha256"],pixel_sha256=proof["pixel_sha256"]))
    opid=_id(api,"media-cover-attestation-")
    record=dict(schema_version="1.0",operation_id=opid,kind="media_cover_attestation",created_at=api["now"](),status="completed_verified",
                plan_operation_id=args.operation_id,plan_contract_sha256=_contract_hash(manifest),tool_source_sha256=api["sha256_file"](SOURCE),
                reason=args.reason,items=checked,read_only=True,historical_manifest_unchanged=True)
    _save(api,record,"updated_cover_code_revalidated")
    return dict(operation_id=opid,status=record["status"],verified=len(checked),historical_manifest_unchanged=True)


def _check_file(api, path, sha):
    api["reject_symlink_components"](path)
    if not path.is_file() or api["sha256_file"](path) != sha:
        raise ValueError(f"Payload ausente ou alterado: {path}")


def _event(api, manifest, item, event):
    # Persist intent before each mutation; rollback can recognize completed steps after interruption.
    item["events"].append(event)
    api["atomic_json"](api["operation_dir"](manifest["operation_id"])/"manifest.json", manifest)
    api["append_event"](manifest["operation_id"], event["kind"], action="media_covers", status=item["status"], item_id=item["item_id"], **event)


def _alias(api, manifest, item, source, target):
    if os.path.lexists(source):
        raise FileExistsError(source)
    source.parent.mkdir(parents=True, exist_ok=True)
    _event(api, manifest, item, dict(kind="create_alias", path=str(source.relative_to(api["PROJECT_ROOT"])), target=os.path.relpath(target, source.parent)))
    api["relative_media_symlink"](source, target)


def _verify_item(api, opdir, item):
    full = _media(api, item["full_payload"])
    front = _media(api, item["front_payload"])
    _check_file(api, full, item["source_sha256"])
    _check_file(api, _storage(api, opdir, item), item["source_sha256"])
    _check_file(api, front, item["front_proof"]["sha256"])
    with _pixels(front) as (image, _):
        if _pixel_hash(image) != item["front_proof"]["pixel_sha256"]:
            raise ValueError("Pixels da frente não coincidem com a região revisada")
    for key, payload in (("front",front), ("full",full)):
        logical = _media(api,item[key])
        if logical != payload and not api["media_link_matches"](logical,payload):
            raise ValueError("Alias de capa alterado")
    source = _media(api,item["source"])
    logical_front = _media(api,item["front"])
    if source != logical_front and not api["media_link_matches"](source,logical_front):
        raise ValueError("Referência anterior não encontra a frente correta")


def apply(args, api):
    if not args.apply:
        raise ValueError("Aplicação exige --apply e um --item-id")
    opdir, manifest = _load(api,args.operation_id)
    _binding(api,manifest)
    item = next((i for i in manifest["items"] if i["item_id"] == args.item_id),None)
    if not item or item["status"] != "prepared":
        raise ValueError("Item não está preparado; não reaplicar um resultado/falha")
    source = _media(api,item["source"])
    _check_file(api,source,item["source_sha256"])
    full, front = _media(api,item["full"]), _media(api,item["front"])
    full_payload, front_payload = _media(api,item["full_payload"]), _media(api,item["front_payload"])
    storage = _storage(api,opdir,item)
    for p in {full,front,storage}:
        if os.path.lexists(p) and p != source:
            raise FileExistsError(p)
    for p,sha in ((full_payload,item["source_sha256"]),(front_payload,item["front_proof"]["sha256"])):
        if p not in {full,front}:
            _check_file(api,p,sha)
    required = MAX_PIXELS*4 + 64*1024*1024
    if shutil.disk_usage(api["MEDIA_DEST_ROOT"]).free < required:
        raise RuntimeError("Reserva de espaço insuficiente para recorte validado")
    data, proof = _front(source,item["front_proof"]["front_box"])
    if proof != item["front_proof"]:
        raise ValueError("Conversor ou pixels mudaram desde o plano")
    staged = opdir / "staging" / (item["item_id"]+".png")
    staged.parent.mkdir(parents=True,exist_ok=True)
    api["reject_symlink_components"](staged)
    if staged.exists():
        # Only our candidate is discarded; the exact source has just been verified.
        api["append_event"](manifest["operation_id"],"interrupted_cover_candidate_rebuilt",item_id=item["item_id"],status="source_preserved")
        staged.unlink()
    with staged.open("xb") as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    del data
    _check_file(api,staged,proof["sha256"])
    item["status"] = "applying"
    manifest["status"] = "applying"
    try:
        _check_file(api,source,item["source_sha256"])
        storage.parent.mkdir(parents=True,exist_ok=True)
        if source.stat().st_dev != storage.parent.stat().st_dev:
            raise ValueError("Separação exige rename do original no mesmo filesystem")
        _event(api,manifest,item,dict(kind="move_original",source=item["source"],destination=str(storage.relative_to(api["PROJECT_ROOT"]))))
        if os.path.lexists(storage):
            raise FileExistsError(storage)
        source.rename(storage)
        front_payload.parent.mkdir(parents=True,exist_ok=True)
        if front_payload == front:
            _event(api,manifest,item,dict(kind="publish_front",path=str(front_payload.relative_to(api["PROJECT_ROOT"]))))
            os.link(staged,front_payload)  # exclusive publication; no overwrite
        else:
            _alias(api,manifest,item,front,front_payload)
        if full != full_payload:
            _alias(api,manifest,item,full,full_payload)
        if source != front:
            _alias(api,manifest,item,source,front)
        _verify_item(api,opdir,item)
        staged.unlink()  # own temporary, now preserved in the verified front payload
        item["status"] = "verified"
        item["verified_at"] = api["now"]()
        manifest["status"] = "completed_verified" if all(i["status"] == "verified" for i in manifest["items"]) else "applied_partial"
        _save(api,manifest,"cover_front_verified")
        _catalog(api,manifest)
        return dict(operation_id=args.operation_id,item_id=args.item_id,status=item["status"],physical_bytes_released=0)
    except Exception as exc:
        item["status"] = "failed_preserved";item["error"] = str(exc);manifest["status"] = "failed_preserved"
        _save(api,manifest,"cover_split_failed_preserved")
        raise


def verify(args,api):
    opdir,manifest = _load(api,args.operation_id)
    verified,failed = 0,[]
    for item in manifest["items"]:
        if item["status"] == "verified":
            try:
                _verify_item(api,opdir,item);verified+=1
            except Exception as exc:
                failed.append(dict(item_id=item["item_id"],error=str(exc)))
    report=dict(operation_id=_id(api,"media-cover-verification-"),kind="media_cover_verification",
                created_at=api["now"](),status="completed_verified" if not failed else "verification_failed",
                parent_operation=args.operation_id,verified=verified,failed=failed,items=[],read_only=True)
    _save(api,report,"cover_verification_finished")
    if failed:
        raise ValueError("Verificação de capa falhou; consultar "+report["operation_id"])
    return dict(operation_id=args.operation_id,status="verified",verified=verified,failed=[],verification_operation_id=report["operation_id"])


def rollback(args,api):
    if not args.rollback:
        raise ValueError("Rollback exige --rollback")
    opdir,manifest = _load(api,args.operation_id)
    _binding(api,manifest)
    todo = [i for i in manifest["items"] if i["status"] in {"verified","applying","failed_preserved"}]
    # Complete preflight, then reverse in dependency order. No partial rollback on known divergence.
    for item in todo:
        storage = _storage(api,opdir,item)
        source = _media(api,item["source"])
        restored = not storage.exists() and source.is_file() and not source.is_symlink() and api["sha256_file"](source)==item["source_sha256"]
        if restored:
            continue
        moved = any(e["kind"] == "move_original" for e in item["events"])
        if moved and storage.exists():
            _check_file(api,storage,item["source_sha256"])
            if os.path.lexists(source):
                front=_media(api,item["front"])
                if source != front and not api["media_link_matches"](source,front):
                    raise ValueError("Caminho original ocupado; rollback recusado antes de alterar arquivos")
        for event in item["events"]:
            if event["kind"] == "create_alias":
                path = api["PROJECT_ROOT"]/event["path"]
                if os.path.lexists(path) and (not path.is_symlink() or os.readlink(path) != event["target"]):
                    raise ValueError("Alias alterado após aplicação; rollback recusado")
            if event["kind"] == "publish_front":
                path = api["PROJECT_ROOT"]/event["path"]
                if os.path.lexists(path):_check_file(api,path,item["front_proof"]["sha256"])
    for item in reversed(todo):
        storage,source = _storage(api,opdir,item),_media(api,item["source"])
        restored = not storage.exists() and source.is_file() and not source.is_symlink() and api["sha256_file"](source)==item["source_sha256"]
        if restored:
            item["status"]="rolled_back"
            _save(api,manifest,"interrupted_cover_rollback_reconciled")
            continue
        for event in reversed(item["events"]):
            if event["kind"] == "create_alias":
                path = api["PROJECT_ROOT"]/event["path"]
                if path.is_symlink():path.unlink()
            elif event["kind"] == "publish_front":
                path = api["PROJECT_ROOT"]/event["path"]
                if path.exists():
                    retained = opdir/"rollback/applied"/(item["item_id"]+".png")
                    retained.parent.mkdir(parents=True,exist_ok=True)
                    if os.path.lexists(retained):raise FileExistsError(retained)
                    path.rename(retained)
            elif event["kind"] == "move_original":
                storage,source = _storage(api,opdir,item),_media(api,item["source"])
                if storage.exists():
                    if os.path.lexists(source):raise FileExistsError(source)
                    storage.rename(source)
        _check_file(api,_media(api,item["source"]),item["source_sha256"])
        staged=opdir/"staging"/(item["item_id"]+".png")
        api["reject_symlink_components"](staged)
        if staged.is_file():
            staged.unlink()  # own temporary; original already restored and verified
        item["status"] = "rolled_back"
        _save(api,manifest,"cover_item_rolled_back")
    manifest["status"] = "rolled_back"
    _save(api,manifest,"cover_plan_rolled_back")
    _catalog(api,manifest,restored=True)
    return dict(operation_id=args.operation_id,status="rolled_back",restored=len(todo))


def _pending(api,manifest):
    path = api["OUTPUT_ROOT"] / "pendencias-integridade-dependencias.csv"
    if not path.is_file():return
    with path.open(encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
    for item in manifest["items"]:
        key="media/"+item["source"]
        row=next((r for r in rows if r.get("source")=="media-cover-layout" and r.get("path")==key),None)
        if not item["needs_review"]:
            if row and item["classification"]=="confirmed_front_cover":
                row.update(status="resolvido; frente legítima confirmada visualmente",next_step="Preservar orientação e bytes; revisão "+item.get("review_operation_id",""))
            continue
        data=dict(type="media_semantics",source="media-cover-layout",path=key,status="revisão semântica; arquivo preservado",
                  dependency_or_impact=item["reason"],next_step="Confirmar função e limites visualmente; romtool media covers plan com revisão SHA; não mover pela orientação")
        if row:row.update(data)
        else:rows.append(data)
    api["write_csv_atomic"](path,rows,fields)
    state_path=api["OUTPUT_ROOT"]/"estado-final-verificacao.json"
    if state_path.is_file():
        state=json.loads(state_path.read_text())
        state["global_pending_rows"]=len(rows)
        state["media_cover_audit"]=dict(as_of=api["now"](),operation_id=manifest["operation_id"],summary=manifest["summary"])
        api["atomic_json"](state_path,state)


def _catalog(api,manifest,restored=False):
    path=api["OUTPUT_ROOT"]/"catalogo-arquivos.csv"
    if not path.is_file():return
    with path.open(encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f);fields=list(reader.fieldnames);rows=list(reader)
    for name in ("current_sha256","current_size_bytes","current_operation_id"):
        if name not in fields:fields.append(name)
    opdir=api["operation_dir"](manifest["operation_id"])
    for item in manifest["items"]:
        if item["status"] not in {"verified","rolled_back"}:continue
        source=_media(api,item["source"])
        front=_media(api,item["front_payload"])
        for row in rows:
            current=row.get("current_absolute_path") or row.get("absolute_path")
            affected={str(source),str(front)}
            if restored:affected.add(str(_media(api,item["full_payload"])))
            if current in affected:
                target=source if restored else front
                row.update(current_absolute_path=str(target),current_sha256=api["sha256_file"](target),current_size_bytes=target.stat().st_size,
                           current_operation_id=manifest["operation_id"],current_path_status="original restaurado" if restored else "frente separada; pixels conferidos",
                           integrity_status="Original completo preservado; recorte validado por SHA-256 RGBA; não prova identidade No-Intro")
        targets=[source] if restored else [_media(api,item["full_payload"]),front]
        for target in targets:
            if any((r.get("current_absolute_path") or r.get("absolute_path"))==str(target) for r in rows):continue
            digest=api["sha256_file"](target)
            data=dict(system_id=item["system_id"],source="media_cover_split",source_root=str(api["MEDIA_DEST_ROOT"]),
                      relative_path=target.relative_to(api["MEDIA_DEST_ROOT"]).as_posix(),absolute_path=str(target),file_name=target.name,
                      inventory_category="game_media",function_hint="mídia externa: "+target.parent.name,extension=target.suffix,
                      size_bytes=target.stat().st_size,sha256=digest,hash_status="SHA-256 integral verificado",hash_evidence=manifest["operation_id"],
                      current_absolute_path=str(target),current_sha256=digest,current_size_bytes=target.stat().st_size,
                      current_operation_id=manifest["operation_id"],current_path_status="canônico verificado")
            rows.append({k:v for k,v in data.items() if k in fields})
    api["write_csv_atomic"](path,rows,fields)
    pending=api["OUTPUT_ROOT"]/"pendencias-integridade-dependencias.csv"
    if pending.is_file():
        with pending.open(encoding="utf-8-sig",newline="") as f:
            reader=csv.DictReader(f);pending_fields=reader.fieldnames;pending_rows=list(reader)
        fixed={"media/"+i["source"] for i in manifest["items"] if i["status"]=="verified"}
        reverted={"media/"+i["source"] for i in manifest["items"] if i["status"]=="rolled_back"}
        for row in pending_rows:
            if row.get("source")=="media-cover-layout" and row.get("path") in fixed:
                row.update(status="resolvido; frente e encarte separados com validação",next_step="Consultar "+manifest["operation_id"]+"; original protegido e rollback disponível")
            elif row.get("source")=="media-cover-layout" and row.get("path") in reverted:
                row.update(status="reaberto após rollback; encarte restaurado ao local anterior",next_step="Revisar novamente; bytes originais restaurados e frentes geradas preservadas no rollback")
        api["write_csv_atomic"](pending,pending_rows,pending_fields)
    state_path=api["OUTPUT_ROOT"]/"estado-final-verificacao.json"
    if state_path.is_file():
        state=json.loads(state_path.read_text());state["current_catalog_rows"]=len(rows)
        state["media_cover_layout"]=dict(as_of=api["now"](),operation_id=manifest["operation_id"],status=manifest["status"],
                                        corrected=sum(i["status"]=="verified" for i in manifest["items"]),physical_bytes_released=0)
        api["atomic_json"](state_path,state)


def dispatch(args,api):
    return {"audit":audit,"review":review,"plan":plan,"attest":attest,"apply":apply,"verify":verify,"rollback":rollback}[args.media_covers_command](args,api)
