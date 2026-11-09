"""Настройки отдельного кольца технических логов, загружаемые при старте."""

from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class TechnicalSettings:
    """Квота в байтах включает segments, manifest и служебные файлы, без срока хранения."""

    MaxSizeBytes: int = 100_000_000
    MaxEventBytes: int = 16384
    QueueMaxItems: int = 1024
    QueueMaxBytes: int = 1048576
    BatchMaxBytes: int = 65536
    BatchMaxDelayMs: int = 250
    ShutdownDrainTimeoutMs: int = 2000

    @property
    def max_size_bytes(self):
        """Возвращает независимую общую квоту технического кольца."""
        return self.MaxSizeBytes

    def validate(self):
        """Не допускает unlimited, отрицательные лимиты или batch меньше одного события."""
        if any(type(value) is not int for value in asdict(self).values()):
            raise ValueError("TechnicalLogs settings must be integers")
        if not 100_000 <= self.MaxSizeBytes <= 100_000_000_000:
            raise ValueError("TechnicalLogs quota out of range")
        if not 1024 <= self.MaxEventBytes <= 16384:
            raise ValueError("TechnicalLogs event size out of range")
        if not self.MaxEventBytes <= self.BatchMaxBytes <= 65536:
            raise ValueError("TechnicalLogs batch must fit one event")
        if not self.BatchMaxBytes <= self.QueueMaxBytes <= 64 * 1024 * 1024 or not 1 <= self.QueueMaxItems <= 65536:
            raise ValueError("TechnicalLogs queue out of range")
        if not 1 <= self.BatchMaxDelayMs <= 1000 or not 1 <= self.ShutdownDrainTimeoutMs <= 10000:
            raise ValueError("TechnicalLogs timing out of range")
        return self


def load_settings(data_dir):
    """Читает namespace TechnicalLogs; изменение файла вступает в силу после рестарта."""
    path = data_dir / "technical-logs-settings.json"
    if not path.exists():
        path.write_text(json.dumps({"TechnicalLogs": asdict(TechnicalSettings())}, indent=2), encoding="utf-8")
    if path.stat().st_size > 16384:
        raise ValueError("TechnicalLogs settings exceed 16 KiB")
    return TechnicalSettings(**json.loads(path.read_text(encoding="utf-8"))["TechnicalLogs"]).validate()
