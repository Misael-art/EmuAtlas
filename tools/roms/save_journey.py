"""RetroArch save history. Native payloads, SQLite metadata and session-owned IPC."""
from __future__ import annotations

import argparse
import codecs
import csv
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zlib

TAGS = {"boss": "Chefe", "ending": "Final", "challenge": "Hiperdesafio",
        "discovery": "Descoberta", "checkpoint": "Marco", "practice": "Treino"}
MAX_PAYLOAD = 512 * 1024 * 1024
MIN_FREE = 10 * 1024**3
CONTEXT_ENV = "EMUATLAS_RETROARCH_JOURNEY"


def safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("O histórico exige caminhos relativos à instalação.")
    path = root / relative
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Um caminho do histórico foi substituído por link.")
        if part == root:
            break
    return path


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def signature(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def atomic(path, data):
    try:
        from .romtool import atomic_write_bytes
    except ImportError:
        from romtool import atomic_write_bytes
    atomic_write_bytes(path, data)


def state_chunks(path):
    """Read RetroArch RZIP v1 with bounded decompression and complete checks."""
    with Path(path).open("rb") as stream:
        header = stream.read(20)
        if not header.startswith(b"#RZIPv\x01#"):
            stream.seek(0)
            while chunk := stream.read(1024*1024):
                yield chunk
            return
        if len(header) != 20:
            raise BlockingIOError("Cabeçalho RZIP incompleto.")
        block_size = int.from_bytes(header[8:12], "little")
        total_size = int.from_bytes(header[12:20], "little")
        if not 0 < block_size <= 8*1024*1024 or not 0 < total_size <= MAX_PAYLOAD:
            raise ValueError("Os limites do contêiner RZIP são incompatíveis com esta captura.")
        remaining = total_size
        while remaining:
            length_bytes = stream.read(4)
            if len(length_bytes) != 4:
                raise BlockingIOError("Bloco RZIP incompleto.")
            length = int.from_bytes(length_bytes, "little")
            if not 0 < length <= block_size*2+1024:
                raise ValueError("Bloco RZIP fora dos limites.")
            compressed = stream.read(length)
            if len(compressed) != length:
                raise BlockingIOError("A gravação RZIP ainda está em andamento.")
            decoder = zlib.decompressobj()
            expected = min(block_size, remaining)
            try:
                chunk = decoder.decompress(compressed, expected+1)
            except zlib.error as exc:
                raise ValueError("O checksum do bloco RZIP falhou.") from exc
            if len(chunk) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError("O tamanho ou o término do bloco RZIP diverge.")
            yield chunk
            remaining -= len(chunk)
        if stream.read(1):
            raise ValueError("Dados adicionais após o fim do RZIP.")


def validate_state(path):
    buffer = bytearray()
    phase, remaining, has_memory, ended, total = "signature", 0, False, False, 0
    for chunk in state_chunks(path):
        total += len(chunk)
        if total > MAX_PAYLOAD:
            raise ValueError("State descomprimido acima de 512 MiB.")
        buffer.extend(chunk)
        while buffer:
            if phase == "opaque":
                buffer.clear()
                break
            if ended:
                raise ValueError("Dados adicionais após END do state.")
            if phase == "signature":
                if len(buffer) < 8:
                    break
                if bytes(buffer[:8]) != b"RASTATE\x01":
                    phase = "opaque"
                    continue
                del buffer[:8]
                phase = "header"
            elif phase == "header":
                if len(buffer) < 8:
                    break
                tag, size = bytes(buffer[:4]), int.from_bytes(buffer[4:8], "little")
                del buffer[:8]
                if tag == b"END ":
                    if size or not has_memory:
                        raise ValueError("Terminador ou bloco de memória inválido.")
                    ended = True
                else:
                    if size > MAX_PAYLOAD or (tag == b"MEM " and (has_memory or size == 0)):
                        raise ValueError("Bloco de memória do state inválido.")
                    has_memory = has_memory or tag == b"MEM "
                    remaining = (size+7)//8*8
                    phase = "body" if remaining else "header"
            else:
                taken = min(remaining, len(buffer))
                del buffer[:taken]
                remaining -= taken
                if not remaining:
                    phase = "header"
    if phase == "opaque":
        return "opaque_legacy_or_core_format"
    if not ended or buffer:
        raise BlockingIOError("A estrutura RASTATE ainda está incompleta.")
    return "complete_rastate_v1"


class Store:
    """One portable source of truth per game/core; payloads precede transactions."""

    def __init__(self, root, system_id, rom, core):
        self.root = Path(root).absolute()
        rom = Path(rom)
        self.rom = (rom if rom.is_absolute() else self.root/rom).relative_to(self.root).as_posix()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", system_id):
            raise ValueError("Plataforma inválida.")
        if not self.rom.startswith(f"roms/{system_id}/"):
            raise ValueError("O jogo deve pertencer à biblioteca canônica.")
        safe_path(self.root, self.rom)
        self.system_id = system_id
        self.core = re.sub(r"\.(so|dll|dylib)$", "", Path(core).name, flags=re.IGNORECASE)
        self.game_id = hashlib.sha256(f"{system_id}\n{self.rom}\n{self.core}".encode()).hexdigest()[:24]
        self.directory = safe_path(self.root, f"emulator/data/RetroArch/states/journey/{self.game_id}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = safe_path(self.directory, "history.sqlite3")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    preview TEXT,
                    companion INTEGER REFERENCES entries(sequence),
                    favorite INTEGER NOT NULL DEFAULT 0,
                    tags TEXT NOT NULL DEFAULT '[]',
                    stage INTEGER,
                    note TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_at TEXT, seconds REAL DEFAULT 0,
                    ended_at TEXT, status TEXT
                );
                CREATE TABLE IF NOT EXISTS loading_cache (
                    path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, signature TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entries_chronological
                    ON entries(favorite DESC, julianday(created_at) DESC, sequence DESC);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, limit=100, offset=0):
        with self.connect() as db:
            return [self.decode(row) for row in db.execute(
                "SELECT * FROM entries ORDER BY favorite DESC, julianday(created_at) DESC, sequence DESC LIMIT ? OFFSET ?",
                (limit, offset))]

    @staticmethod
    def decode(row):
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["tags"] = json.loads(result["tags"])
        return result

    def get(self, sequence):
        with self.connect() as db:
            row = db.execute("SELECT * FROM entries WHERE sequence=?", (sequence,)).fetchone()
        if row is None:
            raise ValueError("A gravação selecionada deixou de existir.")
        return self.decode(row)

    def count(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def total_seconds(self):
        with self.connect() as db:
            return db.execute("SELECT COALESCE(SUM(seconds),0) FROM sessions").fetchone()[0]

    def session(self, identifier, seconds, status="running"):
        now = dt.datetime.now().astimezone().isoformat()
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id,started_at,seconds,status) VALUES(?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET seconds=excluded.seconds,status=excluded.status,"
                       "ended_at=CASE WHEN excluded.status='running' THEN NULL ELSE ? END",
                       (identifier, now, seconds, status, now))

    def preserve_payload(self, source, suffix, allow_empty=False):
        relative = source.relative_to(self.root).as_posix()
        safe_path(self.root, relative)
        before = signature(source)
        if before[2] > MAX_PAYLOAD or (before[2] == 0 and not allow_empty):
            raise ValueError("O arquivo está vazio ou excede 512 MiB. O original foi preservado.")
        if shutil.disk_usage(self.directory).free < MIN_FREE + before[2] * 2:
            raise OSError("Espaço insuficiente para preservar a reserva de 10 GiB.")
        sha = digest(source)
        if signature(source) != before:
            raise BlockingIOError("A gravação ainda está em andamento.")
        payload = f"objects/{sha}{suffix}"
        target = safe_path(self.directory, payload)
        if target.exists():
            if digest(target) != sha:
                raise ValueError("O payload preservado falhou na verificação de hash.")
        else:
            target.parent.mkdir(exist_ok=True)
            temporary = target.with_name("." + uuid.uuid4().hex + ".part")
            try:
                with source.open("rb") as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                if signature(source) != before or digest(temporary) != sha:
                    raise BlockingIOError("O arquivo mudou durante a preservação.")
                os.replace(temporary, target)
                try:
                    from .romtool import sync_directory_chain
                except ImportError:
                    from romtool import sync_directory_chain
                sync_directory_chain(target.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return {"source": relative, "payload": payload, "sha256": sha,
                "size_bytes": before[2], "source_mtime_ns": before[3]}

    def capture(self, source, kind, metadata, preview=None):
        source = Path(source)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            payload = self.preserve_payload(source, ".state" if kind == "state" else source.suffix.lower())
            relative, sha = payload["source"], payload["sha256"]
            last = db.execute("SELECT sha256 FROM entries WHERE source=? ORDER BY sequence DESC LIMIT 1",
                              (relative,)).fetchone()
            if last and last["sha256"] == sha:
                return None
            dependencies = []
            clock = source.with_suffix(".rtc")
            if kind == "save" and clock.is_file() and clock != source:
                dependencies.append(self.preserve_payload(clock, ".rtc", allow_empty=True))
            if source.stat().st_mtime_ns != payload["source_mtime_ns"] or digest(source) != sha:
                raise BlockingIOError("O conjunto mudou durante a preservação.")
            captured = dt.datetime.now().astimezone().isoformat()
            value = {**metadata, **payload, "schema": 1, "system_id": self.system_id, "rom": self.rom,
                     "core": self.core, "recorded_at": captured, "dependencies": dependencies}
            row = db.execute("INSERT INTO entries(created_at,kind,source,sha256,metadata) VALUES(?,?,?,?,?)",
                             (metadata.get("saved_at", captured), kind, relative, sha,
                              json.dumps(value, ensure_ascii=False)))
            sequence = row.lastrowid
            if preview is not None:
                try:
                    preview_path = self.make_preview(sequence, preview, {**value, "sequence": sequence})
                    db.execute("UPDATE entries SET preview=? WHERE sequence=?", (preview_path, sequence))
                except (OSError, ValueError) as exc:
                    value["image_error"] = str(exc)
                    db.execute("UPDATE entries SET metadata=? WHERE sequence=?",
                               (json.dumps(value, ensure_ascii=False), sequence))
        return sequence

    def make_preview(self, sequence, source, metadata):
        from PIL import Image, PngImagePlugin
        try:
            from .save_capture import _validate_png
        except ImportError:
            from save_capture import _validate_png
        with Path(source).open("rb") as stream:
            header = stream.read(24)
        if len(header) < 24 or int.from_bytes(header[16:20], "big") * int.from_bytes(header[20:24], "big") > 16_777_216:
            raise ValueError("A imagem de captura excede o limite de pixels ou está incompleta.")
        try:
            _validate_png(Path(source), max_bytes=64*1024*1024)
        except zlib.error as exc:
            raise ValueError("O fluxo da imagem PNG está corrompido.") from exc
        with Image.open(source) as image:
            if image.width * image.height > 16_777_216:
                raise ValueError("A imagem de captura excede o limite de pixels.")
            image.load()
            image = image.convert("RGB")
            info = PngImagePlugin.PngInfo()
            info.add_text("EmuAtlas", json.dumps(metadata, ensure_ascii=False), zip=True)
            output = io.BytesIO()
            image.save(output, format="PNG", pnginfo=info)
        relative = f"previews/{sequence:08d}.png"
        path = safe_path(self.directory, relative)
        path.parent.mkdir(exist_ok=True)
        atomic(path, output.getvalue())
        return relative

    def set_preview(self, sequence, source, accuracy):
        row = self.get(sequence)
        with self.connect() as db:
            metadata = {**row["metadata"], "image_accuracy": accuracy}
            metadata.pop("image_error", None)
            preview = self.make_preview(sequence, source, {**metadata, "sequence": sequence})
            db.execute("UPDATE entries SET preview=?,metadata=? WHERE sequence=?",
                       (preview, json.dumps(metadata, ensure_ascii=False), sequence))
            db.execute("UPDATE entries SET preview=? WHERE companion=?", (preview, sequence))

    def annotate(self, sequence, *, favorite=None, tags=None, stage=None, note=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            found = db.execute("SELECT * FROM entries WHERE sequence=?", (sequence,)).fetchone()
            if found is None:
                raise ValueError("A gravação selecionada deixou de existir.")
            row = self.decode(found)
            selected_tags = row["tags"] if tags is None else tags
            if not isinstance(selected_tags, list) or any(tag not in TAGS for tag in selected_tags):
                raise ValueError("Marcação desconhecida.")
            if stage is not None and (type(stage) is not int or not 0 <= stage <= 9999):
                raise ValueError("Use uma fase entre 1 e 9999, ou zero para remover.")
            text = row["note"] if note is None else note.strip()
            if len(text) > 500:
                raise ValueError("A nota aceita até 500 caracteres.")
            db.execute("UPDATE entries SET favorite=?,tags=?,stage=?,note=?,revision=revision+1 WHERE sequence=?",
                       (row["favorite"] if favorite is None else bool(favorite),
                        json.dumps(sorted(set(selected_tags))), row["stage"] if stage is None else stage or None,
                        text, sequence))

    def pair(self, battery, state):
        with self.connect() as db:
            source = self.get(battery)
            target = self.get(state)
            metadata = {**source["metadata"], "companion_delay_seconds":
                round((dt.datetime.fromisoformat(target["created_at"]) -
                       dt.datetime.fromisoformat(source["created_at"])).total_seconds(), 3),
                "image_accuracy": "after_disk_write_companion_state"}
            db.execute("UPDATE entries SET companion=?,metadata=?,preview=? WHERE sequence=? AND kind='save'",
                       (state, json.dumps(metadata, ensure_ascii=False), target["preview"], battery))

    def export_csv(self, destination):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Sequência", "Tipo", "Data e hora", "Favorito", "Tags", "Fase", "Nota",
                         "Sessão (s)", "Total observado (s)", "Core", "ROM", "Origem", "SHA-256",
                         "Arquivo preservado", "Imagem", "Precisão da imagem", "State acompanhante",
                         "Intervalo até state (s)", "Origem do registro", "Dependências"])
        def cell(value):
            value = str(value if value is not None else "")
            return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
        for row in self.rows(limit=2_147_483_647):
            meta = row["metadata"]
            preserved = (self.directory / meta["payload"]).relative_to(self.root).as_posix()
            preview = (self.directory / row["preview"]).relative_to(self.root).as_posix() if row["preview"] else ""
            writer.writerow([cell(value) for value in [row["sequence"], row["kind"], row["created_at"],
                bool(row["favorite"]), ", ".join(TAGS[tag] for tag in row["tags"]), row["stage"], row["note"],
                meta.get("session_seconds"), meta.get("total_observed_seconds"), meta["core"], meta["rom"],
                row["source"], row["sha256"], preserved, preview,
                meta.get("image_accuracy") if preview else "Sem imagem", row["companion"],
                meta.get("companion_delay_seconds"), meta.get("origin"),
                json.dumps(meta.get("dependencies", []), ensure_ascii=False)]])
        atomic(Path(destination), output.getvalue().encode("utf-8-sig"))

    def clean_loading_cache(self):
        with self.connect() as db:
            for row in db.execute("SELECT * FROM loading_cache").fetchall():
                path = safe_path(self.root, row["path"])
                # The committed intent names a derived copy. A changed file is
                # preserved and becomes a normal native slot on the next scan.
                if path.is_file() and list(signature(path)) == json.loads(row["signature"]) and digest(path) == row["sha256"]:
                    path.unlink()
                    try:
                        from .romtool import sync_directory_chain
                    except ImportError:
                        from romtool import sync_directory_chain
                    sync_directory_chain(path.parent)
                db.execute("DELETE FROM loading_cache WHERE path=?", (row["path"],))


def publish_exclusive(source, target):
    if os.name == "nt":
        os.rename(source, target)  # Windows rename refuses an existing target.
    else:
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(target))
    try:
        from .romtool import sync_directory_chain
    except ImportError:
        from romtool import sync_directory_chain
    sync_directory_chain(target.parent)


