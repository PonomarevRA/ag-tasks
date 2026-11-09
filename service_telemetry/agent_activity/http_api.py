"""Переносимые versioned HTTP status/events без изменения UI, WebSocket или MCP."""

import ipaddress
import json

from fastapi import Request
from fastapi.responses import JSONResponse

from .http_query import EventQuery, query_pairs

SCHEMA = "agent-activity-http/v1"
MAX_PAGE_EVENTS = 100
MAX_PAGE_BYTES = 262144
ERRORS = {400: ("invalid_query", "Invalid query parameters."),
          403: ("forbidden", "Access forbidden."),
          409: ("cursor_expired", "Cursor has expired."),
          503: ("journal_unavailable", "Journal is unavailable.")}


def permitted_request(request):
    """Принимает loopback HTTP clients без Origin и browser только с точным same Origin."""
    try:
        peer = ipaddress.ip_address(request.client.host)
        local_peer = peer.is_loopback or bool(getattr(peer, "ipv4_mapped", None) and peer.ipv4_mapped.is_loopback)
        hostname = request.url.hostname
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
        return (local_peer and hostname in {"127.0.0.1", "localhost", "::1"}
                and (origin is None or origin == expected)
                and request.headers.get("sec-fetch-site") != "cross-site")
    except (AttributeError, ValueError):
        return False


def http_response(value, status=200):
    """Возвращает JSON без cache даже при использовании feature router без глобальной boundary."""
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def error_response(status, reset_cursor=None):
    """Возвращает fixed safe error; exception/input/path никогда не отражаются в ответе."""
    code, message = ERRORS[status]
    return http_response({"schemaVersion": SCHEMA, "application": 'ag-tasks',
                          "error": {"code": code, "message": message, "retryable": status == 503},
                          "resetCursor": reset_cursor if status == 409 else None}, status)


def normalized_health(status):
    """Отделяет известные process counters от отсутствующих gauges, сохраняя unknown как null."""
    health = status.get("health") or {}
    dropped = health.get("dropped")
    if isinstance(dropped, dict):
        counters = list(dropped.values())
        dropped = sum(counters) if all(type(value) is int and value >= 0 for value in counters) else None
    if type(dropped) is not int or dropped < 0:
        dropped = None
    result = {"status": "ok" if health.get("status") in {"ok", "healthy"} else "degraded",
              "droppedTotal": dropped, "errorCode": health.get("errorCode")}
    for field in ("queueItems", "queueBytes"):
        value = health.get(field)
        result[field] = value if type(value) is int and value >= 0 else None
    return result


def versioned_status(status):
    """Проецирует только общий status контракт, не экспортируя настройки диска или storage paths."""
    return {"schemaVersion": SCHEMA, "application": 'ag-tasks', "eventSchemaVersion": "agent-activity/v1",
            "journalEpoch": status["journalEpoch"], "oldestSequence": status["oldestSequence"],
            "latestSequence": status["latestSequence"], "maxSizeBytes": status["maxSizeBytes"],
            "usedBytes": status["usedBytes"], "cursor": status["cursor"],
            "limits": {"maxPageEvents": MAX_PAGE_EVENTS, "maxPageBytes": MAX_PAGE_BYTES},
            "health": normalized_health(status)}


def cursor_is_current(query, status):
    """Проверяет границу по одному committed window; rolled-out prefix всегда даёт 409."""
    if query.sequence is None:
        return True
    return (query.journal_epoch == status["journalEpoch"]
            and int(status["oldestSequence"]) - 1 <= query.sequence <= int(status["latestSequence"]))


def event_page(query, rows, status, store, project_event):
    """Обрезает только непрерывный край страницы, сохраняя эксклюзивный continuation без holes."""
    forward = query.direction == "forward"
    has_more = len(rows) > query.limit
    candidates = rows[:query.limit] if forward else list(reversed(rows[-query.limit:]))
    page = {"schemaVersion": SCHEMA, "application": 'ag-tasks', "direction": query.direction, "events": [],
            "nextCursor": dict(status["cursor"]), "highWatermark": status["cursor"],
            "oldestSequence": status["oldestSequence"], "hasMore": has_more, "truncated": False,
            "health": normalized_health(status)}
    # Cursor digit lengths and boolean values can grow after rows are selected; reserve their small overhead.
    size = len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 128
    for event in candidates:
        value = project_event(event, store)
        row_size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) + 1
        if size + row_size > MAX_PAGE_BYTES:
            if not page["events"]:
                raise ValueError("Oversized projected activity event")
            page["hasMore"] = page["truncated"] = True
            break
        page["events"].append(value)
        size += row_size
    if not forward:
        page["events"].reverse()
    if page["hasMore"]:
        boundary = page["events"][-1 if forward else 0]["sequence"]
    else:
        boundary = status["latestSequence"] if forward else str(int(status["oldestSequence"]) - 1)
    page["nextCursor"] = {"journalEpoch": status["journalEpoch"], "sequence": boundary}
    return page


def register_http_activity_routes(app, journal, store, project_event):
    """Добавляет только versioned read-only routes рядом с неизменными legacy endpoints."""

    @app.get("/api/agent-activity/v1/status")
    def activity_http_status(request: Request):
        """Возвращает общий status; access/query/storage ошибки имеют безопасный единый envelope."""
        if not permitted_request(request):
            return error_response(403)
        try:
            if query_pairs(request):
                return error_response(400)
        except (ValueError, UnicodeError):
            return error_response(400)
        if journal.disk is None:
            return error_response(503)
        try:
            return http_response(versioned_status(journal.status()))
        except (OSError, ValueError, TypeError, KeyError):
            return error_response(503)

    @app.get("/api/agent-activity/v1/events")
    def activity_http_events(request: Request):
        """Читает coherent committed window и continuation; запрос не добавляет события в журнал."""
        if not permitted_request(request):
            return error_response(403)
        try:
            query = EventQuery.parse(request)
        except (ValueError, UnicodeError):
            return error_response(400)
        if journal.disk is None:
            return error_response(503)
        try:
            with journal.disk.lock:
                status = journal.status()
                if not cursor_is_current(query, status):
                    return error_response(409, status["cursor"])
                rows = journal.disk.read(after=query.after, before=query.before, limit=query.limit + 1,
                                         project_id=query.project_id, session_id=query.session_id,
                                         operation=query.operation, query=query.query, literal_query=True)
                return http_response(event_page(query, rows, status, store, project_event))
        except (OSError, ValueError, TypeError, KeyError):
            return error_response(503)
