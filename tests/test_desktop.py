"""Native launcher ownership and readiness contracts without requiring a GUI session."""

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

class LoadedHandlers(list):
    """Match pywebview's event subscription without initializing Cocoa."""

    def __iadd__(self, handler):
        """Retain a registered callback for lifecycle assertions."""
        self.append(handler)
        return self


class ClosingHandlers(LoadedHandlers):
    """Match synchronous close-handler removal during launcher cleanup."""
    def __isub__(self, handler):
        """Release one owned event subscription."""
        self.remove(handler)
        return self


from ag_tasks import desktop


class Listener:
    """Track reserved-socket cleanup without opening a real port in unit tests."""

    def __init__(self):
        """Start with an owned socket marker and a fixed isolated address."""
        self.closed = False
        self.options = []

    def setsockopt(self, *option):
        """Record safe socket options without opening a real listener."""
        self.options.append(option)

    def getsockname(self):
        """Expose the bound port used by the native window URL."""
        return "127.0.0.1", 49001

    def close(self):
        """Record release even if app initialization fails before server startup."""
        self.closed = True


class Server:
    """Cooperate with the real launcher thread using the same readiness/shutdown flags."""

    def __init__(self, config):
        """Keep configuration and start before the launcher observes readiness."""
        self.config = config
        self.started = False
        self.should_exit = False
        self.force_exit = False
        self.stopped = False

    def run(self, sockets):
        """Wait for the launcher to stop this instance, never another process."""
        self.started = True
        while not self.should_exit:
            time.sleep(0.001)
        self.stopped = True


def native_dependencies(monkeypatch):
    """Inject only optional platform adapters; retain actual launcher lifecycle behavior."""
    from ag_tasks import shutdown_platforms
    monkeypatch.setattr(shutdown_platforms, "install_platform", lambda *args: None)
    servers = []
    windows = []

    def create_server(config):
        """Retain the launched server for ownership assertions."""
        value = Server(config)
        servers.append(value)
        return value

    def create_window(*args, **kwargs):
        """Retain native window options and permit lifecycle event registration."""
        windows.append((args, kwargs))
        return SimpleNamespace(events=SimpleNamespace(loaded=LoadedHandlers(), shown=ClosingHandlers(), before_show=ClosingHandlers(), closing=ClosingHandlers(), closed=LoadedHandlers()), expose=lambda callback:None)

    view = SimpleNamespace(settings={}, create_window=create_window, start=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=lambda app, **kwargs: (app, kwargs), Server=create_server))
    monkeypatch.setitem(sys.modules, "webview", view)
    monkeypatch.setitem(sys.modules, "webview.menu", SimpleNamespace(
        Menu=lambda title, items: (title, items),
        MenuAction=lambda label, callback: (label, callback),
    ))
    return servers, windows


def test_close_window_stops_own_server_and_releases_bound_socket(monkeypatch, tmp_path):
    """The displayed URL uses the reserved socket and factory receives its own data directory."""
    servers, windows = native_dependencies(monkeypatch)
    listener = Listener()
    monkeypatch.setattr(desktop, "reserve_loopback", lambda port: listener)
    received = []
    desktop.run_desktop(lambda **kwargs: received.append(kwargs) or object(), tmp_path)
    assert len(received) == 1
    options = received[0]
    if sys.platform == "darwin":
        assert not options.pop("desktop_automation").enabled
    assert options == {"data_dir": tmp_path, "origin": "http://127.0.0.1:49001"}
    assert windows[0][0] == ("ag-tasks", "http://127.0.0.1:49001/?desktop=1")
    assert servers[0].stopped and servers[0].should_exit
    assert listener.closed


def test_factory_failure_releases_socket_before_window_creation(monkeypatch, tmp_path):
    """Failing initialization cannot leave a listening socket or show another service's page."""
    servers, windows = native_dependencies(monkeypatch)
    listener = Listener()
    monkeypatch.setattr(desktop, "reserve_loopback", lambda port: listener)

    def fail(**kwargs):
        """Represent a rejected local configuration before Uvicorn exists."""
        raise ValueError("configuration rejected")

    with pytest.raises(ValueError, match="configuration rejected"):
        desktop.run_desktop(fail, tmp_path)
    assert listener.closed
    assert not windows and not servers


def test_failed_or_timed_out_startup_never_opens_empty_window():
    """Readiness checks handle both finished workers and finite startup deadlines."""
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(RuntimeError, match="stopped before startup"):
        desktop.wait_started(SimpleNamespace(started=False, should_exit=False), stopped)
    with pytest.raises(RuntimeError, match="timed out"):
        desktop.wait_started(SimpleNamespace(started=False, should_exit=False), threading.Event(), timeout=0)


