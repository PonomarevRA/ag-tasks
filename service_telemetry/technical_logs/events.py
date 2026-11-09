"""Bounded primitive snapshots: no live request, traceback, scope or arbitrary object is queued."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import math
import re
from itertools import islice
from uuid import uuid4

CORRELATION = ("traceId", "spanId", "operationId", "sessionId", "taskId")
context_fields = ContextVar("ag-tasks_technical_log_context", default=None)
SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|credential|apikey|api_key|body|headers|actor|scope", re.I)
ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
URL_CREDENTIAL = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
FORMAT = re.compile(r"%(?:\([^)]+\))?[-+#0 ]*(\*|[0-9]+)?(?:\.(\*|[0-9]+))?[hlL]?[diouxXeEfFgGcrsa%]")
DIAGNOSTIC_KEYS = {"durationMs", "elapsedMs", "resultCount", "count", "statusCode", "errorCode", "retryCount",
                   "attempt", "queueItems", "queueBytes", "sizeBytes", "bytes", "component", "phase", "status"}


def utc_now():
    """Returns an explicit UTC timestamp for commit or capture."""
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, maximum):
    """Truncates native strings, removes invalid scalars and redacts common credential forms."""
    if type(value) is not str:
        return None
    value = value[:maximum]
    value = "".join(char for char in value if char in "\n\t" or ord(char) >= 32 and not 0xD800 <= ord(char) <= 0xDFFF)
    value = ASSIGNMENT.sub(lambda match: match.group(1) + "=[REDACTED]", value)
    value = BEARER.sub(lambda match: match.group(1) + " [REDACTED]", value)
    return URL_CREDENTIAL.sub(r"\1[REDACTED]@", value)[:maximum]


def scalar(value):
    """Accepts safe bounded native diagnostic primitives; never invokes foreign repr/str."""
    if type(value) is str:
        return safe_text(value, 256)
    if value is None or type(value) is bool:
        return value
    if type(value) is int and -(2**63) <= value <= 2**63 - 1:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def level_name(number):
    """Maps Python numeric levels to the six portable severity names."""
    for boundary, name in ((10, "TRACE"), (20, "DEBUG"), (30, "INFO"), (40, "WARN"), (50, "ERROR")):
        if number < boundary:
            return name
    return "FATAL"


@contextmanager
def log_context(**fields):
    """Captures only known correlation IDs in the producer task, restoring nested context on exit."""
    value = dict(context_fields.get() or {})
    for key in CORRELATION:
        if key in fields:
            value[key] = safe_text(fields[key], 128)
    token = context_fields.set(value)
    try:
        yield
    finally:
        context_fields.reset(token)


def exception_snapshot(info):
    """Copies at most 32 frame locations without locals, source lines or retained traceback objects."""
    if not info or not isinstance(info, tuple) or len(info) != 3 or info[1] is None:
        return None
    error, trace = info[1], info[2]
    frames = []
    while trace is not None and len(frames) < 32:
        code = trace.tb_frame.f_code
        frames.append(f"{safe_text(code.co_filename, 256)}:{trace.tb_lineno} in {safe_text(code.co_name, 128)}")
        trace = trace.tb_next
    arguments = getattr(error, "args", ())
    message = "Exception message unavailable"
    if type(arguments) is tuple:
        # Calling an arbitrary exception's __str__ can execute application code or dump object state.
        parts = [scalar(value) for value in arguments[:4]]
        message = safe_text("; ".join(str(value) for value in parts if value is not None), 2048)
    return {"type": safe_text(type(error).__name__, 128), "message": message,
            "stackTrace": safe_text("\n".join(frames), 8192)}


def from_record(record):
    """Snapshots a native logging record before queue handoff; complex arguments are omitted."""
    template = safe_text(record.msg, 2048) or "[non-text log message]"
    arguments = record.args
    message = template
    if type(arguments) is tuple:
        arguments = tuple(scalar(value) for value in arguments[:16])
    elif type(arguments) is dict:
        arguments = {key: "[REDACTED]" if SENSITIVE.search(key) else scalar(value)
                     for key, value in islice(arguments.items(), 16) if type(key) is str and len(key) <= 128}
    else:
        arguments = ()
    bounded_format = True
    for match in FORMAT.finditer(template):
        for width in match.groups():
            if width and (width == "*" or len(width) > 4 or int(width) > 2048):
                bounded_format = False
    if arguments and bounded_format:
        try:
            message = safe_text(template % arguments, 2048)
        except (ValueError, TypeError, OverflowError):
            message = template
    properties = {}
    supplied = getattr(record, "properties", None)
    if type(supplied) is dict:
        for key, value in islice(supplied.items(), 16):
            if type(key) is str and len(key) <= 64 and (key in DIAGNOSTIC_KEYS or SENSITIVE.search(key)):
                properties[safe_text(key, 64)] = "[REDACTED]" if SENSITIVE.search(key) else scalar(value)
    event = {"schemaVersion": "technical-log/v1", "eventId": uuid4().hex, "application": 'ag-tasks',
             "occurredAt": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
             "level": level_name(record.levelno), "category": safe_text(record.name, 128) or "application",
             "eventName": safe_text(getattr(record, "eventName", None), 128), "messageTemplate": template,
             "message": message, "exception": exception_snapshot(record.exc_info), "properties": properties}
    context = context_fields.get() or {}
    for key in CORRELATION:
        event[key] = safe_text(getattr(record, key, context.get(key)), 128)
    return event


def estimate(value):
    """Conservatively bounds UTF-8 JSON and writer-assigned metadata without rendering JSON."""
    if type(value) is dict:
        return 2 + sum(estimate(key) + estimate(item) + 2 for key, item in value.items())
    if type(value) is str:
        return 2 + len(value.encode("utf-8")) + sum(5 for char in value if char in {'"', "\\"} or ord(char) < 32)
    if value is None or type(value) is bool:
        return 5
    return 32


def fit_event(event, maximum):
    """Shrinks diagnostic text to the configured byte budget; envelope and explicit nulls remain."""
    while estimate(event) + 256 > maximum:
        exception = event.get("exception")
        if exception and len(exception["stackTrace"] or "") > 128:
            exception["stackTrace"] = exception["stackTrace"][:len(exception["stackTrace"]) // 2]
        elif event["properties"]:
            event["properties"].pop(next(reversed(event["properties"])))
        elif exception and len(exception["message"] or "") > 128:
            exception["message"] = exception["message"][:len(exception["message"]) // 2]
        elif len(event["message"]) > 128:
            event["message"] = event["message"][:len(event["message"]) // 2]
        elif len(event["messageTemplate"]) > 128:
            event["messageTemplate"] = event["messageTemplate"][:len(event["messageTemplate"]) // 2]
        else:
            return None
    return event
