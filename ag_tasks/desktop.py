"""Independent native-window lifecycle; HTTP service and data belong only to Tasks."""

import errno
import os
from pathlib import Path
import socket
import sys
import threading
import time

from .native_shutdown import SERVER_DRAIN_SECONDS


def reserve_loopback(port: int) -> socket.socket:
    """Bind before starting the server; occupied preferred ports fall back to an owned ephemeral socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # POSIX restart may reuse TIME_WAIT, never a live listener; no SO_REUSEPORT.
        if os.name != "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def wait_started(server, finished: threading.Event, timeout: float = 30) -> None:
    """Refuse an empty native window when Uvicorn fails startup or its readiness deadline expires."""
    deadline = time.monotonic() + timeout
    while not server.started:
        if finished.is_set() or server.should_exit:
            raise RuntimeError("Tasks HTTP server stopped before startup")
        if time.monotonic() >= deadline:
            raise RuntimeError("Tasks HTTP startup timed out")
        finished.wait(0.05)
    if finished.is_set():
        raise RuntimeError("Tasks HTTP server stopped before opening the window")


def run_desktop(app_factory, data_dir: Path, port: int = 8895, *, enable_automation=False) -> None:
    """Run an independent native main-thread window; keep one owned server while a ready tray can restore the hidden window.

    app_factory receives data_dir=Path, origin. An already occupied port is never reused
    as a trusted application; this instance owns the bound socket it displays.
    """
    try:
        import uvicorn
        import webview
    except ImportError as error:
        raise RuntimeError("Install ag-tasks[desktop] to open its native window") from error
    automation = None
    app_options = {}
    if sys.platform == "darwin" or enable_automation:
        from .desktop_automation import DesktopAutomation, desktop_enabled
        automation = DesktopAutomation(enabled=enable_automation or desktop_enabled(data_dir))
        app_options["desktop_automation"] = automation
    listener = reserve_loopback(port)
    try:
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        finished = threading.Event()
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        app = app_factory(data_dir=data_dir, origin=origin, **app_options)
        config = uvicorn.Config(app, host="127.0.0.1", port=listener.getsockname()[1],
                                timeout_graceful_shutdown=2, loop="asyncio", http="h11", ws="none", log_config=None, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
    except BaseException:
        listener.close()
        raise

    def serve() -> None:
        """Always signal termination, including startup SystemExit from the server thread."""
        try:
            server.run(sockets=[listener])
        finally:
            finished.set()

    worker = threading.Thread(target=serve, name="ag-tasks-http", daemon=True)
    worker.start()

    def shutdown_owned_server():
        """Close release prompts and request graceful cleanup within the watchdog deadline."""
        if update_menu is not None:
            update_menu.close()
        server.should_exit = True
        worker.join(SERVER_DRAIN_SECONDS)
        return not worker.is_alive()

    restore_shutdown = None
    presentation = None
    background = None
    update_menu = None
    try:
        wait_started(server, finished)
        webview.settings["ALLOW_DOWNLOADS"] = False
        window = webview.create_window("ag-tasks", f"http://127.0.0.1:{listener.getsockname()[1]}/?desktop=1",
                              width=1160, height=780, min_size=(390, 560), text_select=True)
        if automation is not None:
            from .desktop_automation_host import NativeWindow
            automation.attach(NativeWindow(window, origin))
            window.events.closed += automation.detach
        from . import __version__
        from .desktop_update_menu import DesktopUpdateMenu
        from .native_menu import bind_native_shutdown, install_native_menu
        from .release_updates import RELEASES_URL

        def check_for_release():
            """Use the running owner's release cache for an explicit native check."""
            return app.state.release_updates.check(force=True)

        update_menu = DesktopUpdateMenu(window, current_version=__version__, releases_url=RELEASES_URL,
                                        fetch=check_for_release)
        help_actions = (
            ("Tasks Help", lambda: window.create_confirmation_dialog("Ag Tasks",
                "Create issues, link evidence and register local repositories. Prepare an isolated branch and submit it for review; merging requires explicit user approval. Closing hides the window only when its tray is ready. Quit always stops this instance.")),
            *update_menu.actions(),
        )
        from .native_toolbar import bind_sidebar_presentation
        presentation = bind_sidebar_presentation(window, origin, sidebar_id="sidebar", right_class="", collapsed_class="sidebar-collapsed")

        def toggle_sidebar():
            """Dispatch only the fixed local UI event shared by the native titlebar and menu shortcut."""
            window.run_js("window.dispatchEvent(new Event('ag-tasks:toggle-sidebar'))")

        def configure_menu():
            """Install platform menus with a native-only bridge preference action."""
            view_actions = [("Refresh Data", lambda: window.run_js("document.getElementById('refresh')?.click()"))]
            if automation is not None:
                from .desktop_mcp_menu import show_preference
                view_actions.append(("Мост MCP…", lambda: show_preference(window, automation, data_dir)))
            install_native_menu(window, "ag-tasks", sidebar_action=toggle_sidebar, view_actions=view_actions,
                                help_actions=help_actions)
            from .native_toolbar import install_native_toolbar
            install_native_toolbar(window, toggle_sidebar, identity="ag-tasks")

        def close_presentation():
            """Dispose the tray and sidebar before the native UI loop ends."""
            try:
                if background and not background.closed:
                    background.close()
            finally:
                if presentation:
                    presentation.close()

        restore_shutdown = bind_native_shutdown(window, shutdown_owned_server, on_closed=close_presentation,
            hide_on_close=lambda: bool(background and background.hide_on_close()))
        from desktop_support import bind_background
        background = bind_background(window, app_id='ag-tasks', title='ag-tasks', origin=origin,
            on_quit=restore_shutdown.request_quit, open_activity=lambda: window.load_url(origin + '/desktop-activity.html?desktop=1'),
            data_directory=data_dir, current_version=__version__, check_updates=update_menu.check)
        window.events.loaded += configure_menu
        start_options = {}
        if sys.platform != "darwin":
            from webview.menu import Menu, MenuAction
            start_options["menu"] = [Menu("Справка", [MenuAction(*action) for action in help_actions])]
        webview.start(gui={"darwin": "cocoa", "win32": "edgechromium"}.get(sys.platform, "qt"),
                      debug=False, private_mode=False, storage_path=str(data_dir / "webview"), **start_options)
    finally:
        if automation is not None:
            automation.detach()
        try:
            stopped = shutdown_owned_server()
        finally:
            if background and not background.closed:
                background.close()
            if presentation:
                presentation.close()
            if restore_shutdown:
                restore_shutdown()
            listener.close()
        if not stopped:
            raise RuntimeError("Tasks HTTP server did not stop after closing its window")
