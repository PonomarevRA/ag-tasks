"""Protect the local desktop boundary and preserve independent operations."""

from fastapi.testclient import TestClient

from ag_tasks.app import create_app
from ag_tasks.desktop_automation import DesktopAutomation, ROUTE, TOKEN_HEADER

REVISION = "a" * 36 + ":1"


def test_desktop_automation_requires_opt_in_token_and_exact_window(tmp_path):
    """Reject browsers, stale windows and arbitrary actions before dispatch."""
    calls = []

    def handler(operation, payload, _dispatch):
        """Return bounded synthetic state with a renderer-specific capture failure."""
        calls.append((operation, payload))
        if operation == "capture":
            return {"status": "error", "error": "desktop_capture_failed"}
        return {"status": "ok", "revision": REVISION}

    broker = DesktopAutomation(enabled=True)
    broker.attach(handler)
    with TestClient(create_app(tmp_path, desktop_automation=broker), base_url="http://localhost") as client:
        assert client.get(ROUTE + "/inspect").status_code == 403
        token = {TOKEN_HEADER: broker.token}
        assert client.get(ROUTE + "/inspect", headers={**token, "Origin": "http://localhost"}).status_code == 403
        state = client.get(ROUTE + "/inspect", headers=token).json()
        scope = {"windowId":state["windowId"], "revision":REVISION}
        assert client.post(ROUTE + "/execute", headers=token, json={**scope, "action":"evaluate_js"}).status_code == 422
        stale = {**scope, "windowId":"0" * 32, "action":"open_board"}
        assert client.post(ROUTE + "/execute", headers=token, json=stale).status_code == 409
        assert client.post(ROUTE + "/capture", headers=token, json=scope).json()["error"] == "desktop_capture_failed"
        assert client.post(ROUTE + "/execute", headers=token, json={**scope, "action":"open_board"}).json()["status"] == "ok"
        assert client.get(ROUTE + "/inspect", headers=token).json()["status"] == "ok"
        assert [entry[0] for entry in calls] == ["inspect", "capture", "execute", "inspect"]
        broker.detach()
        assert client.get(ROUTE + "/inspect", headers=token).status_code == 503


def test_default_catalog_does_not_expose_desktop_tools(tmp_path):
    """Only an explicit MCP opt-in changes the tool list."""
    import asyncio
    from ag_tasks.mcp_server import build_mcp

    def names(enabled):
        """Read the server's actual advertised schemas."""
        server = build_mcp(directory=tmp_path, desktop_automation=enabled)
        return {tool.name for tool in asyncio.run(server.list_tools())}

    assert names(True) - names(False) == {"tasks_desktop_inspect", "tasks_desktop_execute", "tasks_desktop_capture"}
    with TestClient(create_app(tmp_path), base_url="http://localhost") as client:
        assert client.get(ROUTE + "/inspect").status_code == 404



def test_desktop_discovery_does_not_reuse_stale_file_permissions(tmp_path):
    """Publish the live token with private permissions despite a stale legacy temp file."""
    import json
    import os
    import stat

    stale = tmp_path / "endpoint.json.tmp"
    stale.write_text("previous failed startup", encoding="utf-8")
    stale.chmod(0o644)
    broker = DesktopAutomation(enabled=True)
    app = create_app(tmp_path, origin="http://localhost", desktop_automation=broker)
    with TestClient(app, base_url="http://localhost"):
        endpoint = tmp_path / "endpoint.json"
        if os.name == "posix":
            assert stat.S_IMODE(endpoint.stat().st_mode) == 0o600
        assert json.loads(endpoint.read_text(encoding="utf-8"))["desktopToken"] == broker.token
        assert stale.read_text(encoding="utf-8") == "previous failed startup"
    assert not endpoint.exists()


def test_native_preference_controls_live_access_and_next_mcp_catalog(tmp_path):
    """Exercise the native action with a dialog double, then restart the owner."""
    import asyncio
    from types import SimpleNamespace

    from ag_tasks.desktop_automation import desktop_enabled
    from ag_tasks.desktop_mcp_menu import show_preference
    from ag_tasks.mcp_server import build_mcp

    titles = []

    def confirm(title, _message):
        """Accept the native confirmation and record its direction."""
        titles.append(title)
        return True

    def handler(_operation, _payload, _dispatch):
        """Return a stable synthetic window for live access assertions."""
        return {"status": "ok", "revision": REVISION}

    def advertised():
        """Discover the catalog of a new MCP process using the same data directory."""
        tools = asyncio.run(build_mcp(directory=tmp_path).list_tools())
        return {tool.name for tool in tools if tool.name.startswith("tasks_desktop_")}

    def status(client, broker):
        """Use the same authenticated transport as the local MCP client."""
        return client.get(ROUTE + "/inspect", headers={TOKEN_HEADER:broker.token})

    window = SimpleNamespace(create_confirmation_dialog=confirm)
    broker = DesktopAutomation()
    broker.attach(handler)
    assert not desktop_enabled(tmp_path) and not advertised()
    with TestClient(create_app(tmp_path, desktop_automation=broker), base_url="http://localhost") as client:
        assert status(client, broker).json()["error"]["code"] == "desktop_disabled"
        show_preference(window, broker, tmp_path)
        assert desktop_enabled(tmp_path) and len(advertised()) == 3
        assert status(client, broker).status_code == 200

    restarted = DesktopAutomation(enabled=desktop_enabled(tmp_path))
    restarted.attach(handler)
    with TestClient(create_app(tmp_path, desktop_automation=restarted), base_url="http://localhost") as client:
        assert status(client, broker).status_code == 403
        initial = status(client, restarted).json()
        assert initial["status"] == "ok"
        show_preference(window, restarted, tmp_path)
        assert status(client, restarted).json()["error"]["code"] == "desktop_disabled"
        assert not desktop_enabled(tmp_path) and not advertised()
        show_preference(window, restarted, tmp_path)
        assert status(client, restarted).json()["windowId"] != initial["windowId"]
    assert titles == ["Включить мост MCP?", "Выключить мост MCP?", "Включить мост MCP?"]


def test_disable_fences_work_already_waiting_for_native_dispatch():
    """A request admitted before disable cannot start once its binding is retired."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ag_tasks.issues import DomainError

    waiting, resume = Event(), Event()
    started = []

    def handler(_operation, _payload, dispatch):
        """Hold a request before its simulated Cocoa main-thread dispatch."""
        waiting.set()
        assert resume.wait(5)
        assert not dispatch(lambda: started.append(True))
        return {"status": "ok"}

    broker = DesktopAutomation(enabled=True)
    broker.attach(handler)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(broker.call, "inspect", token=broker.token)
        assert waiting.wait(5)
        assert broker.set_enabled(True, False, lambda _value: None)
        resume.set()
        error = future.exception(timeout=5)
    assert isinstance(error, DomainError) and error.code == "desktop_window_changed"
    assert not started
