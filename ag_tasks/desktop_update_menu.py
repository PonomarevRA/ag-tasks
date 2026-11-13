"""Native, user-triggered update commands shared by Help and the tray."""

import logging
from threading import Event, Lock, Thread
import webbrowser


LOG = logging.getLogger(__name__)


class DesktopUpdateMenu:
    """Give one manual check ownership of network work and its native result dialog."""

    def __init__(self, window, *, current_version, releases_url, fetch):
        """Keep the current version, trusted release destination and shared checker."""
        self.window = window
        self.current_version = current_version
        self.releases_url = releases_url
        self.fetch = fetch
        self.closed = Event()
        self.in_flight = Lock()

    def actions(self):
        """Use the same labels and callbacks in AppKit, Qt and Windows Forms."""
        return (
            ("Проверить обновления…", self.check),
            ("Релизы на GitHub", self.open_releases),
        )

    def check(self):
        """Start at most one manual check without waiting on the caller's UI thread."""
        if self.closed.is_set() or not self.in_flight.acquire(blocking=False):
            return
        try:
            Thread(target=self._check, name="ag-tasks-update-menu", daemon=True).start()
        except RuntimeError:
            self.in_flight.release()
            raise

    def _check(self):
        """Display bounded release facts and open GitHub only after confirmation."""
        try:
            try:
                result = self.fetch()
            except (OSError, ValueError):
                LOG.info("Manual release check unavailable", exc_info=True)
                message = f"Установлена версия {self.current_version}.\nНе удалось проверить релизы на GitHub."
                destination = self.releases_url
            else:
                destination, message = self._describe(result)
            if self.closed.is_set():
                return
            if self.window.create_confirmation_dialog("Обновления ag-tasks", message + "\n\nОткрыть релизы на GitHub?"):
                self._open(destination)
        finally:
            self.in_flight.release()

    def _describe(self, result):
        """Keep plain release notes compact and separate current from available versions."""
        installed = f"Установлена версия {self.current_version}."
        if not result["available"]:
            return self.releases_url, (f"{installed}\nПоследний релиз на GitHub — {result['version']}.\n"
                                       "Более новой версии пока нет.")
        message = f"{installed}\nДоступна версия {result['version']}."
        changelog = result["changelog"]
        bullets = changelog.get("ru") or changelog.get("en") or ()
        if bullets:
            message += "\n\nЧто нового\n" + "\n".join(f"• {line}" for line in bullets)
        return result["url"], message

    def _open(self, url):
        """Use the system browser for the checker-owned destination."""
        if not self.closed.is_set():
            webbrowser.open(url)

    def open_releases(self):
        """Open the public releases index directly when requested from Help."""
        self._open(self.releases_url)

    def close(self):
        """Suppress a pending result or browser launch once the window has closed."""
        self.closed.set()
