"""Строгие параметры переносимого read-only HTTP API, отдельно от legacy UI поиска."""

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl

MAX_SEQUENCE = 9223372036854775807
PARAMETERS = {"limit", "journalEpoch", "afterSequence", "beforeSequence", "sessionId", "operation", "q", "projectId"}


def scalar_text(value, maximum):
    """Проверяет Unicode scalars без нормализации или замены недопустимых входных данных."""
    if len(value) > maximum or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError("invalid_query")
    return value


def decimal_sequence(value):
    """Разбирает canonical signed-Int64 nonnegative decimal без float и Unicode digits."""
    if len(value) > 19 or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError("invalid_query")
    number = int(value)
    if number > MAX_SEQUENCE:
        raise ValueError("invalid_query")
    return number


def query_pairs(request):
    """Декодирует percent-encoded UTF-8 строго, до потери информации стандартным parser."""
    raw = request.scope.get("query_string", b"").decode("utf-8", errors="strict")
    if re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise ValueError("invalid_query")
    return parse_qsl(raw, keep_blank_values=True, encoding="utf-8", errors="strict")


@dataclass(frozen=True)
class EventQuery:
    """Содержит только проверенные фильтры; HTTP cursor не выбирает authority или data path."""

    limit: int = 100
    journal_epoch: str | None = None
    after: int | None = None
    before: int | None = None
    session_id: str | None = None
    operation: str | None = None
    query: str | None = None
    project_id: str | None = None

    @property
    def direction(self):
        """Определяет обход; исходящий порядок событий всегда остаётся возрастающим."""
        return "forward" if self.after is not None else "backward"

    @property
    def sequence(self):
        """Возвращает явно заданную границу или None для начальной backward страницы."""
        return self.after if self.after is not None else self.before

    @classmethod
    def parse(cls, request):
        """Отклоняет unknown/repeated keys и несогласованные epoch/sequence pairs с HTTP 400."""
        values = {}
        for key, value in query_pairs(request):
            if key not in PARAMETERS or key in values:
                raise ValueError("invalid_query")
            values[key] = value
        limit = values.get("limit", "100")
        if len(limit) > 3 or not re.fullmatch(r"[1-9][0-9]*", limit) or int(limit) > 100:
            raise ValueError("invalid_query")
        has_after, has_before = "afterSequence" in values, "beforeSequence" in values
        has_epoch = "journalEpoch" in values
        if has_after and has_before or has_epoch != (has_after or has_before):
            raise ValueError("invalid_query")
        epoch = scalar_text(values["journalEpoch"], 128) if has_epoch else None
        if has_epoch and not epoch:
            raise ValueError("invalid_query")
        filters = {}
        for external, internal, maximum in (("sessionId", "session_id", 128), ("operation", "operation", 128),
                                             ("projectId", "project_id", 128), ("q", "query", 512)):
            if external in values:
                filters[internal] = scalar_text(values[external], maximum) or None
        return cls(limit=int(limit), journal_epoch=epoch,
                   after=decimal_sequence(values["afterSequence"]) if has_after else None,
                   before=decimal_sequence(values["beforeSequence"]) if has_before else None, **filters)
