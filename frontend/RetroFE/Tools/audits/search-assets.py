#!/usr/bin/env python3
"""Discover workspace-shared assets and game media without moving or reclassifying files."""

from __future__ import annotations

import argparse
import base64
from collections import Counter
import csv
import hashlib
import html
import io
import json
import os
import stat
import sys
import warnings
import zlib
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "Manifests" / "asset-search-index.csv"
WORKSPACE = ROOT.parents[1]
MEDIA = WORKSPACE / "media"
PLATFORMS = ROOT / "Config" / "platform-catalog.json"
MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def project_file(row: dict) -> Path:
    """Resolve a record lexically; never inspect through symlink ancestors."""
    literal = Path(row["project_path"])
    if not literal.parts or literal.is_absolute() or ".." in literal.parts:
        raise ValueError("Caminho do índice fora do projeto")
    path = WORKSPACE / literal
    for parent in path.parents:
        if parent == WORKSPACE:
            break
        if parent.is_symlink():
            raise ValueError("Ancestral symlink; inspeção recusada")
    return path


def file_metadata(row: dict) -> dict:
    result = dict(row)
    result.update(path_status="unavailable", file_kind="unknown", size_bytes="")
    try:
        path = project_file(row)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            result.update(file_kind="symlink", alias_target=os.readlink(path), path_status="symlink")
            target = path.resolve(strict=False)
            if not target.is_relative_to(WORKSPACE.resolve()):
                result["path_status"] = "external_alias"
            elif not target.exists():
                result["path_status"] = "broken_alias"
        elif stat.S_ISREG(info.st_mode):
            result.update(path_status="present", file_kind="regular", size_bytes=str(info.st_size))
        else:
            result.update(path_status="special_file", file_kind="special")
    except FileNotFoundError:
        result["path_status"] = "missing"
    except (OSError, ValueError, RuntimeError) as error:
        result.update(path_status="unavailable", path_error=str(error))
    return result


def media_rows() -> list[dict[str, str]]:
    """Read live paths, with the official taxonomy; names do not prove identity."""
    if str(WORKSPACE) not in sys.path:
        sys.path.insert(0, str(WORKSPACE))
    from tools.roms import romtool

    if MEDIA.is_symlink() or not MEDIA.is_dir():
        raise ValueError(f"Raiz de mídia ausente ou symlink: {MEDIA}")
    systems = {item["id"] for item in json.loads(PLATFORMS.read_text(encoding="utf-8"))["systems"]
               if item.get("kind") == "platform"}
    aliases = romtool.ESDE_MEDIA_TYPE_MAP
    result = []

    def fail(error):
        raise error

    for current, directories, names in os.walk(MEDIA, followlinks=False, onerror=fail):
        base = Path(current)
        for name in sorted(list(directories)):
            if (base / name).is_symlink():
                directories.remove(name)
                names.append(name)
        for name in sorted(names):
            path = base / name
            parts = path.relative_to(MEDIA).parts
            system_id = parts[0] if len(parts) >= 2 else ""
            category = parts[1] if len(parts) >= 2 else ""
            canonical_category = aliases.get(category, category)
            status = "path_only"
            if system_id == "pending":
                status = "pending_staging"
            elif system_id not in systems:
                status = "unknown_system"
            elif canonical_category not in romtool.MEDIA_TYPES:
                status = "unknown_category"
            if name in {".nomedia", ".directory"}:
                status = "protected_marker"
            extension = path.suffix.casefold()
            kind = "unknown"
            if extension in romtool.media_optimizer.IMAGE_EXTS | {".jfif", ".svg"}:
                kind = "images"
            elif extension in romtool.media_optimizer.VIDEO_EXTS:
                kind = "videos"
            elif extension in romtool.media_optimizer.AUDIO_EXTS:
                kind = "audio"
            elif extension == ".pdf":
                kind = "documents"
            elif extension == ".txt":
                kind = "text"
            result.append(file_metadata({
                "library": "media", "scope": "game-media", "system_id": system_id,
                "media_type": kind, "media_type_evidence": "extension_hint_only",
                "category": category, "canonical_category_hint": canonical_category, "style": "",
                "display_name": path.stem, "logical_name": "", "canonical_path": path.relative_to(MEDIA).as_posix(),
                "project_path": path.relative_to(WORKSPACE).as_posix(), "sha256": "", "hash_bucket": "",
                "source_types": category, "source_names": name, "source_paths": "", "aliases": "",
                "classification_status": status, "classification_evidence": "path_only_not_content_or_game_identity",
            }))
    return result