def config_values(path):
    return dict(re.findall(r'(?m)^\s*(\w+)\s*=\s*"?([^"\n]*?)"?\s*$', path.read_text()))


class RetroArchSession:
    """A single worker observes this game's files and commands its owned child."""

    def __init__(self, root, context, operation_directory):
        self.root, self.context = Path(root), context
        for name in ("rom", "core", "config", "content"):
            safe_path(self.root, context[name])
        self.rom = self.root / context["rom"]
        self.core = self.root / context["core"]
        self.content = self.root / context["content"]
        self.config = config_values(self.root / context["config"])
        if Path(self.config.get("libretro_directory", "")) != self.core.parent:
            raise ValueError("O diretório de cores da sessão precisa coincidir com o core lançado.")
        info = safe_path(self.root, f"emulator/data/RetroArch/info/{self.core.stem}.info")
        if not info.is_file() or not config_values(info).get("systemid", "").strip():
            # RetroArch 1.22.2's GET_STATUS dereferences core_info->system_id.
            # An incomplete info record can crash the emulator itself.
            raise ValueError("O monitor exige o .info do core com systemid válido.")
        self.supports_states = config_values(info).get("savestate", "true") != "false"
        self.store = Store(self.root, context["system_id"], self.rom, self.core)
        self.lease = safe_path(self.store.directory, "monitor.lock").open("a+b")
        if os.name == "nt":
            import msvcrt
            if self.lease.tell() == 0:
                self.lease.write(b"\0")
                self.lease.flush()
            self.lease.seek(0)
            msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.store.clean_loading_cache()
        self.native = self.root / "emulator/data/RetroArch"
        self.operation = Path(operation_directory)
        self.identifier = self.operation.name
        self.session_directory = safe_path(self.native, f"states/journey/{self.store.game_id}/sessions/{self.identifier}")
        self.session_directory.mkdir(parents=True, exist_ok=True)
        self.options = self.session_directory / "session.cfg"
        self.screenshots = Path(self.config.get("screenshot_directory", str(self.native / "screenshots")))
        safe_path(self.root, self.screenshots.relative_to(self.root))
        self.screenshots.mkdir(parents=True, exist_ok=True)
        self.core_hash = digest(self.core)
        self.content_hash = digest(self.content) if self.content.stat().st_size <= 64*1024*1024 else None
        self.content_crc32 = None
        self.rom_signature = list(signature(self.rom)[2:])
        self.started = time.monotonic()
        self.seconds, self.last_time = 0.0, time.monotonic()
        self.previous_seconds = self.store.total_seconds()
        self.status, self.subject, self.version = "UNKNOWN", "", ""
        self.message = ""
        self.errors = []
        self.last_resume = None
        self.revision = 0
        self.actions = queue.Queue(maxsize=32)
        self.lines = queue.Queue(maxsize=256)
        self.stopping = threading.Event()
        self.child = self.thread = self.reader = None
        self.pending = {}
        self.incomplete_attempts = {}
        self.baseline = {}
        self.imports = []
        self.battery_pending = []
        self.preview_pending = []
        self.reserved = {}
        self.load_pending = None
        self.load_requested = None
        self.latest_log = ""
        self.output_position = self.last_status_position = self.status_generation = 0
        self.last_load_outcome = None
        self.native_paths = {}
        self.paused_by_us = False
        self.pause_wanted = False
        self.last_pause_command = 0
        self.last_scan = 0
        self.last_import = 0
        self.write_options()

    def write_options(self):
        values = {"stdin_cmd_enable": "true",
                  "savestate_thumbnail_enable": "true", "savestate_auto_index": "true",
                  "savestate_max_keep": "0", "config_save_on_exit": "false",
                  "autosave_interval": "5"}
        atomic(self.options, "".join(f'{key} = "{value}"\n' for key, value in values.items()).encode())

    def prepare(self, command):
        if str(self.core) not in command or str(self.content) not in command:
            raise ValueError("O contexto de saves diverge do core/conteúdo lançado.")
        # Append options after existing arguments; RetroArch's parser accepts
        # options after the positional content without changing its identity.
        return [*command, "--verbose", "--appendconfig", str(self.options)]

    def attach(self, child):
        self.child = child
        self.reader = threading.Thread(target=self.read_output, daemon=True)
        self.thread = threading.Thread(target=self.work, daemon=True)
        self.reader.start()
        self.thread.start()

    def read_output(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        log = None
        try:
            log = (self.operation / "retroarch-journey.log").open("ab", buffering=0)
        except OSError as exc:
            self.message = "Registro de diagnóstico indisponível · " + str(exc)
        try:
            while True:
                chunk = self.child.stdout.read(4096)
                if not chunk:
                    break
                if log:
                    try:
                        log.write(chunk)
                    except OSError:
                        log.close()
                        log = None
                        self.message = "Diagnóstico sem espaço. A entrada do jogo continua ativa."
                try:
                    self.lines.put_nowait(decoder.decode(chunk))
                except queue.Full:
                    self.message = "Monitor atrasado. As gravações nativas continuam disponíveis."
                # Preserve emulator diagnostics at the existing entry point.
                if not chunk.startswith(b"GET_STATUS "):
                    try:
                        os.write(1, chunk)
                    except OSError:
                        pass
        finally:
            if log:
                log.close()

    def send(self, command):
        if not self.child or self.child.poll() is not None:
            raise ValueError("A sessão do RetroArch encerrou.")
        self.child.stdin.write((command + "\n").encode())
        self.child.stdin.flush()

    def request(self, action, value=None):
        if self.thread is not None and not self.thread.is_alive():
            self.message = "Monitor indisponível. As gravações nativas continuam disponíveis."
            return False
        try:
            self.actions.put_nowait((action, value))
        except queue.Full:
            raise ValueError("Aguarde a ação de save em andamento.")
        return True

    def read_status(self):
        while True:
            try:
                piece = self.lines.get_nowait()
                self.output_position += len(piece)
                self.latest_log = (self.latest_log + piece)[-32768:]
            except queue.Empty:
                break
        for match in re.finditer(r"GET_STATUS (PLAYING|PAUSED) ([^,\r\n]+),(.+?),crc32=([0-9a-fA-F]+)(?=[\r\n])", self.latest_log):
            position = self.output_position - len(self.latest_log) + match.end()
            if position > self.last_status_position:
                self.last_status_position = position
                self.status_generation += 1
            self.status = match[1]
            self.subject = match[3]
            self.content_crc32 = match[4].lower()
        versions = re.findall(r"RetroArch (\d+\.\d+(?:\.\d+)?)", self.latest_log)
        if versions:
            self.version = versions[-1]
        if self.latest_log.rfind("GET_STATUS CONTENTLESS") > max(
                self.latest_log.rfind("GET_STATUS PLAYING"), self.latest_log.rfind("GET_STATUS PAUSED")):
            self.status, self.subject = "CONTENTLESS", ""
        for match in re.finditer(r'\[Override(?:s)?\][^\r\n]*"([^"\r\n]+)"', self.latest_log):
            path = Path(match[1])
            kind = "state" if ".state" in path.suffix else "save"
            expected = self.native / ("states" if kind == "state" else "saves")
            if path.is_relative_to(expected):
                safe_path(self.root, path.relative_to(self.root))
                self.native_paths[kind] = path

    def paths(self):
        result = {}
        for kind, folder in (("state", "states"), ("save", "saves")):
            active = self.native_paths.get(kind)
            if active:
                directories = [active.parent]
                stem = re.sub(r"\.state(?:\d+|\.auto)?$", "", active.name) if kind == "state" else active.stem
            else:
                directories = [self.native / folder]
                stem = self.content.stem
                # Legacy core/content sorting is bounded and basename-exact.
                for directory in list(directories[0].iterdir())[:256] if directories[0].is_dir() else []:
                    if directory.is_dir() and not directory.is_symlink() and directory.name != "journey":
                        directories.append(directory)
                        if (directory / self.content.parent.name).is_dir():
                            directories.append(directory / self.content.parent.name)
            for directory in directories:
                if not directory.is_dir():
                    continue
                for path in directory.iterdir():
                    if not path.name.startswith(stem + "."):
                        continue
                    if path.is_symlink() or not path.is_file() or path in self.reserved:
                        continue
                    if kind == "state" and not re.fullmatch(re.escape(stem) + r"\.state(?:\d+|\.auto)?", path.name):
                        continue
                    if kind == "save" and path.suffix.lower() not in {".srm", ".sav", ".eep", ".eeprom", ".flash", ".nv"}:
                        continue
                    result[path] = kind
                    if len(result) > 10000:
                        raise ValueError("O monitor atingiu 10.000 arquivos do jogo.")
        return result

    def metadata(self, source, kind, legacy=False):
        saved = dt.datetime.fromtimestamp(source.stat().st_mtime, dt.timezone.utc).astimezone().isoformat()
        known_time = not legacy and self.status in {"PLAYING", "PAUSED"} and self.subject == self.content.stem
        with self.store.connect() as db:
            previous = db.execute("SELECT MAX(sequence) FROM entries").fetchone()[0]
        return {"session_id": self.identifier, "core_sha256": None if legacy else self.core_hash,
                "observed_with_core_sha256": self.core_hash,
                "content_sha256": None if legacy else self.content_hash,
                "content_crc32": None if legacy else self.content_crc32,
                "rom_signature": None if legacy else self.rom_signature,
                "session_seconds": round(self.seconds, 2) if known_time else None,
                "total_observed_seconds": round(self.previous_seconds + self.seconds, 2) if known_time else None,
                "time_basis": ("observed_playing_status" if known_time else
                               "unknown_before_monitoring" if legacy else "status_unconfirmed"),
                "saved_at": saved, "date_basis": "filesystem_mtime",
                "origin": "existing_file" if legacy else "observed_disk_write",
                "image_accuracy": "native_state_thumbnail" if kind == "state" else "pending_after_disk_write",
                "previous_sequence": previous, "last_shelf_resume_sequence": self.last_resume,
                "retroarch_version": self.version or "unknown",
                "content_name": self.content.name}

    def capture(self, source, kind, legacy=False):
        validation = None
        if kind == "state":
            try:
                validation = validate_state(source)
            except (OSError, ValueError):
                if not legacy:
                    raise
                validation = "incomplete_or_invalid_legacy_state"
        thumbnail = Path(str(source) + ".png")
        preview = thumbnail if (kind == "state" and thumbnail.is_file() and
            thumbnail.stat().st_mtime_ns >= source.stat().st_mtime_ns - 50_000_000) else None
        sequence = self.store.capture(source, kind,
            {**self.metadata(source, kind, legacy), "container_validation": validation}, preview)
        if sequence is None:
            return
        self.revision += 1
        self.message = f"Gravação #{sequence} preservada"
        if kind == "state":
            for battery, requested in self.battery_pending:
                if time.monotonic() - requested < 8:
                    self.store.pair(battery, sequence)
            self.battery_pending.clear()
            if not self.store.get(sequence)["preview"]:
                self.preview_pending.append((sequence, thumbnail, time.monotonic(), "native_state_thumbnail"))
        elif (not legacy and self.child and self.child.poll() is None and
              self.subject == self.content.stem and self.status in {"PLAYING", "PAUSED"}):
            requested = time.monotonic()
            if self.load_pending is not None or self.load_requested is not None:
                self.message = f"Save #{sequence} preservado durante a retomada"
            elif self.supports_states and shutil.disk_usage(self.store.directory).free >= MIN_FREE + MAX_PAYLOAD*3:
                in_flight = bool(self.battery_pending)
                self.battery_pending.append((sequence, requested))
                if not in_flight:
                    # RetroArch rejects a second blocking task while a screenshot
                    # is being saved. The companion's native thumbnail serves both.
                    self.send("SAVE_STATE")
            else:
                screenshots = set(self.screenshots.glob("*.png"))
                self.send("SCREENSHOT")
                self.preview_pending.append((sequence, screenshots, requested, "after_disk_write"))
                self.message = f"Save #{sequence} preservado · state acompanhante indisponível"

    def import_and_scan(self):
        now = time.monotonic()
        current = self.paths()
        subject_matches = self.subject == self.content.stem
        for source, kind in current.items():
            sig = signature(source)
            if source not in self.baseline:
                self.baseline[source] = sig
                self.pending[source] = (sig, now, kind)
                continue
            if sig != self.baseline[source]:
                self.baseline[source] = sig
                self.pending[source] = (sig, now, kind)
                self.incomplete_attempts.pop(source, None)
        for source, (sig, seen, kind) in list(self.pending.items()):
            if not subject_matches and self.status != "UNKNOWN":
                self.message = "Outro conteúdo está ativo. O histórico desta entrada está suspenso."
                continue
            if source not in current:
                self.pending.pop(source, None)
                continue
            if now - seen < .5 or signature(source) != sig:
                continue
            try:
                self.capture(source, kind)
                self.pending.pop(source, None)
                self.incomplete_attempts.pop(source, None)
            except BlockingIOError as exc:
                attempts = self.incomplete_attempts.get(source, 0)+1
                self.incomplete_attempts[source] = attempts
                if attempts < 3:
                    self.pending[source] = (signature(source), now+1, kind)
                else:
                    self.pending.pop(source, None)
                    self.errors.append({"source": source.relative_to(self.root).as_posix(), "error": str(exc)})
                    self.message = "Gravação incompleta preservada na origem"
            except (OSError, ValueError) as exc:
                self.pending.pop(source, None)
                self.message = "Histórico da gravação indisponível · " + str(exc)
                self.errors.append({"source": source.relative_to(self.root).as_posix(), "error": str(exc)})
                atomic(self.operation / "save-journey-error.json", json.dumps(
                    {"source": str(source.relative_to(self.root)), "error": str(exc)}).encode())
        # One legacy file per poll bounds disk traffic while making old slots visible.
        if self.imports and now-self.last_import >= 2:
            self.last_import = now
            source, kind = self.imports.pop(0)
            if source.exists() and source not in self.pending:
                try:
                    self.capture(source, kind, legacy=True)
                except (OSError, ValueError) as exc:
                    self.errors.append({"source": source.relative_to(self.root).as_posix(), "error": str(exc)})
                    self.message = "Arquivo antigo preservado · " + str(exc)

    def previews(self):
        now = time.monotonic()
        for item in list(self.preview_pending):
            sequence, candidate, requested, accuracy = item
            if isinstance(candidate, set):
                files = [p for p in self.screenshots.glob("*.png") if p not in candidate]
                source = max(files, key=lambda p: p.stat().st_mtime_ns) if files else None
            else:
                source = candidate if (candidate.is_file() and candidate.stat().st_mtime_ns >=
                    self.store.get(sequence)["metadata"]["source_mtime_ns"] - 50_000_000) else None
            if source and now - requested > .25:
                try:
                    self.store.set_preview(sequence, source, accuracy)
                    self.preview_pending.remove(item)
                    self.revision += 1
                except (OSError, ValueError):
                    if now - requested > 5:
                        self.preview_pending.remove(item)
            elif now - requested > 5:
                self.preview_pending.remove(item)
                self.message = f"Gravação #{sequence} preservada · imagem indisponível"

    def begin_load(self, sequence):
        if self.load_pending:
            raise ValueError("Aguarde a retomada em andamento.")
        if self.subject != self.content.stem or self.status not in {"PLAYING", "PAUSED"}:
            raise ValueError("O RetroArch ainda não confirmou este jogo como conteúdo ativo.")
        if not self.supports_states:
            raise ValueError("Este core declara ausência de suporte a save states.")
        row = self.store.get(sequence)
        if row["kind"] == "save":
            if row["companion"] is None:
                raise ValueError("Save interno sem state acompanhante. Use o carregamento nativo do jogo.")
            row = self.store.get(row["companion"])
        if row["metadata"].get("core_sha256") and row["metadata"]["core_sha256"] != self.core_hash:
            raise ValueError("O core mudou. Use a versão original para carregar este state.")
        metadata = row["metadata"]
        if metadata.get("container_validation") == "incomplete_or_invalid_legacy_state":
            raise ValueError("Este state histórico está incompleto. O arquivo original foi preservado.")
        if metadata.get("content_sha256") and metadata["content_sha256"] != self.content_hash:
            raise ValueError("O conteúdo do jogo mudou desde esta gravação.")
        if metadata.get("content_crc32") and metadata["content_crc32"] != self.content_crc32:
            raise ValueError("O CRC do conteúdo ativo diverge desta gravação.")
        if not metadata.get("content_sha256") and metadata.get("rom_signature") and metadata["rom_signature"] != self.rom_signature:
            raise ValueError("O arquivo do jogo foi alterado. Confira a identidade antes de retomar.")
        source = safe_path(self.store.directory, row["metadata"]["payload"])
        if shutil.disk_usage(self.store.directory).free < MIN_FREE + source.stat().st_size*3:
            raise ValueError("A retomada precisa preservar a reserva de 10 GiB.")
        if digest(source) != row["sha256"]:
            raise ValueError("A versão preservada falhou na verificação de integridade.")
        active = self.native_paths.get("state")
        if active is None:
            candidates = [p for p, k in self.paths().items() if k == "state"]
            if not candidates:
                raise ValueError("O RetroArch ainda não confirmou o caminho nativo dos states.")
            active = candidates[0]
        base = re.sub(r"\.state(?:\d+|\.auto)?$", ".state", str(active))
        slot = 1_000_000 + int(uuid.uuid4().hex[:6], 16)
        target = Path(base + str(slot))
        safe_path(self.root, target.relative_to(self.root))
        if target.exists():
            raise ValueError("O slot temporário já existe. Tente novamente.")
        # Preserve a recoverable current state before changing the emulated timeline.
        before = set(self.paths())
        with self.store.connect() as db:
            floor = db.execute("SELECT COALESCE(MAX(sequence),0) FROM entries").fetchone()[0]
        self.send("SAVE_STATE")
        self.load_pending = {"sequence": sequence, "target": target, "slot": slot,
                             "started": time.monotonic(), "before": before,
                             "sequence_floor": floor, "phase": "backup",
                             "source": source, "sha256": row["sha256"],
                             "timeout": max(12, 20+source.stat().st_size/(100*1024*20))}
        self.message = "Preservando o momento atual…"

    def advance_load(self):
        pending = self.load_pending
        if not pending:
            return
        age = time.monotonic() - pending["started"]
        if pending["phase"] == "render":
            if self.status_generation >= pending["render_status"] + 2:
                self.last_resume = pending["sequence"]
                self.last_load_outcome = "loaded"
                self.message = f"Gravação #{pending['sequence']} pronta para continuar"
                self.load_pending = None
                self.revision += 1
                self.store.clean_loading_cache()
                self.reserved.clear()
            return
        if pending["phase"] == "backup":
            new = set(self.paths()) - pending["before"]
            with self.store.connect() as db:
                candidates = db.execute("SELECT * FROM entries WHERE kind='state' AND sequence>? "
                                        "ORDER BY sequence DESC", (pending["sequence_floor"],)).fetchall()
            backup = next((self.store.decode(row) for row in candidates if self.root/row["source"] in new
                           and json.loads(row["metadata"]).get("container_validation") !=
                               "incomplete_or_invalid_legacy_state"), None)
            if backup:
                source, target = pending["source"], pending["target"]
                temporary = target.with_name(".emuatlas-load-" + uuid.uuid4().hex + ".part")
                try:
                    with source.open("rb") as reader, temporary.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, 1024*1024)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if digest(temporary) != pending["sha256"]:
                        raise ValueError("A cópia de retomada falhou na verificação.")
                    sig = list(signature(temporary))
                    with self.store.connect() as db:
                        db.execute("INSERT INTO loading_cache(path,sha256,signature) VALUES(?,?,?)",
                                   (target.relative_to(self.root).as_posix(), pending["sha256"], json.dumps(sig)))
                    publish_exclusive(temporary, target)
                    self.reserved[target] = (pending["sha256"], sig)
                finally:
                    temporary.unlink(missing_ok=True)
                self.latest_log = ""
                self.send(f"LOAD_STATE_SLOT {pending['slot']}")
                pending.update(phase="load", started=time.monotonic(), backup=backup)
                self.message = "Retomando a gravação…"
            elif age > pending["timeout"]:
                self.load_pending = None
                self.last_load_outcome = "failed"
                self.message = "A cópia do momento atual falhou. A retomada foi cancelada."
                self.revision += 1
        elif age > .6:
            target = str(pending["target"])
            related = [line for line in self.latest_log.splitlines() if target in line]
            if any("[ERROR]" in line or "[ERRO]" in line for line in related):
                backup = pending.get("backup")
                if pending["phase"] == "load" and backup:
                    path = safe_path(self.root, backup["source"])
                    match = re.search(r"\.state(\d*)$", path.name)
                    if match and path.is_file() and digest(path) == backup["sha256"]:
                        self.latest_log = ""
                        slot = int(match[1] or 0)
                        self.send(f"LOAD_STATE_SLOT {slot}")
                        pending.update(phase="rollback", started=time.monotonic(), slot=slot, target=path)
                        self.message = "State recusado. Recuperando o momento anterior…"
                        return
                self.message = "A retomada falhou. Use a versão preservada do momento anterior."
                self.load_pending = None
                self.last_load_outcome = "failed"
                self.revision += 1
            elif (f"LOAD_STATE_SLOT {pending['slot']}" in self.latest_log and any("[State]" in line for line in related)
                  and max(self.latest_log.rfind("GET_STATUS PLAYING"), self.latest_log.rfind("GET_STATUS PAUSED"))
                      > self.latest_log.rfind(target)):
                if pending["phase"] == "rollback":
                    self.last_resume = pending["backup"]["sequence"]
                    self.last_load_outcome = "recovered"
                    self.message = "State recusado · momento anterior recuperado"
                    self.load_pending = None
                    self.revision += 1
                    self.store.clean_loading_cache()
                    self.reserved.clear()
                else:
                    # Refresh the restored frame behind the preview before the
                    # shelf releases its pause and fades out.
                    self.send("FRAMEADVANCE")
                    pending.update(phase="render", render_status=self.status_generation)
                    self.message = "Preparando a imagem da retomada…"
            elif age > pending["timeout"]:
                self.message = "Carregamento em andamento · aguardando o RetroArch"

    def work(self):
        try:
            self.baseline = {p: signature(p) for p in self.paths()}
            self.imports = sorted(self.paths().items(), key=lambda pair: pair[0].stat().st_mtime_ns)
            self.store.session(self.identifier, 0)
            last_save = 0
            while not self.stopping.wait(.25):
                now = time.monotonic()
                if self.child.poll() is not None:
                    break
                self.read_status()
                if self.status == "PLAYING" and self.subject == self.content.stem:
                    self.seconds += min(now - self.last_time, 1)
                self.last_time = now
                self.send("GET_STATUS")
                if now-self.last_scan >= .75:
                    self.import_and_scan()
                    self.last_scan = now
                self.previews()
                self.advance_load()
                if self.battery_pending and now-self.battery_pending[0][1] > 8:
                    self.battery_pending.clear()
                    self.message = "Save interno preservado · core sem confirmação de state acompanhante"
                if self.pause_wanted and self.status == "PLAYING" and now - self.last_pause_command > 1:
                    self.send("PAUSE_TOGGLE")
                    self.paused_by_us = True
                    self.last_pause_command = now
                if not self.pause_wanted and self.paused_by_us and self.status == "PAUSED":
                    self.send("PAUSE_TOGGLE")
                    self.paused_by_us = False
                try:
                    action, value = self.actions.get_nowait()
                    if action == "load":
                        self.load_requested = value
                        self.last_load_outcome = "pending"
                    elif action == "pause":
                        self.pause_wanted = value
                    elif action == "favorite":
                        self.store.annotate(value, favorite=not self.store.get(value)["favorite"])
                        self.revision += 1
                    elif action == "annotate":
                        self.store.annotate(**value)
                        self.revision += 1
                    elif action == "export":
                        directory = self.operation / ("journey-export-" + uuid.uuid4().hex[:8])
                        directory.mkdir()
                        self.store.export_csv(directory / "saves.csv")
                        self.message = "Planilha CSV exportada no histórico da sessão"
                except queue.Empty:
                    pass
                except (ValueError, OSError) as exc:
                    self.message = str(exc)
                    self.revision += 1
                if self.load_requested is not None and not self.battery_pending and not self.preview_pending:
                    sequence = self.load_requested
                    self.load_requested = None
                    try:
                        self.begin_load(sequence)
                    except (ValueError, OSError) as exc:
                        self.message = str(exc)
                        self.last_load_outcome = "failed"
                        self.revision += 1
                if now - last_save > 5:
                    self.store.session(self.identifier, round(self.seconds, 2))
                    last_save = now
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.message = "Histórico indisponível · " + str(exc)
            self.load_pending = self.load_requested = None
            self.last_load_outcome = "failed"
            self.revision += 1
            atomic(self.operation / "save-journey-error.json", json.dumps({"error": str(exc)}).encode())
        finally:
            if self.paused_by_us and self.status == "PAUSED" and self.child and self.child.poll() is None:
                try:
                    self.send("PAUSE_TOGGLE")
                except (OSError, ValueError):
                    pass
            self.store.session(self.identifier, round(self.seconds, 2), "closed")

    def close(self):
        try:
            self.stopping.set()
            if self.thread:
                while self.thread.is_alive():
                    self.thread.join(timeout=.5)
            if self.reader:
                self.reader.join(timeout=1)
            if not self.thread or not self.thread.is_alive():
                try:
                    self.import_and_scan()
                    if self.pending:
                        time.sleep(.55)
                        self.import_and_scan()
                except (OSError, ValueError):
                    pass
            # Only derived loading copies are disposable. Historical payloads persist.
            self.store.clean_loading_cache()
            if self.child and self.child.stdin:
                self.child.stdin.close()
            atomic(self.operation / "save-journey-summary.json", json.dumps({
                "game_id": self.store.game_id, "database": str(self.store.database.relative_to(self.root)),
                "versions": self.store.count(), "session_seconds": round(self.seconds, 2),
                "status": self.status, "message": self.message,
                "errors": self.errors,
                "native_payloads_modified_by_metadata": False,
                "physical_gameplay_verified": False}, ensure_ascii=False, indent=2).encode())
        finally:
            self.lease.close()