def test_busy_preferred_port_uses_owned_ephemeral_socket(monkeypatch):
    """An occupied origin must not cause this window to trust an existing foreign server."""
    import errno
    addresses = []
    listener = Listener()

    def bind(address):
        """Reject only the preferred port, then permit an ephemeral local reservation."""
        addresses.append(address)
        if len(addresses) == 1:
            raise OSError(errno.EADDRINUSE, "already bound")

    listener.bind = bind
    listener.listen = lambda backlog: None
    monkeypatch.setattr(desktop.socket, "socket", lambda *args: listener)
    assert desktop.reserve_loopback(8895) is listener
    assert addresses == [("127.0.0.1", 8895), ("127.0.0.1", 0)]
    assert not listener.closed


def test_other_bind_failures_release_socket_without_retry(monkeypatch):
    """Permission and address failures are not treated as harmless port collisions."""
    import errno
    listener = Listener()

    def bind(address):
        """Represent a denied bind, which cannot be repaired by choosing another port."""
        raise OSError(errno.EACCES, "denied")

    listener.bind = bind
    monkeypatch.setattr(desktop.socket, "socket", lambda *args: listener)
    with pytest.raises(OSError):
        desktop.reserve_loopback(8895)
    assert listener.closed


def test_restart_reuses_time_wait_but_never_a_live_listener():
    """An accepted connection must not move the next desktop owner to a random port on POSIX."""
    import os, socket
    if os.name == 'nt':
        pytest.skip('POSIX TIME_WAIT policy')
    owner = desktop.reserve_loopback(0)
    port = owner.getsockname()[1]
    foreign = desktop.reserve_loopback(port)
    assert foreign.getsockname()[1] != port
    foreign.close()
    peer = socket.create_connection(('127.0.0.1', port))
    accepted, _ = owner.accept()
    accepted.close(); peer.close(); owner.close()
    replacement = desktop.reserve_loopback(port)
    try:
        assert replacement.getsockname()[1] == port
    finally:
        replacement.close()


@pytest.mark.parametrize(("platform", "renderer"), [
    ("darwin", "cocoa"), ("win32", "edgechromium"), ("linux", "qt"),
])
def test_native_help_and_tray_share_installed_version_checker(monkeypatch, tmp_path, platform, renderer):
    """Wire every supported native Help menu and the tray to one explicit release checker."""
    from ag_tasks import desktop_update_menu, native_menu, native_toolbar

    native_dependencies(monkeypatch)
    monkeypatch.setattr(desktop, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(desktop, "reserve_loopback", lambda _: Listener())
    native_menus = []
    starts = []
    confirmations = []
    checks = []
    tray = []
    windows = []
    view = sys.modules["webview"]
    fixture_window = view.create_window

    def create_window(*args, **kwargs):
        """Retain the owned window and record result prompts without rendering GUI."""
        window = fixture_window(*args, **kwargs)
        window.create_confirmation_dialog = lambda *message: confirmations.append(message)
        windows.append(window)
        return window

    def start(**options):
        """Fire the real launcher's loaded hook after menu setup."""
        starts.append(options)
        for handler in windows[0].events.loaded:
            handler()
        if platform != "darwin":
            _, actions = options["menu"][0]
            check_for_updates = dict(actions)["Проверить обновления…"]
            check_for_updates()
            tray[0]()

    def bind_background(window, **options):
        """Capture the injected updater while preserving the launcher close contract."""
        tray.append(options["check_updates"])
        return SimpleNamespace(closed=False, close=lambda: None)

    def check(*, force=False):
        """Record whether the native callback requested a manual refresh."""
        checks.append(force)
        return {"available": False, "version": "v0.8.3", "changelog": {"ru": [], "en": []}}

    monkeypatch.setattr(view, "create_window", create_window)
    monkeypatch.setattr(view, "start", start)
    monkeypatch.setattr("desktop_support.bind_background", bind_background)
    monkeypatch.setattr(native_toolbar, "install_native_toolbar", lambda *_, **__: None)
    monkeypatch.setattr(native_toolbar, "bind_sidebar_presentation", lambda *_, **__: None)

    def run_update_worker(*, target, **options):
        """Run the separately unit-tested worker inline inside this wiring fixture."""
        return SimpleNamespace(start=target)

    monkeypatch.setattr(desktop_update_menu, "Thread", run_update_worker)

    def verify_help(window, app_name, *, help_actions, **kwargs):
        """Verify menu admission while the application owns its live server."""
        if platform == "darwin":
            native_menus.append(help_actions)
            help_actions[1][1]()
            tray[0]()

    monkeypatch.setattr(native_menu, "install_native_menu", verify_help)
    app = SimpleNamespace(state=SimpleNamespace(release_updates=SimpleNamespace(check=check)))
    desktop.run_desktop(lambda **_: app, tmp_path)

    expected = ["Tasks Help", "Проверить обновления…", "Релизы на GitHub"]
    assert starts[0]["gui"] == renderer
    if platform == "darwin":
        assert [label for label, _ in native_menus[0]] == expected
        assert "menu" not in starts[0]
    else:
        (title, actions), = starts[0]["menu"]
        assert title == "Справка"
        assert [label for label, _ in actions] == expected
    assert checks == [True, True]
    assert len(confirmations) == 2
    assert "Более новой версии пока нет" in confirmations[0][1]