def read_theme_rows(manifest: Path | None = None) -> list[dict[str, str]]:
    if manifest is not None:
        manifest_path = manifest
        if not manifest_path.is_absolute() and not manifest_path.is_file():
            manifest_path = ROOT / manifest_path
        plan = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload = plan.get("search_index_after_zlib_base64")
        if not payload:
            raise ValueError(f"Manifesto não contém índice de busca previsto: {manifest_path}")
        data = zlib.decompress(base64.b64decode(payload)).decode("utf-8")
        return list(csv.DictReader(io.StringIO(data)))
    if not INDEX.is_file():
        raise FileNotFoundError(f"Índice ainda não existe: {INDEX}; gere/aplique o plano do materializador de assets.")
    with INDEX.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def read_rows(manifest: Path | None = None, library: str = "assets") -> list[dict[str, str]]:
    if manifest is not None and library != "assets":
        raise ValueError("--manifest consulta somente assets planejados; use --library assets")
    rows = []
    if library in {"assets", "all"}:
        for source in read_theme_rows(manifest):
            row = dict(source)
            row.update(library="assets", system_id="",
                       project_path=(Path("frontend/RetroFE") / row["canonical_path"]).as_posix(),
                       classification_evidence="catalog_record_not_visual_acceptance",
                       media_type_evidence="catalog_record", hash_evidence="recorded_not_rehashed")
            rows.append(row)
    if library in {"media", "all"}:
        rows.extend(media_rows())
    return rows


def search(rows: list[dict[str, str]], args: argparse.Namespace) -> list[dict[str, str]]:
    query = (args.query or "").casefold().split()
    result = []
    for row in rows:
        if args.media_type and row.get("media_type", "").casefold() != args.media_type.casefold():
            continue
        if args.category and args.category.casefold() not in row.get("category", "").casefold():
            continue
        if args.style and args.style.casefold() not in row.get("style", "").casefold():
            continue
        if args.scope and row.get("scope", "").casefold() != args.scope.casefold():
            continue
        if getattr(args, "system_id", None) and row.get("system_id") != args.system_id:
            continue
        if getattr(args, "classification_status", None) and row.get("classification_status") != args.classification_status:
            continue
        if getattr(args, "path_status", None) and row.get("path_status") != args.path_status:
            continue
        haystack = " ".join(row.values()).casefold()
        if query and not all(term in haystack for term in query):
            continue
        result.append(row)
    return sorted(result, key=lambda row: (row.get("media_type", ""), row.get("category", ""), row.get("style", ""), row.get("display_name", "").casefold(), row.get("canonical_path", "").casefold()))


