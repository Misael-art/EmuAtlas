#!/usr/bin/env python3
"""Fetch public No-Intro DAT snapshots from DAT-o-MATIC and retain evidence.

DAT-o-MATIC exposes downloads through a session-backed form. This helper follows
that public flow, saves the original ZIP, extracts the single DAT, and writes a
manifest with URLs, timestamps, filenames, and SHA-256 values. It never touches
ROM or media files.
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.message
import hashlib
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request
import zipfile
from html import unescape
from pathlib import Path

BASE = "https://datomatic.no-intro.org/index.php"
USER_AGENT = "romtool-no-intro-dat-fetch/1.0 (local preservation workflow)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def selected_options(page: str, select_name: str) -> list[tuple[str, str]]:
    match = re.search(
        rf"<select\b[^>]*\bname=[\"']{re.escape(select_name)}[\"'][^>]*>(.*?)</select>",
        page, re.I | re.S,
    )
    if not match:
        raise RuntimeError(f"Form field not found: {select_name}")
    options = []
    for attrs, label in re.findall(r"<option\b([^>]*)>(.*?)</option>", match.group(1), re.I | re.S):
        value = re.search(r"\bvalue=[\"']([^\"']*)[\"']", attrs, re.I)
        if value and "selected" in attrs.lower():
            clean_label = unescape(re.sub(r"<[^>]+>", "", label)).strip()
            options.append((value.group(1), clean_label))
    return options


def submit(opener, url: str, values: dict[str, str], referer: str) -> tuple[bytes, str, dict[str, str]]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode(),
        headers={"User-Agent": USER_AGENT, "Referer": referer,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(request, timeout=90) as response:
        return response.read(), response.geturl(), dict(response.headers.items())


def content_filename(headers: dict[str, str]) -> str:
    message = email.message.Message()
    message["content-disposition"] = headers.get("Content-Disposition", "")
    filename = message.get_filename()
    if not filename:
        raise RuntimeError("Download response has no attachment filename")
    filename = Path(filename).name
    if not filename.lower().endswith(".zip"):
        raise RuntimeError(f"Expected a ZIP attachment, got {filename!r}")
    return filename


def download(system_id: str) -> tuple[str, str, bytes, bytes, dict]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    opener.addheaders = [("User-Agent", USER_AGENT)]
    url = f"{BASE}?page=download&op=dat&s={urllib.parse.quote(system_id)}"
    with opener.open(url, timeout=60) as response:
        page = response.read().decode("utf-8", "replace")
        page_url = response.geturl()

    systems = selected_options(page, "system_selection")
    if len(systems) != 1 or systems[0][0] != system_id:
        raise RuntimeError(f"DAT-o-MATIC selected an unexpected system: {systems!r}")
    ordering = selected_options(page, "sys_list_order")
    if len(ordering) != 1:
        raise RuntimeError("Could not determine selected system list order")
    buttons = re.findall(
        r"<input\b(?=[^>]*\bname=[\"'](download_dat_[^\"']+)[\"'])(?=[^>]*\bvalue=[\"']Prepare[\"'])[^>]*>",
        page, re.I,
    )
    if not buttons:
        raise RuntimeError(f"No published DAT download is available for system {system_id}")
    button_name = buttons[-1]
    prep_bytes, manager_url, prep_headers = submit(
        opener, page_url,
        {"system_selection": system_id, "sys_list_order": ordering[0][0],
         "region_filter": "all_regions", button_name: "Prepare"},
        page_url,
    )
    manager_page = prep_bytes.decode("utf-8", "replace")
    tokens = re.findall(
        r"<input\b(?=[^>]*\bname=[\"']([0-9a-fA-F]{32})[\"'])(?=[^>]*\bvalue=[\"']Download!![\"'])[^>]*>",
        manager_page, re.I,
    )
    if not tokens:
        raise RuntimeError(f"Download confirmation token not found for system {system_id}")
    token = tokens[-1]
    zip_bytes, final_url, download_headers = submit(opener, manager_url, {token: "Download!!"}, manager_url)
    prep_type = prep_headers.get("Content-Type", "")
    content_type = download_headers.get("Content-Type", "")
    if not zip_bytes.startswith(b"PK\x03\x04"):
        raise RuntimeError(f"Expected ZIP bytes, got {content_type!r} from {final_url}")
    with zipfile.ZipFile(__import__("io").BytesIO(zip_bytes)) as archive:
        dat_names = [item for item in archive.namelist()
                     if not item.endswith("/") and item.lower().endswith(".dat")]
        if len(dat_names) != 1:
            raise RuntimeError(f"Expected exactly one DAT, found {dat_names!r}")
        dat_name = Path(dat_names[0]).name
        dat_bytes = archive.read(dat_names[0])
        if not dat_bytes.lstrip().startswith(b"<"):
            raise RuntimeError(f"Extracted DAT is not XML: {dat_name}")
    metadata = {
        "system_id": system_id,
        "system_label": systems[0][1],
        "selected_dat_button": button_name,
        "system_page_url": page_url,
        "download_url": final_url,
        "content_type": content_type,
        "prep_content_type": prep_type,
        "downloaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "original_zip_filename": "",
        "original_zip_sha256": sha256(zip_bytes),
        "dat_filename": dat_name,
        "dat_sha256": sha256(dat_bytes),
        "dat_bytes": len(dat_bytes),
    }
    metadata["original_zip_filename"] = content_filename(download_headers)
    return metadata["original_zip_filename"], dat_name, zip_bytes, dat_bytes, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system-id", action="append", required=True,
                        help="DAT-o-MATIC system ID; may be repeated")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="New, empty directory for originals, extracted DATs, and manifest")
    args = parser.parse_args()
    if len(set(args.system_id)) != len(args.system_id):
        parser.error("system IDs must be unique")
    out_dir = args.out_dir
    if out_dir.exists() and any(out_dir.iterdir()):
        parser.error(f"output directory must be empty: {out_dir}")
    (out_dir / "original").mkdir(parents=True, exist_ok=True)
    (out_dir / "dat").mkdir(parents=True, exist_ok=True)
    rows = []
    manifest = {
        "schema_version": "1.0",
        "source": "DAT-o-MATIC public DAT download flow",
        "source_index_url": "https://datomatic.no-intro.org/?page=download&s=79",
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "status": "running",
        "systems": rows,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        for system_id in args.system_id:
            original_name, dat_name, zip_bytes, dat_bytes, metadata = download(system_id)
            original_rel = Path("original") / f"{system_id}-{original_name}"
            dat_rel = Path("dat") / f"{system_id}-{dat_name}"
            for rel, content in ((original_rel, zip_bytes), (dat_rel, dat_bytes)):
                target = out_dir / rel
                if target.exists():
                    raise FileExistsError(target)
                temp = target.with_name(target.name + ".tmp")
                temp.write_bytes(content)
                temp.replace(target)
            metadata["original_path"] = original_rel.as_posix()
            metadata["dat_path"] = dat_rel.as_posix()
            rows.append(metadata)
            manifest["systems"] = rows
            manifest["last_updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"system_id": system_id, "system_label": metadata["system_label"],
                              "dat": dat_name, "dat_sha256": metadata["dat_sha256"],
                              "original_zip": original_name, "zip_bytes": len(zip_bytes)},
                             ensure_ascii=False), flush=True)
        manifest["status"] = "completed"
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        manifest["systems"] = rows
        manifest["last_updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "systems_fetched": len(rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
