"""Explicit, audited Linux extent deduplication; never removes or rewrites file data."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import signal
import threading
import sys
import uuid

# linux/fs.h: _IOWR(0x94, 54, struct file_dedupe_range), sizeof(header)=24.
FIDEDUPERANGE = 0xC0189436
CHUNK = 16 * 1024 * 1024
ROOTS = {"roms", "media", "frontend", "tools", "work", "outputs", "metadata", "quarantine", "mods"}


def add_parser(commands):
    p = commands.add_parser("storage", help="deduplicação física CoW, sem exclusão ou alteração de conteúdo")
    actions = p.add_subparsers(dest="storage_action", required=True)
    plan = actions.add_parser("plan")
    plan.add_argument("--groups", required=True, help="JSON revisado: groups=[{sha256, paths}]")
    for action in ("apply", "resume", "verify"):
        p = actions.add_parser(action)
        p.add_argument("--operation-id", required=True)
        if action in {"apply", "resume"}:
            p.add_argument("--apply", action="store_true")
            p.add_argument("--max-items", type=int, default=2000)
            p.add_argument("--max-bytes", type=int, default=4 * 1024**3)


def safe_path(root, value):
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.parts[0] not in ROOTS:
        raise ValueError(f"Caminho fora do projeto/escopo: {value}")
    if any(x.casefold() in {"bios", "saves", "userdata", "keys", ".git"} for x in rel.parts):
        raise ValueError(f"Dados protegidos fora do escopo: {value}")
    q = root
    for part in rel.parts:
        q /= part
        if q.is_symlink():
            raise ValueError(f"Symlink não permitido: {q}")
    if not q.is_file():
        raise ValueError(f"Arquivo regular ausente: {q}")
    return q


def facts(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "size_bytes": st.st_size,
            "mtime_ns": st.st_mtime_ns, "mode": st.st_mode}


def digest_fd(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    while data := os.read(fd, 1024 * 1024):
        h.update(data)
    return h.hexdigest()


def share_fds(src, dst, length):
    """Kernel compares every byte before sharing; later writes remain private."""
    offset = 0
    while offset < length:
        size = min(CHUNK, length - offset)
        arg = bytearray(struct.pack("=QQHHIqQQiI", offset, size, 1, 0, 0, dst, offset, 0, 0, 0))
        fcntl.ioctl(src, FIDEDUPERANGE, arg, True)
        _, _, _, _, _, _, _, count, status, _ = struct.unpack("=QQHHIqQQiI", arg)
        if status or count <= 0 or count > size:
            raise OSError(f"Deduplicação recusada: status={status}, bytes={count}, offset={offset}")
        offset += count
    return offset


def assert_idle(api):
    if any(api["frontend_processes_running"]().values()):
        raise ValueError("Feche os frontends normalmente antes de aplicar.")
    root = str(api["PROJECT_ROOT"])
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            exe = os.readlink(p / "exe")
            if exe.startswith(root + "/emulator/") or exe.startswith(root + "/tools/roms/runtimes/"):
                raise ValueError(f"Runtime do projeto ativo: PID {p.name}. Encerre normalmente.")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass


def free_bytes(root):
    st = os.statvfs(root)
    return st.f_bavail * st.f_frsize


def share_item(root, item):
    src = safe_path(root, item["source"])
    dst = safe_path(root, item["destination"])
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    with os.fdopen(os.open(src, os.O_RDONLY | flags), "rb") as sf, os.fdopen(os.open(dst, os.O_RDWR | flags), "r+b") as df:
        if facts(os.fstat(sf.fileno())) != item["source_before"] or facts(os.fstat(df.fileno())) != item["destination_before"]:
            raise ValueError(f"Plano obsoleto: {dst}")
        if digest_fd(sf.fileno()) != item["sha256"] or digest_fd(df.fileno()) != item["sha256"]:
            raise ValueError(f"Conteúdo alterado: {dst}")
        count = share_fds(sf.fileno(), df.fileno(), item["source_before"]["size_bytes"])
        if facts(os.fstat(sf.fileno())) != item["source_before"] or facts(os.fstat(df.fileno())) != item["destination_before"]:
            raise ValueError(f"Metadados inesperados: {dst}")
    return count


def applied_paths(op, root):
    applied = set()
    for line in (op / 'events.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event.get('event') == 'extent_shared':
            applied.add(str(Path(event['destination']).relative_to(root)))
    return applied


class StopRequested(Exception):
    pass


def verify_items(root, items, applied, api):
    failures, cache = [], {}
    progress = {'checked': 0}
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(15):
            print(f"storage verify: {progress['checked']}/{len(applied)}", file=sys.stderr, flush=True)
    worker = threading.Thread(target=heartbeat, daemon=True)
    worker.start()
    verified = 0
    try:
        for item in items:
            if item['destination'] not in applied:
                continue
            valid = True
            for field in ('source', 'destination'):
                try:
                    p = safe_path(root, item[field])
                    current = facts(p.stat())
                    same = current == item[field + '_before']
                    if same:
                        key = (p, tuple(current.values()))
                        if key not in cache:
                            cache[key] = api['sha256_file'](p)
                        same = cache[key] == item['sha256'] and facts(p.stat()) == current
                except (ValueError, OSError):
                    same = False
                if not same:
                    failures.append(item[field])
                    valid = False
            verified += int(valid)
            progress['checked'] += 1
    finally:
        stop.set()
        worker.join(timeout=1)
    return failures, verified


def dispatch(args, api):
    root = api["PROJECT_ROOT"]
    if args.storage_action == "plan":
        data = json.loads(Path(args.groups).read_text())
        items, seen, skipped = [], set(), []
        for group in data["groups"]:
            h = group["sha256"]
            paths = list(dict.fromkeys(group["paths"]))
            valid = []
            for path in paths:
                try:
                    q = safe_path(root, path)
                    f = facts(q.stat())
                    if not f["size_bytes"] or api["sha256_file"](q) != h or facts(q.stat()) != f:
                        raise ValueError("hash/metadata changed since discovery")
                    valid.append((path, f))
                except (ValueError, OSError) as exc:
                    skipped.append({"path": path, "reason": str(exc)})
            if len(valid) < 2:
                continue
            source_path, sf = valid[0]
            for path, tf in valid[1:]:
                if path in seen or tf["ino"] == sf["ino"] and tf["dev"] == sf["dev"]:
                    continue
                if tf["dev"] != sf["dev"] or tf["size_bytes"] != sf["size_bytes"]:
                    skipped.append({"path": path, "reason": "different filesystem/size"})
                    continue
                items.append({"source": source_path, "destination": path, "sha256": h,
                              "source_before": sf, "destination_before": tf})
                seen.add(path)
        oid = "storage-dedupe-" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        op = api["operation_dir"](oid)
        op.mkdir(parents=True, exist_ok=False)
        manifest = {"schema_version": "1.0", "operation_id": oid, "kind": "storage_dedupe",
                    "status": "prepared", "created_at": api["now"](), "items": items, "skipped": skipped,
                    "policy": {"permanent_delete": False, "path_change": False, "content_change": False,
                               "kernel_compare_before_share": True,
                               "rollback": "No logical change to revert. Physical unsharing requires extra free space; never automatic."}}
        api["atomic_json"](op / "manifest.json", manifest)
        api["append_event"](oid, "storage_plan_prepared", action="storage_dedupe", status="prepared",
                            details={"items": len(items), "groups_source": str(args.groups)})
        api["rebuild_history_index"]()
        return {"operation_id": oid, "status": "prepared", "items": len(items),
                "skipped": len(skipped),
                "candidate_logical_bytes": sum(i["source_before"]["size_bytes"] for i in items)}
    oid = args.operation_id
    if Path(oid).name != oid or not oid.startswith("storage-dedupe-"):
        raise ValueError("ID inválido")
    op = api["operation_dir"](oid)
    manifest = json.loads((op / "manifest.json").read_text())
    if manifest.get("kind") != "storage_dedupe":
        raise ValueError("Tipo de operação inválido")
    if args.storage_action in {"apply", "resume"}:
        allowed = {"prepared"} if args.storage_action == 'apply' else {"applying", "paused", "partial", "interrupted"}
        if not args.apply or manifest["status"] not in allowed:
            raise ValueError("Exige --apply e estado apropriado para apply/resume")
        if args.max_items <= 0 or args.max_bytes <= 0:
            raise ValueError("Limites de lote devem ser positivos")
        assert_idle(api)
        before = free_bytes(root)
        already_applied = applied_paths(op, root)
        if manifest['status'] == 'prepared':
            manifest['free_before_bytes'] = before
        manifest.setdefault('free_before_bytes', None)
        manifest['last_attempt_free_before_bytes'] = before
        manifest['last_attempt_at'] = api['now']()
        manifest.pop('error', None)
        manifest["status"] = "applying"
        api["atomic_json"](op / "manifest.json", manifest)
        results, apply_skipped = [], manifest.get('apply_skipped', [])
        previously_skipped = {x['destination'] for x in apply_skipped}
        attempted = batch_bytes = 0
        stop = threading.Event()
        progress = {'completed': len(already_applied), 'target': ''}
        def heartbeat():
            while not stop.wait(15):
                print(f"storage: completed={progress['completed']}/{len(manifest['items'])} target={progress['target']}", file=sys.stderr, flush=True)
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        def stopping(signum, frame):
            raise StopRequested(f'signal {signum}')
        old_handlers = {s: signal.signal(s, stopping) for s in (signal.SIGTERM, signal.SIGINT)}
        try:
            for item in manifest["items"]:
                if item['destination'] in already_applied or item['destination'] in previously_skipped:
                    continue
                length = item['source_before']['size_bytes']
                if attempted and (attempted >= args.max_items or batch_bytes + length > args.max_bytes):
                    break
                attempted += 1
                batch_bytes += length
                progress['target'] = item['destination']
                try:
                    count = share_item(root, item)
                except (OSError, ValueError) as exc:
                    skipped = {"destination": item["destination"], "reason": str(exc)}
                    apply_skipped.append(skipped)
                    api["append_event"](oid, "extent_skipped", action="storage_dedupe", status="skipped", details=skipped)
                    continue
                result = {"destination": item["destination"], "status": "shared", "kernel_deduped_bytes": count}
                results.append(result)
                progress['completed'] += 1
                api["append_event"](oid, "extent_shared", action="storage_dedupe", status="completed",
                                    source=str(root / item["source"]), destination=str(root / item["destination"]), sha256_before=item["sha256"],
                                    sha256_after=item["sha256"], details=result)
            remaining = len(manifest['items']) - len(already_applied) - len(results) - len(apply_skipped)
            manifest["status"] = 'paused' if remaining else 'partial' if apply_skipped else 'completed'
        except Exception as exc:
            manifest["status"] = "interrupted" if isinstance(exc, StopRequested) else "partial"
            manifest["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            worker.join(timeout=1)
            for signum, previous in old_handlers.items():
                signal.signal(signum, previous)
        sync = subprocess.run(["btrfs", "filesystem", "sync", str(root)], capture_output=True, text=True, timeout=30)
        manifest.update(completed_at=api["now"](), free_after_bytes=free_bytes(root),
                        sync_returncode=sync.returncode, completed_items=len(already_applied)+len(results),
                        last_batch_completed_items=len(results), apply_skipped=apply_skipped)
        api["atomic_json"](op / "manifest.json", manifest)
        api["append_event"](oid, "storage_apply_finished", action="storage_dedupe", status=manifest["status"],
                            details={k: v for k, v in manifest.items() if k not in {"items", "policy"}})
        api["rebuild_history_index"]()
        return {k: v for k, v in manifest.items() if k not in {"items", "policy"}}
    applied = applied_paths(op, root)
    failures, verified_items = verify_items(root, manifest['items'], applied, api)
    result = {"operation_id": oid, "status": "verified" if not failures else "failed", "failures": failures,
              "verified_items": verified_items, "unapplied_items": len(manifest['items'])-len(applied),
              "scope": "successfully applied items only; unapplied/skipped items are explicit", "content_and_identity_preserved": not failures}
    api["atomic_json"](op / "verification.json", result)
    api["append_event"](oid, "storage_verified", action="storage_dedupe", status=result["status"], details=result)
    return result