def run_unrouted(root, command, env, context):
    """Capture metadata where the KDE input router is unavailable."""
    try:
        from . import romtool
    except ImportError:
        import romtool
    identifier = "save-journey-session-" + time.strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:8]
    directory = romtool.operation_dir(identifier)
    directory.mkdir(mode=0o700)
    manifest = {"operation_id": identifier, "kind": "save_journey_session", "status": "preparing",
                "created_at": romtool.now(), "system_id": context["system_id"],
                "overlay": "requires_KDE_session_router"}
    romtool.atomic_json(directory / "manifest.json", manifest)
    try:
        backend = RetroArchSession(root, context, directory)
        prepared = backend.prepare(command)
    except (OSError, ValueError) as exc:
        manifest.update(status="monitor_unavailable", error=str(exc))
        romtool.atomic_json(directory / "manifest.json", manifest)
        print("[SAVES] Histórico indisponível: " + str(exc), flush=True)
        return subprocess.run(command, env=env, check=False).returncode
    child = None
    try:
        child = subprocess.Popen(prepared, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        backend.attach(child)
        manifest.update(status="running", pid=child.pid)
        romtool.atomic_json(directory / "manifest.json", manifest)
        return child.wait()
    finally:
        # A recoverable exception must preserve the live emulator and its bridge.
        if child is not None:
            while child.poll() is None:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
        backend.close()
        manifest.update(status="completed", closed_at=romtool.now(),
                        returncode=child.returncode if child else None)
        romtool.atomic_json(directory / "manifest.json", manifest)
        romtool.append_event(identifier, "save_journey_session_closed", status="completed",
                             details={"system_id": context["system_id"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--system-id", required=True)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--export-csv", type=Path, required=True)
    args = parser.parse_args()
    store = Store(args.root, args.system_id, args.rom, args.core)
    destination = args.export_csv if args.export_csv.is_absolute() else args.root/args.export_csv
    try:
        relative = destination.relative_to(args.root)
        if not relative.parts or relative.parts[0] != "outputs":
            raise ValueError("Exporte a projeção em outputs/.")
        safe_path(args.root, relative)
        if destination.exists():
            raise ValueError("O destino já existe. Escolha um nome novo.")
    except ValueError as exc:
        parser.error(str(exc))
    store.export_csv(destination)


if __name__ == "__main__":
    main()