def inspect_image(row: dict) -> dict:
    result = file_metadata(row)
    if result["path_status"] != "present" or result.get("media_type") != "images":
        result["inspection_status"] = "not_inspected"
        return result
    if int(result["size_bytes"]) > MAX_IMAGE_BYTES:
        result["inspection_status"] = "preserved_size_limit"
        return result
    try:
        from PIL import Image
    except ImportError as error:
        raise ValueError("--inspect-images exige Pillow; a consulta comum dispensa essa dependência") from error
    path = project_file(result)
    before = path.stat()
    try:
        with warnings.catch_warnings(), path.open("rb") as stream:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            digest_state = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest_state.update(block)
            stream.seek(0)
            with Image.open(stream) as picture:
                width, height = picture.size
                orientation = picture.getexif().get(274, 1)
                if orientation in {5, 6, 7, 8}:
                    width, height = height, width
                result.update(width=width, height=height, image_mode=picture.mode,
                              image_format=picture.format, frame_count=getattr(picture, "n_frames", 1),
                              alpha_channel_or_transparency="A" in picture.getbands() or "transparency" in picture.info,
                              orientation="landscape" if width > height else "portrait" if height > width else "square")
                if width * height > MAX_IMAGE_PIXELS:
                    result["inspection_status"] = "preserved_pixel_limit"
                    return result
                if result["frame_count"] != 1:
                    result["inspection_status"] = "preserved_multiframe"
                    return result
                picture.load()
            digest = digest_state.hexdigest()
        after = path.stat()
        snapshot = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if snapshot(before) != snapshot(after):
            result["inspection_status"] = "changed_during_inspection"
            return result
        result.update(inspection_status="decoded_current_file", current_sha256=digest,
                      hash_evidence="recorded_and_current_compared" if result.get("sha256") else "current_file_rehashed",
                      recorded_hash_status="matches" if result.get("sha256") == digest else
                      "differs" if result.get("sha256") else "not_recorded")
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        result.update(inspection_status="unsupported_or_decode_failed", inspection_error=str(error))
    return result


def summarize(rows: list[dict]) -> dict:
    facets = {field: dict(sorted(Counter(str(row.get(field) or "[unknown]") for row in rows).items()))
              for field in ("library", "scope", "media_type", "category", "style", "classification_status", "path_status")}
    return {"records": len(rows), "facets": facets,
            "regular_files": sum(row.get("file_kind") == "regular" for row in rows),
            "logical_regular_bytes": sum(int(row.get("size_bytes") or 0) for row in rows if row.get("file_kind") == "regular"),
            "physical_bytes_freed": 0, "payloads_changed": 0,
            "notes": ["Consulta derivada dos índices oficiais e caminhos atuais; não cria catálogo.",
                      "Aliases não são percorridos nem contados como outro payload.",
                      "Categoria/estilo do catálogo não comprovam aceite visual; media usa apenas pistas do caminho/extensão.",
                      "Tamanho lógico não mede extents exclusivos, duplicidade, integridade ou economia física."]}


def print_gallery(rows: list[dict], directory: Path) -> None:
    """Emit an offline review view referencing canonical files, never thumbnails."""
    directory = directory.resolve()
    if not directory.is_relative_to(WORKSPACE.resolve()):
        raise ValueError("--html-base deve ficar dentro do projeto")
    escape = html.escape
    print('<!doctype html><html lang="pt-BR"><meta charset="utf-8">')
    print('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; style-src \'unsafe-inline\'">')
    print('<title>Revisão de assets e mídia</title><style>body{font:16px system-ui;background:#15191e;color:#edf0f2;margin:32px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px}article{background:#242b33;border-radius:10px;padding:16px;overflow-wrap:anywhere}img{width:100%;height:200px;object-fit:contain;background:#101318}a{color:#9ed8ff}small{color:#c5cbd2}</style>')
    print('<h1>Revisão de assets e mídia</h1><p>Referências aos arquivos canônicos, sem cópias. A seleção exige revisão de função, identidade, composição e direitos; esta galeria não aprova nem altera classificações.</p><main>')
    for source in rows:
        row = file_metadata(source)
        preview = "<p>Prévia indisponível para este formato ou caminho.</p>"
        if row["path_status"] == "present":
            path = project_file(row)
            url = escape(quote(Path(os.path.relpath(path, directory)).as_posix(), safe="/"), quote=True)
            if row.get("media_type") == "images" and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".avif", ".gif", ".bmp"}:
                preview = f'<img loading="lazy" src="{url}" alt="{escape(row.get("display_name", ""), quote=True)}">'
            preview += f'<p><a href="{url}">Abrir arquivo canônico</a></p>'
        print(f'<article><h2>{escape(row.get("display_name", ""))}</h2>{preview}<p>{escape(row.get("library", ""))} · {escape(row.get("category", ""))} · {escape(row.get("style", "") or "estilo não revisado")}</p><p>{escape(row["project_path"])}</p><small>{escape(row.get("classification_evidence", ""))} · {escape(row["path_status"])}</small></article>')
    print('</main></html>')


