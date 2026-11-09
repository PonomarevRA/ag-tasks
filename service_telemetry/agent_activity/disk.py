"""Единственный владелец сегментированного JSONL с общей квотой и durable commit."""

from collections import deque
import json
import os
from pathlib import Path
from threading import RLock
from uuid import uuid4
import zlib

from ..process_lock import ProcessLock
from .events import utc_now


class SegmentJournal:
    """Хранит только ограниченные сегменты; читатели не удерживают открытые файлы."""

    def __init__(self, directory: Path, settings):
        """Восстанавливает порядок и удерживает process lock до закрытия журнала."""
        self.directory = directory
        self.settings = settings
        self.lock = RLock()
        self.segments = deque()
        self.sequence = 0
        self.epoch = uuid4().hex
        self.reserve = 8192
        self.recovery_reset = False
        self.segment_limit = min(1_000_000, max(settings.BatchMaxBytes, settings.max_size_bytes // 50))
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or any(path.is_symlink() for path in directory.iterdir()):
            raise OSError("Journal symlinks are forbidden")
        self.process_lock = ProcessLock(directory / "writer.lock")
        self.process_lock.__enter__()
        try:
            try:
                self._recover()
            except (ValueError, TypeError, KeyError, UnicodeError):
                # Corrupt bytes cannot prove replay continuity; only this journal is reset.
                for path in directory.glob("segment-*.jsonl"):
                    path.unlink()
                self.segments.clear()
                self.epoch, self.sequence = uuid4().hex, 0
                self.recovery_reset = True
                self._manifest()
        except BaseException:
            self.process_lock.__exit__(None, None, None)
            raise

    def used_bytes(self):
        """Считает все обычные файлы каталога, включая lock/manifest/temp."""
        return sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())

    def _sync_directory(self):
        """Закрепляет rename/delete на POSIX; Windows не предоставляет такой directory fd."""
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _manifest(self):
        """Атомарно закрепляет epoch и sequence внутри зарезервированного бюджета."""
        temporary = self.directory / "manifest.tmp"
        raw = json.dumps({"schemaVersion": 1, "journalEpoch": self.epoch,
                          "sequence": str(self.sequence)}, separators=(",", ":")).encode()
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.directory / "manifest.json")
        self._sync_directory()

    def _recover(self):
        """Сканирует только квотированный журнал; частичный хвост удаляет без потери commit."""
        manifest = self.directory / "manifest.json"
        if manifest.exists():
            if manifest.stat().st_size > 2048:
                raise ValueError("Oversized journal manifest")
            value = json.loads(manifest.read_bytes())
            self.epoch = value["journalEpoch"]
            self.sequence = int(value["sequence"])
        previous = 0
        paths = sorted(self.directory.glob("segment-*.jsonl"))
        for path in paths:
            ids = set()
            first, last, valid_length = None, None, 0
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n") and path == paths[-1]:
                        break
                    if len(line) > 8192:
                        raise ValueError("Oversized journal record")
                    event = json.loads(line)
                    checksum = event.pop("_crc32", None)
                    canonical = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                    if checksum != f"{zlib.crc32(canonical):08x}":
                        raise ValueError("Journal checksum mismatch")
                    sequence = int(event["sequence"])
                    if event["journalEpoch"] != self.epoch or sequence <= previous or (previous and sequence != previous + 1):
                        raise ValueError("Journal order is invalid")
                    first = sequence if first is None else first
                    last = previous = sequence
                    valid_length += len(line)
                    ids.add(event["eventId"])
            if valid_length != path.stat().st_size:
                with path.open("r+b") as handle:
                    handle.truncate(valid_length)
                    handle.flush()
                    os.fsync(handle.fileno())
            if first is not None:
                self.segments.append({"path": path, "first": first, "last": last, "ids": ids})
            else:
                path.unlink()
        # A manifest ahead of durable records means continuity cannot be proven.
        if self.segments and self.sequence > self.segments[-1]["last"]:
            self.clear()
            return
        self.sequence = max(self.sequence, previous)
        self._trim(0)
        self._manifest()

    def _trim(self, incoming):
        """Освобождает целые старые сегменты до записи; denied delete останавливает рост."""
        maximum = self.settings.max_size_bytes - self.reserve
        while self.used_bytes() + incoming > maximum and self.segments:
            oldest = self.segments[0]
            oldest["path"].unlink()
            self.segments.popleft()
        if self.used_bytes() + incoming > maximum:
            raise OSError("Journal quota cannot be reclaimed")

    def append(self, events):
        """Назначает sequence и публикует batch только после file+manifest fsync."""
        with self.lock:
            encoded, committed = [], []
            ids = set()
            for event in events:
                identifier = event["eventId"]
                if identifier in ids or any(identifier in segment["ids"] for segment in self.segments):
                    continue
                value = {**event, "recordedAt": utc_now(), "journalEpoch": self.epoch,
                         "sequence": str(self.sequence + len(committed) + 1)}
                canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                value["_crc32"] = f"{zlib.crc32(canonical):08x}"
                raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
                if len(raw) > self.settings.MaxEventBytes:
                    raise ValueError("Activity event exceeds configured maximum")
                encoded.append(raw)
                committed.append(value)
                ids.add(identifier)
            if not committed:
                return []
            data = b"".join(encoded)
            self._trim(len(data))
            current = self.segments[-1] if self.segments else None
            if current is None or current["path"].stat().st_size + len(data) > self.segment_limit:
                path = self.directory / f"segment-{int(committed[0]['sequence']):020d}.jsonl"
                current = {"path": path, "first": int(committed[0]["sequence"]), "last": 0, "ids": set()}
                self.segments.append(current)
            with current["path"].open("ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self.sequence = int(committed[-1]["sequence"])
            current["last"] = self.sequence
            current["ids"].update(ids)
            self._manifest()
            return committed

    def bounds(self):
        """Возвращает включительные границы сохранённого окна и последний durable cursor."""
        oldest = self.segments[0]["first"] if self.segments else self.sequence + 1
        return oldest, self.sequence

    def read(self, *, after=None, before=None, limit=100, project_id=None, session_id=None, operation=None, query=None, literal_query=False):
        """Читает страницу в порядке commit; reverse scan истории ограничен размером журнала."""
        with self.lock:
            result = []
            segments = self.segments if after is not None else reversed(self.segments)
            for segment in segments:
                if after is not None and segment["last"] <= after:
                    continue
                if before is not None and segment["first"] >= before:
                    continue
                with segment["path"].open("rb") as handle:
                    lines = handle.readlines()
                rows = lines if after is not None else reversed(lines)
                for line in rows:
                    if not line.endswith(b"\n"):
                        continue
                    if len(line) > 8192:
                        raise ValueError("Oversized journal record")
                    event = json.loads(line)
                    checksum = event.pop("_crc32", None)
                    canonical = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
                    if checksum != f"{zlib.crc32(canonical):08x}":
                        raise ValueError("Journal checksum mismatch")
                    sequence = int(event["sequence"])
                    # Bytes from an unsuccessful file fsync are not a persisted event.
                    if sequence > self.sequence:
                        continue
                    if after is not None and sequence <= after or before is not None and sequence >= before:
                        continue
                    target = event.get("target") or {}
                    if project_id and target.get("projectId") != project_id:
                        continue
                    if session_id and event.get("sessionId") != session_id:
                        continue
                    if operation and event.get("operation") != operation:
                        continue
                    if query and not _matches_query(event, query, literal_query):
                        continue
                    result.append(event)
                    if len(result) >= limit:
                        return result if after is not None else list(reversed(result))
            return result if after is not None else list(reversed(result))

    def clear(self):
        """Удаляет только activity segments, меняет epoch и закрепляет сброс курсора."""
        with self.lock:
            for segment in list(self.segments):
                segment["path"].unlink()
                self.segments.popleft()
            self.epoch, self.sequence = uuid4().hex, 0
            self._manifest()

    def close(self):
        """Освобождает process lock после остановки единственного writer."""
        self.process_lock.__exit__(None, None, None)


def _matches_query(event, query, literal):
    """Сохраняет legacy casefold preview; HTTP v1 ищет literal в четырёх публичных полях."""
    preview = (event.get("query") or {}).get("preview") or ""
    if not literal:
        return query.casefold() in preview.casefold()
    fields = (event.get("operation"), preview, (event.get("request") or {}).get("summary"),
              (event.get("response") or {}).get("summary"))
    for value in fields:
        if isinstance(value, str) and query in value:
            return True
    return False
