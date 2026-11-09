"""Bounded, idempotent desktop shutdown shared as source by independent services."""

import logging
import os
import signal
import sys
import threading

SHUTDOWN_SECONDS = 8
SERVER_DRAIN_SECONDS = 6


def stop_owned_children():
    """Kill only live descendants of this desktop owner; psutil guards against PID reuse."""
    import psutil
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return
    for child in reversed(children):
        try:
            child.kill()
        except psutil.Error:
            pass


class Shutdown:
    """Run draining off the UI thread; a watchdog bounds even failed callbacks and non-daemon workers."""

    def __init__(self, drain, finish, *, emergency=None, timeout=SHUTDOWN_SECONDS, exit_process=os._exit):
        """Accept only callbacks for resources owned by this desktop instance, never discovered PIDs."""
        self.drain, self.finish, self.emergency = drain, finish, emergency
        self.timeout, self.exit_process = timeout, exit_process
        self.lock = threading.Lock()
        self.started = False
        self.finished = threading.Event()
        self.system_ending = False
        self.watchdog = None

    def request(self):
        """Start exactly one drain and one non-resetting deadline; return immediately to the native event loop."""
        with self.lock:
            if self.started:
                return
            self.started = True
            self.watchdog = threading.Timer(self.timeout, self.expire)
            self.watchdog.daemon = True
            try:
                self.watchdog.start()
            except RuntimeError:
                self.expire()
                return
        try:
            threading.Thread(target=self._drain, name='desktop-shutdown', daemon=True).start()
        except RuntimeError:
            self.expire()

    def _drain(self):
        """Approve exit only after graceful cleanup; failed cleanup leaves the original deadline armed."""
        try:
            if self.drain() is False:
                return
            self.finished.set()
            self.finish()
        except Exception:
            logging.getLogger(__name__).exception('Desktop graceful shutdown failed')

    def expire(self):
        """Stop owned children without waiting, then bypass Python finalizers that could block OS logout."""
        try:
            if self.emergency:
                self.emergency()
        finally:
            try:
                stop_owned_children()
            finally:
                self.exit_process(1)


class SignalPipe:
    """Wake a native POSIX event loop on TERM/INT even while Python's main thread is inside Cocoa/Qt."""

    def __init__(self, request):
        """Install main-thread handlers; native adapters register the read descriptor without polling."""
        self.request = request
        self.previous = {}
        self.read_fd = self.write_fd = self.previous_fd = None
        if threading.current_thread() is not threading.main_thread():
            return
        if os.name != 'nt':
            self.read_fd, self.write_fd = os.pipe()
            os.set_blocking(self.read_fd, False)
            os.set_blocking(self.write_fd, False)
            self.previous_fd = signal.set_wakeup_fd(self.write_fd, warn_on_full_buffer=False)
        for number in (signal.SIGTERM, signal.SIGINT):
            self.previous[number] = signal.getsignal(number)
            signal.signal(number, self.on_signal)

    def on_signal(self, signum, frame):
        """Signal callbacks do no database work, no joining and no synchronous UI calls."""
        self.request()

    def ready(self):
        """Drain wakeup bytes and ignore unrelated registered Python signals."""
        try:
            data = os.read(self.read_fd, 4096)
        except BlockingIOError:
            return
        if any(number in data for number in (signal.SIGTERM, signal.SIGINT)):
            self.request()

    def close(self):
        """Detach native descriptor watchers first; restore only handlers still owned by this instance."""
        for number, previous in self.previous.items():
            if signal.getsignal(number) == self.on_signal:
                signal.signal(number, previous)
        if self.read_fd is not None:
            current = signal.set_wakeup_fd(-1)
            signal.set_wakeup_fd(self.previous_fd if current == self.write_fd else current)
            os.close(self.read_fd)
            os.close(self.write_fd)
            self.read_fd = self.write_fd = None


def bind_native_shutdown(window, shutdown, *, on_closed=None, on_timeout=None, hide_on_close=None):
    """Bind close, OS session end and signals without synchronous UI draining or an OS quit veto.

    Normal window-close can wait asynchronously; OS termination is approved by
    each platform adapter. Confirmed writes must already be durable during normal
    operation. The watchdog stays armed until process exit, including after drain.
    """
    from .shutdown_platforms import install_platform
    adapter = [None]

    def finish():
        """Finish on the native main loop; disposal errors cannot revoke system termination."""
        def complete():
            try:
                if on_closed:
                    on_closed()
            finally:
                if adapter[0]:
                    adapter[0].finish()
                else:
                    window.destroy()
        if adapter[0]:
            adapter[0].dispatch(complete)
        else:
            complete()

    controller = Shutdown(shutdown, finish, emergency=on_timeout)
    signals = SignalPipe(controller.request)

    def request_quit():
        """Explicit Quit always enters the existing bounded owner drain, even while the window is hidden."""
        if adapter[0] and sys.platform == 'darwin':
            adapter[0].request_quit()
        else:
            controller.request()

    def before_close():
        """Hide a normal close only with a ready tray; signals and OS termination never hide or veto exit."""
        if controller.finished.is_set():
            return None
        if not controller.started and not controller.system_ending and hide_on_close:
            try:
                if hide_on_close():
                    return False
            except Exception:
                # Failed optional tray work must retain the established close-to-quit behavior.
                pass
        request_quit()
        return None if controller.system_ending else False

    def install():
        """Install after the renderer owns its native window, once even across page reloads."""
        if adapter[0] is None:
            adapter[0] = install_platform(window, controller, signals)

    # AppKit's first Quit can precede shown/loaded. Install its delegate before
    # creating the NSWindow so that an OS quit never reaches a cancelable fallback.
    if sys.platform == 'darwin':
        install()
    window.events.closing += before_close
    window.events.before_show += install

    def restore():
        """Remove subscriptions; do not cancel a shutdown watchdog while Python workers can remain alive."""
        window.events.closing -= before_close
        window.events.before_show -= install
        if adapter[0]:
            adapter[0].close()
        signals.close()

    restore.request_quit = request_quit
    return restore
