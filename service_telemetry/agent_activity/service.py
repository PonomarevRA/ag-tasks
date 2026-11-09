"""No-wait producer queue и фоновый durable writer, независимые от UI и log level."""

from collections import Counter, deque
import asyncio
import threading
import time

from .disk import SegmentJournal
from .settings import ActivitySettings, load_settings


class ActivityJournal:
    """Изолирует ошибки истории от основной операции; вся очередь ограничена bytes/items."""

    def __init__(self, data_dir, settings=None):
        """Загружает настройки сервиса и открывает журнал; ошибки видны в status."""
        self.settings = settings or ActivitySettings()
        self.disk = None
        self.error = None
        self.dropped = Counter()
        self.queue = deque()
        self.queue_bytes = 0
        self.queue_lock = threading.Lock()
        self.wakeup = threading.Event()
        self.stopping = threading.Event()
        self.worker = None
        self.inflight = 0
        self.generation = 0
        self.watchers = set()
        self.watcher_lock = threading.Lock()
        self.accepted = self.committed = self.batches = 0
        try:
            self.settings = (settings or load_settings(data_dir)).validate()
            self.disk = SegmentJournal(data_dir / "agent-activity", self.settings)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            self.error = "configuration_or_storage_unavailable"

    def submit(self, event):
        """Копирует bounded scalar snapshot без JSON и I/O; full/lock contention не ждут."""
        if self.stopping.is_set() or self.disk is None or self.error:
            self.dropped["storage_unavailable"] += 1
            return False
        # Producers only create the documented flat DTO and two small nested objects.
        try:
            copied = self._snapshot(event)
            estimate = self._estimate(copied)
        except (ValueError, TypeError, UnicodeError):
            self.dropped["invalid_event"] += 1
            return False
        if estimate > self.settings.MaxEventBytes:
            self.dropped["oversize"] += 1
            return False
        if not self.queue_lock.acquire(blocking=False):
            self.dropped["full_queue"] += 1
            return False
        try:
            if len(self.queue) >= self.settings.QueueMaxItems or self.queue_bytes + estimate > self.settings.QueueMaxBytes:
                self.dropped["full_queue"] += 1
                return False
            self.queue.append((copied, estimate, self.generation))
            self.queue_bytes += estimate
            self.accepted += 1
            if self.worker is None:
                self.worker = threading.Thread(target=self._run, name="ag-tasks-activity-writer", daemon=True)
                self.worker.start()
            self.wakeup.set()
            return True
        finally:
            self.queue_lock.release()

    @staticmethod
    def _snapshot(event):
        """Копирует только scalar allowlist; чужой объект или граф никогда не удерживается очередью."""
        import math
        allowed = {"schemaVersion", "eventId", "occurredAt", "application", "source", "clientKind",
                   "sessionId", "agentId", "identityQuality", "operationId", "parentOperationId",
                   "operation", "phase", "outcome", "durationMs", "resultCount", "errorCode"}
        if not isinstance(event, dict) or not {"eventId", "sessionId", "operationId", "phase"} <= event.keys():
            raise ValueError("invalid event")
        copied = {}
        for key in allowed:
            value = event.get(key)
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("non-scalar field")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite field")
            if isinstance(value, str) and len(value) > 512:
                raise ValueError("unbounded field")
            if key in event:
                copied[key] = value
        for key, fields in (("target", {"kind", "projectId", "nodeId", "snapshotId", "label", "status"}),
                            ("query", {"mode", "preview", "truncated"})):
            nested = event.get(key) or {}
            if not isinstance(nested, dict):
                raise ValueError("invalid nested field")
            copied[key] = {}
            for name in fields:
                value = nested.get(name)
                if value is not None and not isinstance(value, (str, bool)):
                    raise ValueError("non-scalar nested field")
                if isinstance(value, str) and len(value) > 512:
                    raise ValueError("unbounded nested field")
                if name in nested:
                    copied[key][name] = value
        for key in ("request", "response"):
            nested = event.get(key)
            if nested is None:
                continue
            if not isinstance(nested, dict):
                raise ValueError("invalid summary")
            copied[key] = {}
            for name in ("tool", "summary", "resultCount", "truncated", "errorCode"):
                value = nested.get(name)
                if value is not None and not isinstance(value, (str, int, bool)):
                    raise ValueError("invalid summary field")
                if isinstance(value, str) and len(value) > 512:
                    raise ValueError("unbounded summary field")
                if name in nested:
                    copied[key][name] = value
            items = nested.get("items", [])
            if not isinstance(items, list) or len(items) > 3:
                raise ValueError("unbounded summary items")
            if items:
                copied[key]["items"] = []
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("invalid summary item")
                    projected = {}
                    for name in ("label", "file", "nodeId"):
                        value = item.get(name)
                        if value is not None:
                            if not isinstance(value, str) or len(value) > 128:
                                raise ValueError("invalid summary item field")
                            projected[name] = value
                    copied[key]["items"].append(projected)
        return copied

    @staticmethod
    def _estimate(event):
        """Ограничивает payload с запасом на JSON escaping и добавляемый envelope."""
        # Reserve for writer-assigned epoch, sequence, recordedAt and integrity checksum.
        amount = 224
        pending = [event]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                amount += 2 + max(0, len(value) - 1)
                for key, item in value.items():
                    amount += len(key) + 3
                    pending.append(item)
            elif isinstance(value, list):
                amount += 2 + max(0, len(value) - 1)
                pending.extend(value)
            elif isinstance(value, str):
                amount += 2 + len(value.encode("utf-8"))
                amount += sum(5 for char in value if char in {'"', "\\"} or ord(char) < 32)
            elif value is None:
                amount += 4
            elif isinstance(value, bool):
                amount += 5
            else:
                amount += len(str(value))
        return amount

    def _take_batch(self):
        """Собирает batch до max bytes либо deadline от первого события, без idle writes."""
        batch, size = [], 0
        deadline = time.monotonic() + self.settings.BatchMaxDelayMs / 1000
        while True:
            with self.queue_lock:
                while self.queue:
                    item = self.queue[0]
                    if batch and size + item[1] > self.settings.BatchMaxBytes:
                        return batch
                    self.queue.popleft()
                    self.queue_bytes -= item[1]
                    size += item[1]
                    batch.append(item)
                self.wakeup.clear()
            remaining = deadline - time.monotonic()
            if self.stopping.is_set() or remaining <= 0:
                return batch
            self.wakeup.wait(remaining)

    def _run(self):
        """Единственный writer закрепляет batch и будит live consumers после commit."""
        while True:
            self.wakeup.wait()
            with self.queue_lock:
                if not self.queue:
                    if self.stopping.is_set():
                        return
                    self.wakeup.clear()
                    continue
                self.inflight = 1
            batch = self._take_batch()
            try:
                with self.disk.lock:
                    events = [event for event, _, generation in batch if generation == self.generation]
                    committed = self.disk.append(events)
                self.committed += len(committed)
                self.batches += bool(committed)
                self._notify()
            except (ValueError, TypeError, KeyError):
                self.dropped["invalid_event"] += len(batch)
            except OSError:
                self.error = "storage_unavailable"
                with self.queue_lock:
                    self.dropped["storage_unavailable"] += len(batch) + len(self.queue)
                    self.queue.clear()
                    self.queue_bytes = 0
            finally:
                with self.queue_lock:
                    self.inflight = 0
                    if self.queue or self.stopping.is_set():
                        self.wakeup.set()

    def subscribe(self):
        """Регистрирует coalesced wakeup; события остаются в journal, не в network queue."""
        subscription = (asyncio.get_running_loop(), asyncio.Event())
        with self.watcher_lock:
            self.watchers.add(subscription)
        return subscription

    def unsubscribe(self, subscription):
        """Освобождает notifier закрывшегося browser stream."""
        with self.watcher_lock:
            self.watchers.discard(subscription)

    def _notify(self):
        """Планирует один флаг обновления на подписчика, без диска и socket I/O под lock."""
        with self.watcher_lock:
            for loop, event in self.watchers:
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(event.set)
                    except RuntimeError:
                        # A browser event loop may close between the state check and scheduling.
                        pass

    def status(self):
        """Возвращает effective quota, durable cursor и bounded diagnostic counters."""
        epoch, oldest, latest, used = None, 1, 0, 0
        if self.disk:
            with self.disk.lock:
                epoch = self.disk.epoch
                oldest, latest = self.disk.bounds()
                used = self.disk.used_bytes()
        return {"schemaVersion": "agent-activity/v1", "journalEpoch": epoch,
                "maxSizeBytes": self.settings.max_size_bytes if self.disk else None,
                "usedBytes": used, "oldestSequence": str(oldest), "latestSequence": str(latest),
                "cursor": {"journalEpoch": epoch, "sequence": str(latest)},
                "health": {"status": "degraded" if self.error else "ok", "errorCode": self.error,
                           "dropped": dict(self.dropped), "queueItems": len(self.queue),
                           "queueBytes": self.queue_bytes, "accepted": self.accepted,
                           "committed": self.committed, "batchCount": self.batches,
                           "recoveryReset": bool(self.disk and self.disk.recovery_reset)}}

    def clear(self):
        """Сбрасывает pending и persisted историю атомарно относительно writer generation."""
        if self.disk is None:
            return self.status()
        with self.disk.lock:
            with self.queue_lock:
                self.generation += 1
                self.queue.clear()
                self.queue_bytes = 0
            try:
                self.disk.clear()
                self.error = None
            except OSError:
                self.error = "storage_unavailable"
                raise
        self._notify()
        return self.status()

    def flush(self, timeout=2):
        """Ждёт только bounded drain; False означает неполный drain, не durable success."""
        self.wakeup.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.queue_lock:
                if not self.queue and not self.inflight:
                    return not self.error
            time.sleep(.005)
        return False

    def close(self):
        """Останавливает admission и ждёт drain до настроенного shutdown deadline."""
        self.stopping.set()
        self.wakeup.set()
        if self.worker:
            self.worker.join(self.settings.ShutdownDrainTimeoutMs / 1000)
            if self.worker.is_alive():
                self.error = "shutdown_timeout"
                return
        if self.disk:
            self.disk.close()
