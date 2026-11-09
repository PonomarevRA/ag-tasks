"""Ограниченные снимки полей операции без хранения request/response graph."""

from datetime import datetime, timezone
import re
from urllib.parse import parse_qs, urlsplit, unquote
from uuid import uuid4

SCHEMA = "agent-activity/v1"


def utc_now():
    """Возвращает UTC timestamp с явной временной зоной."""
    return datetime.now(timezone.utc).isoformat()


def text(value, maximum=128):
    """Копирует только ограниченный текст; управляющие символы не проходят в UI."""
    if not isinstance(value, str):
        return None
    return "".join(char for char in value[:maximum] if ord(char) >= 32 and not 0xD800 <= ord(char) <= 0xDFFF)


def operation_name(path):
    """Отображает маршрут в ограниченный набор операций, не пишет URL как сообщение."""
    if "search" in path or "query" in path:
        return "search"
    if "describe" in path or "description" in path:
        return "describe"
    if "neighbors" in path:
        return "neighbors"
    if "source" in path or "/nodes/" in path or "graph-node" in path:
        return "read"
    if "status" in path or "health" in path:
        return "status"
    return "other"


def operation_context(scope, payload):
    """Извлекает allowlist параметров из уже ограниченного JSON и query string."""
    path = scope["path"]
    params = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="replace"))
    fields = {}
    for key in ("projectId", "nodeId", "snapshotId", "query", "question"):
        value = payload.get(key) if isinstance(payload, dict) else None
        fields[key] = value if isinstance(value, str) else next(iter(params.get(key, [])), None)
    match = re.match(r"/api/projects/([^/]+)", path)
    if match:
        fields["projectId"] = unquote(match[1])
    node_match = re.search(r"/nodes/([^/]+)", path)
    if node_match:
        fields["nodeId"] = unquote(node_match[1])
    query = fields.get("query") or fields.get("question")
    preview = text(query, 512)
    # A shortened raw ID could identify a different node; omit rather than alter references.
    node_id = fields["nodeId"]
    if not isinstance(node_id, str) or len(node_id) > 512 or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in node_id):
        node_id = None
    target = {"kind": "code", "projectId": text(fields["projectId"]),
              "nodeId": node_id, "snapshotId": text(fields["snapshotId"]),
              "label": None, "status": "available" if node_id else "unavailable"}
    return {"operationId": uuid4().hex, "parentOperationId": None,
            "operation": operation_name(path), "target": target,
            "query": {"mode": "preview" if preview else "omitted", "preview": preview,
                      "truncated": bool(isinstance(query, str) and len(query) > 512)}}


def activity_event(context, session, client_kind, phase, **extra):
    """Создаёт новый плоский snapshot события; session не выдается за ID субагента."""
    return {"schemaVersion": SCHEMA, "eventId": uuid4().hex, "occurredAt": utc_now(),
            "application": 'ag-tasks', "source": "mcp", "clientKind": client_kind,
            "sessionId": session, "agentId": None, "identityQuality": "process",
            **context, "phase": phase, **extra}
