"""Opt-in local broker for one owned desktop WebView."""

import base64
import binascii
import json
import os
import secrets
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .issues import DomainError

MAX_PNG_BYTES = 1024 * 1024
ROUTE = "/api/v1/agent/desktop"
TOKEN_HEADER = "x-ag-tasks-desktop-token"
PREFERENCE = "desktop-mcp.enabled"
ACTIONS = Literal["open_board", "open_sessions", "open_analytics", "open_spaces", "close_spaces", "toggle_sidebar"]


def desktop_enabled(directory):
    """Read the opt-in marker; a fresh or missing data directory is disabled."""
    marker = Path(directory) / PREFERENCE
    try:
        return not marker.is_symlink() and marker.is_file()
    except OSError:
        return False


def save_desktop_enabled(directory, enabled):
    """Persist the boolean as an atomic marker creation or removal."""
    marker = Path(directory) / PREFERENCE
    if not enabled:
        marker.unlink(missing_ok=True)
        return
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not desktop_enabled(directory):
            raise
    else:
        os.close(descriptor)


class WindowScope(BaseModel):
    """A command or capture applies to one observed window revision."""

    model_config = ConfigDict(extra="forbid")
    windowId: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: str = Field(pattern=r"^[0-9a-f-]{36}:[1-9][0-9]{0,14}$")


class WindowCommand(WindowScope):
    """Only fixed navigation and presentation actions cross the bridge."""

    action: ACTIONS


class DesktopAutomation:
    """Serialize operations and reject results from a retired window binding."""

    def __init__(self, *, enabled=False):
        self._enabled = enabled
        self.token = secrets.token_hex(32)
        self.binding = None
        self.lifecycle = Lock()
        self.operation = Lock()

    @property
    def enabled(self):
        """Return the effective process state for the native settings dialog."""
        with self.lifecycle:
            return self._enabled

    def set_enabled(self, expected, enabled, persist):
        """Commit the preference before changing live access and retiring old window scopes."""
        with self.lifecycle:
            if self._enabled != expected:
                return False
            persist(enabled)
            self._enabled = enabled
            if self.binding is not None:
                self.binding = (uuid4().hex, self.binding[1])
            return True

    def attach(self, handler):
        """Publish a fresh opaque identity for this window lifetime."""
        with self.lifecycle:
            self.binding = (uuid4().hex, handler)

    def detach(self):
        """Retire the current window before server shutdown."""
        with self.lifecycle:
            self.binding = None

    def call(self, operation, scope=None, *, token):
        """Dispatch a single bounded operation without queueing behind a stalled UI."""
        if not self.operation.acquire(blocking=False):
            raise DomainError("desktop_busy", "Desktop window is busy.", 409)
        try:
            return self._call_bound(operation, scope, token)
        finally:
            self.operation.release()

    def _call_bound(self, operation, scope, token):
        """Fence both input identity and the native response."""
        with self.lifecycle:
            if not self._enabled:
                raise DomainError("desktop_disabled", "Desktop automation is disabled.", 503)
            if not secrets.compare_digest(token.encode(), self.token.encode()):
                raise DomainError("desktop_forbidden", "Desktop automation requires local MCP authorization.", 403)
            binding = self.binding
        if binding is None:
            raise DomainError("desktop_unavailable", "No owned desktop window.", 503)
        window_id, handler = binding
        if scope and scope.windowId != window_id:
            raise DomainError("desktop_window_changed", "Inspect the current window again.", 409)
        payload = scope.model_dump(exclude={"windowId"}) if scope else {}
        def dispatch(start):
            """Start queued native work only while this exact binding still owns access."""
            with self.lifecycle:
                if self.binding is not binding:
                    return False
                start()
                return True

        result = handler(operation, payload, dispatch)
        with self.lifecycle:
            if self.binding is not binding:
                raise DomainError("desktop_window_changed", "Inspect the current window again.", 409)
        limit = (MAX_PNG_BYTES + 2) // 3 * 4 + 2048 if operation == "capture" else 16_384
        if not isinstance(result, dict) or len(json.dumps(result).encode()) > limit:
            raise DomainError("desktop_result_invalid", "Invalid or oversized desktop result.", 502)
        if operation == "capture" and result.get("status") == "ok":
            decode_png(result.get("pngBase64"))
        return {**result, "windowId": window_id}


def decode_png(encoded):
    """Validate bounded PNG bytes before forwarding them to an agent."""
    try:
        png = base64.b64decode(encoded, validate=True)
    except (binascii.Error, TypeError, ValueError) as error:
        raise DomainError("desktop_image_invalid", "Invalid desktop image.", 502) from error
    if len(png) > MAX_PNG_BYTES or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DomainError("desktop_image_invalid", "Invalid desktop image.", 502)
    return png


def register(app, automation):
    """Register for the owned desktop only; the broker enforces current opt-in."""
    if automation is None:
        return

    def authorize(request):
        """Reject browser-origin requests and require the current owner token."""
        if "origin" in request.headers or "sec-fetch-site" in request.headers:
            raise DomainError("desktop_forbidden", "Desktop automation requires local MCP authorization.", 403)
        return request.headers.get(TOKEN_HEADER, "")

    @app.get(ROUTE + "/inspect")
    def inspect(request: Request):
        """Read semantic state without starting a visual capture."""
        return automation.call("inspect", token=authorize(request))

    @app.post(ROUTE + "/execute")
    def execute(request: Request, scope: WindowCommand):
        """Run one allowlisted action on the observed window."""
        return automation.call("execute", scope, token=authorize(request))

    @app.post(ROUTE + "/capture")
    def capture(request: Request, scope: WindowScope):
        """Capture WebView content independently of semantic inspection."""
        return automation.call("capture", scope, token=authorize(request))
