"""Native tray ownership independent of HTTP service implementations."""

import logging
from pathlib import Path
import sys
from threading import Lock, Thread
import webbrowser

from .settings import read_installation
from .updates import check_release


class BackgroundHandle:
    """Hide only after native registration succeeds; explicit Quit bypasses this policy."""
    def __init__(self, window, app_id, title, origin, on_quit, open_activity, data_directory, current_version):
        """Retain native objects and callbacks for exactly one desktop owner."""
        self.window, self.app_id, self.title = window, app_id, title
        self.origin, self.on_quit, self.open_activity = origin, on_quit, open_activity
        self.ready = self.closed = False
        self.native = None
        self.settings = read_installation(app_id, data_directory or Path.home() / '.local/share' / app_id)
        self.data_directory = Path(data_directory) if data_directory is not None else self.settings.data_directory
        self.current_version = current_version
        self.update_lock = Lock()
        self.installing = False
        self.dispatch = lambda callback: callback()
        window.events.shown += self.install

    def install(self):
        """Attach once after renderer startup; unsupported trays retain normal close-to-quit behavior."""
        if self.ready or self.closed or self.installing:
            return
        try:
            self.installing = True
            from .tray_platforms import install_tray
            install_tray(self)
        except Exception:
            logging.getLogger(__name__).exception('Native tray unavailable; window close will quit')

    def show(self):
        """Restore the existing window without starting another backend."""
        if self.native and hasattr(self.native, "show"):
            self.dispatch(self.native.show)
        else:
            self.window.show()
            self.window.restore()

    def activity(self):
        """Open service-owned request history; fallback shows a bounded widget snapshot page."""
        if self.open_activity:
            self.open_activity()
        else:
            self.window.load_url(self.origin.rstrip('/') + '/api/desktop/v1/widget')
        self.show()

    def storage(self):
        """Display actual configured folders; runtime data movement is intentionally separate."""
        message = f'Artifacts: {self.data_directory}\nBackups: {self.settings.backup_directory}'
        self.window.create_confirmation_dialog(self.title + ' — Storage', message)

    def updates(self):
        """Fetch metadata off the GUI thread and ask before opening an official installer page."""
        if not self.update_lock.acquire(blocking=False):
            return
        def check():
            """One update worker owns the confirmation until completion or failure."""
            try:
                result = check_release(self.app_id, self.current_version)
                message = ('New release ' if result['available'] else 'Latest release: ') + result['version']
                if self.window.create_confirmation_dialog(self.title + ' — Updates', message + '\nOpen official download page?'):
                    webbrowser.open(result['url'])
            except Exception:
                logging.getLogger(__name__).exception('Update check failed')
                self.window.create_confirmation_dialog(self.title + ' — Updates', 'Update check unavailable. Your data and application were not changed.')
            finally:
                self.update_lock.release()
        Thread(target=check, name='desktop-update-check', daemon=True).start()

    def hide_on_close(self):
        """Return true only when an accessible registered tray can restore the window."""
        if not self.ready or self.closed or not self.settings.close_to_tray:
            return False
        if self.native and hasattr(self.native, "hide"):
            self.dispatch(self.native.hide)
        else:
            self.window.hide()
        return True

    def close(self):
        """Remove the native icon on its owning UI thread, even after partial installation."""
        if self.closed:
            return
        self.closed = True
        self.ready = False
        self.window.events.shown -= self.install
        if self.native:
            self.dispatch(self.native.close)


def bind_background(window, *, app_id, title, origin, on_quit, open_activity=None, data_directory=None, current_version="0.1.0"):
    """Return an immediately usable lifecycle hook; native readiness follows the shown event."""
    return BackgroundHandle(window, app_id, title, origin, on_quit, open_activity, data_directory, current_version)