def print_table(rows: list[dict[str, str]], limit: int) -> None:
    fields = ("library", "media_type", "category", "style", "display_name", "project_path")
    if any("inspection_status" in row for row in rows[:limit]):
        fields = ("library", "category", "display_name", "width", "height", "inspection_status", "project_path")
    widths = {field: min(max([len(field), *(len(str(row.get(field, ""))) for row in rows[:limit])]), 52) for field in fields}
    print("  ".join(field.upper().ljust(widths[field]) for field in fields))
    print("  ".join("─" * widths[field] for field in fields))
    for row in rows[:limit]:
        print("  ".join(str(row.get(field, ""))[:widths[field]].ljust(widths[field]) for field in fields))
    if len(rows) > limit:
        print(f"… {len(rows) - limit} outros resultados; aumente --limit.")
    print(f"{len(rows)} resultado(s). JSON/CSV mostram evidência, origem e aliases; a consulta não reclassifica arquivos.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="termos em nome, origem, caminho, categoria ou estilo")
    parser.add_argument("--library", choices=("assets", "media", "all"), default="assets",
                        help="índice de assets (padrão), mídia atual de jogos ou consulta conjunta")
    parser.add_argument("--media-type", choices=("fonts", "sounds", "music", "videos", "images", "audio", "documents", "text", "unknown"))
    parser.add_argument("--category", help="filtra a categoria semântica, inclusive subcategorias")
    parser.add_argument("--style", help="filtra o estilo visual, como clean, color ou black-white")
    parser.add_argument("--scope", choices=("runtime", "source-artwork", "theme-setup", "game-media"))
    parser.add_argument("--system-id", help="ID exato de plataforma; não infere plataforma de um asset de tema")
    parser.add_argument("--classification-status", help="estado literal do catálogo ou triagem de caminhos")
    parser.add_argument("--path-status", help="estado atual: present, missing, symlink, broken_alias etc.")
    parser.add_argument("--check-paths", action="store_true", help="confere presença/tamanho sem ler payloads")
    parser.add_argument("--summary", action="store_true", help="contagens e bytes lógicos de todos os resultados filtrados")
    parser.add_argument("--inspect-images", action="store_true", help="decodifica somente as imagens da seleção limitada; sem avaliar estética")
    parser.add_argument("--manifest", type=Path, help="consulta o índice previsto por um manifesto sem aplicá-lo")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--format", choices=("table", "json", "csv", "html"), default="table")
    parser.add_argument("--html-base", type=Path, help="pasta onde será salvo o HTML emitido em stdout, dentro do projeto")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit deve ser maior que zero")
    if args.summary and (args.format not in {"table", "json"} or args.inspect_images):
        parser.error("--summary aceita table/json e não decodifica imagens")
    if args.format == "html" and args.html_base is None:
        parser.error("--format html exige --html-base para calcular referências relativas")
    try:
        rows = read_rows(args.manifest, args.library)
        if args.check_paths or args.summary or args.path_status:
            rows = [file_metadata(row) for row in rows]
        rows = search(rows, args)
        if args.summary:
            print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
            return 0
        selected = rows[:args.limit]
        if args.inspect_images:
            selected = [inspect_image(row) for row in selected]
        if args.format == "json":
            print(json.dumps(selected, ensure_ascii=False, indent=2))
        elif args.format == "csv":
            fields = list(dict.fromkeys(key for row in selected for key in row))
            writer = csv.DictWriter(sys.stdout, fieldnames=fields)
            if selected:
                writer.writeheader()
                writer.writerows(selected)
        elif args.format == "html":
            print_gallery(selected, args.html_base)
        else:
            print_table(selected + rows[len(selected):] if args.inspect_images else rows, args.limit)
    except (OSError, ValueError, csv.Error, zlib.error) as error:
        print(error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
