"""Nonblocking logging admission and one batched durable writer; independent of activity/UI."""

from collections import Counter, deque
import logging
import threading
import time

from .disk import SegmentJournal
from .events import estimate, fit_event, from_record
from .settings import TechnicalSettings, load_settings


class TechnicalJournal:
    """Owns a quota-limited journal and a bounded pending queue, with observable losses."""

    def __init__(self, data_dir, settings=None):
        """Opens only this journal; invalid settings/storage degrade logging without failing startup."""
        self.settings = settings or TechnicalSettings()
        self.disk = None
        self.error = None
        self.queue = deque()
        self.queue_bytes = 0
        self.dropped = Counter()
        self.lock = threading.Lock()
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self.worker = None
        self.inflight = 0
        self.handlers = []
        self.previous_root_level = None
        try:
            self.settings = (settings or load_settings(data_dir)).validate()
            self.disk = SegmentJournal(data_dir / "technical-logs", self.settings)
        except Exception:
            self.error = "configuration_or_storage_unavailable"

    def submit(self, record):
        """Captures bounded primitives only; never waits for queue contention or disk I/O."""
        if self.stopping.is_set() or self.disk is None or self.error:
            self.dropped["storage_unavailable"] += 1
            return False
        try:
            event = fit_event(from_record(record), self.settings.MaxEventBytes)
            if event is None:
                self.dropped["oversize"] += 1
                return False
            size = estimate(event) + 256
        except Exception:
            self.dropped["invalid_event"] += 1
            return False
        if not self.lock.acquire(blocking=False):
            self.dropped["full_queue"] += 1
            return False
        try:
            if len(self.queue) >= self.settings.QueueMaxItems or self.queue_bytes + size > self.settings.QueueMaxBytes:
                self.dropped["full_queue"] += 1
                return False
            self.queue.append((event, size))
            self.queue_bytes += size
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="ag-tasks-technical-log-writer", daemon=True)
                self.worker.start()
            self.wakeup.set()
            return True
        finally:
            self.lock.release()

    def _take_batch(self):
        """Drains by byte bound or deadline from the first event; no timer-driven idle writes."""
        batch, size = [], 0
        deadline = time.monotonic() + self.settings.BatchMaxDelayMs / 1000
        while True:
            with self.lock:
                while self.queue:
                    event, amount = self.queue[0]
                    if batch and size + amount > self.settings.BatchMaxBytes:
                        return batch
                    self.queue.popleft()
                    self.queue_bytes -= amount
                    size += amount
                    batch.append(event)
                self.wakeup.clear()
            remaining = deadline - time.monotonic()
            if self.stopping.is_set() or remaining <= 0:
                return batch
            self.wakeup.wait(remaining)

    def _run(self):
        """Commits batches on one worker; faults stop admission and never recursively log."""
        while True:
            self.wakeup.wait()
            with self.lock:
                if not self.queue:
                    if self.stopping.is_set():
                        return
                    self.wakeup.clear()
                    continue
                self.inflight = 1
            batch = self._take_batch()
            try:
                self.disk.append(batch)
            except Exception:
                self.error = "storage_unavailable"
                with self.lock:
                    self.dropped["storage_unavailable"] += len(batch) + len(self.queue)
                    self.queue.clear()
                    self.queue_bytes = 0
            finally:
                with self.lock:
                    self.inflight = 0
                    if self.queue or self.stopping.is_set():
                        self.wakeup.set()

    def status(self):
        """Returns one committed window plus process-local queue/drop health, without storage paths."""
        if self.disk is None:
            raise OSError("Journal unavailable")
        with self.disk.lock:
            oldest, latest = self.disk.bounds()
            epoch = self.disk.epoch
            return {"schemaVersion": "technical-logs-http/v1", "eventSchemaVersion": "technical-log/v1",
                    "application": 'ag-tasks', "journalEpoch": epoch, "oldestSequence": str(oldest),
                    "latestSequence": str(latest), "cursor": {"journalEpoch": epoch, "sequence": str(latest)},
                    "maxSizeBytes": self.settings.max_size_bytes, "usedBytes": self.disk.used_bytes(),
                    "limits": {"maxPageEvents": 100, "maxPageBytes": 262144},
                    "health": {"status": "degraded" if self.error else "ok", "errorCode": self.error,
                               "queueItems": len(self.queue), "queueBytes": self.queue_bytes,
                               "droppedTotal": sum(self.dropped.values())}}

    def install(self):
        """Bridges existing stdlib loggers, including uvicorn's nonpropagating error/access channels."""
        handler = TechnicalHandler(self)
        root = logging.getLogger()
        self.previous_root_level = root.level
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        for logger in (root, logging.getLogger("uvicorn"), logging.getLogger("uvicorn.access")):
            if logger is root or not logger.propagate:
                logger.addHandler(handler)
                self.handlers.append((logger, handler))

    def flush(self, timeout=2):
        """Waits only for bounded drain; false includes storage failure or an unfinished batch."""
        self.wakeup.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if not self.queue and not self.inflight:
                    return self.error is None
            time.sleep(.005)
        return False

    def close(self):
        """Detaches capture before draining; an unresponsive worker retains its own file lock."""
        for logger, handler in self.handlers:
            logger.removeHandler(handler)
        if self.previous_root_level is not None and logging.getLogger().level == logging.INFO:
            logging.getLogger().setLevel(self.previous_root_level)
        self.handlers.clear()
        self.stopping.set()
        self.wakeup.set()
        if self.worker:
            self.worker.join(self.settings.ShutdownDrainTimeoutMs / 1000)
            if self.worker.is_alive():
                self.error = "shutdown_timeout"
                return
        if self.disk:
            self.disk.close()


class TechnicalHandler(logging.Handler):
    """A logging bridge with no Handler lock, console fallback or recursive error path."""

    def __init__(self, journal):
        """Keeps only the bounded service; LogRecords are never retained by the handler."""
        super().__init__(logging.INFO)
        self.journal = journal

    def handle(self, record):
        """Bypasses logging.Handler's blocking lock; all admission is no-wait inside the service."""
        self.emit(record)
        return True

    def emit(self, record):
        """Drops technical-read access noise and isolates malformed third-party records."""
        try:
            if record.name == "uvicorn.access" and type(record.args) is tuple and len(record.args) >= 3:
                path = record.args[2]
                read_paths = ("/api/technical-logs/", "/api/diagnostics/", "/api/agent-activity/")
                if record.args[1] == "GET" and type(path) is str and path.startswith(read_paths):
                    return
            if record.name.startswith("service_telemetry.technical_logs"):
                return
            self.journal.submit(record)
        except Exception:
            self.journal.dropped["invalid_event"] += 1
