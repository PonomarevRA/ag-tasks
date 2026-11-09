"""Настройки сервиса activity journal, читаемые один раз при запуске."""

from dataclasses import dataclass, asdict
import json
from pathlib import Path


@dataclass(frozen=True)
class ActivitySettings:
    """Задаёт десятичную дисковую квоту и независимые пределы очереди и batch."""

    MaxSizeMb: int = 50
    MaxEventBytes: int = 8192
    QueueMaxItems: int = 1024
    QueueMaxBytes: int = 1048576
    EnqueueTimeoutMs: int = 0
    BatchMaxBytes: int = 65536
    BatchMaxDelayMs: int = 250
    Durability: str = "durable-batch"
    ShutdownDrainTimeoutMs: int = 2000

    @property
    def max_size_bytes(self):
        """Возвращает общий предел обычных файлов в байтах, не размер одного сегмента."""
        return self.MaxSizeMb * 1_000_000

    def validate(self):
        """Отклоняет несовместимые значения; ноль никогда не включает unlimited storage."""
        integers = asdict(self)
        integers.pop("Durability")
        if any(type(value) is not int for value in integers.values()):
            raise ValueError("AgentActivity values must be integers")
        if not 1 <= self.MaxSizeMb <= 100_000:
            raise ValueError("AgentActivity.MaxSizeMb must be 1..100000")
        if not 1024 <= self.MaxEventBytes <= 8192:
            raise ValueError("AgentActivity.MaxEventBytes must be 1024..8192")
        if not self.MaxEventBytes <= self.BatchMaxBytes <= 65536:
            raise ValueError("AgentActivity batch must fit an event and be <=65536")
        if not 1 <= self.QueueMaxItems <= 65536 or not self.BatchMaxBytes <= self.QueueMaxBytes <= 64 * 1024 * 1024:
            raise ValueError("AgentActivity queue must fit a batch")
        if self.EnqueueTimeoutMs != 0 or self.Durability != "durable-batch":
            raise ValueError("AgentActivity requires no-wait enqueue and durable-batch")
        if not 1 <= self.BatchMaxDelayMs <= 1000 or not 1 <= self.ShutdownDrainTimeoutMs <= 10000:
            raise ValueError("AgentActivity delay/shutdown out of range")
        return self


def load_settings(data_dir: Path):
    """Читает namespace AgentActivity; файл сервиса доступен для изменения перед рестартом."""
    path = data_dir / "agent-activity-settings.json"
    if not path.exists():
        path.write_text(json.dumps({"AgentActivity": asdict(ActivitySettings())}, indent=2), encoding="utf-8")
    if path.stat().st_size > 16384:
        raise ValueError("AgentActivity settings exceed 16 KiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    settings = ActivitySettings(**value["AgentActivity"])
    return settings.validate()
