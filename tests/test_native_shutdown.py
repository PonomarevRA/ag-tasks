"""Bounded exit and OS session contracts without ending the user's desktop session."""

from pathlib import Path
from types import SimpleNamespace
import json
import os
import signal
import subprocess
import sys
import threading
import time

import psutil
import pytest

from ag_tasks import native_shutdown as shutdown
from ag_tasks import shutdown_platforms as platforms


class Events(list):
    """Model the renderer's callback subscription contract."""
    def __iadd__(self, callback): self.append(callback); return self
    def __isub__(self, callback): self.remove(callback); return self


def test_repeated_quit_drains_once_without_blocking_the_caller():
    """Concurrent close/signal requests share one deadline and one save operation."""
    started, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def drain():
        """Simulate a pending transaction that is committed before completion."""
        calls.append(threading.current_thread())
        started.set()
        release.wait(2)
        return True
    controller = shutdown.Shutdown(drain, completed.set, timeout=3, exit_process=lambda code: None)
    try:
        before = time.monotonic()
        controller.request()
        assert time.monotonic() - before < .5
        assert started.wait(1)
        for _ in range(10): controller.request()
        assert not completed.is_set()
        release.set()
        assert completed.wait(1)
        assert len(calls) == 1 and calls[0] is not threading.main_thread()
        assert controller.finished.is_set()
    finally:
        release.set()
        controller.watchdog.cancel()


def test_failed_drain_keeps_deadline_instead_of_vetoing_exit(monkeypatch):
    """Failure cannot restart the deadline, display a dialog or wait forever."""
    expired, order = threading.Event(), []
    monkeypatch.setattr(shutdown, 'stop_owned_children', lambda: order.append('children'))
    controller = shutdown.Shutdown(lambda: False, lambda: pytest.fail('unfinished drain approved'),
        timeout=.05, emergency=lambda: order.append('emergency'), exit_process=lambda code: (order.append(code), expired.set()))
    controller.request()
    assert expired.wait(1)
    assert order == ['emergency', 'children', 1]
    assert not controller.finished.is_set()


def test_finish_exception_cannot_disarm_the_watchdog(monkeypatch):
    """A disposed native window or failed callback cannot strand Python's remaining threads."""
    expired = threading.Event()
    monkeypatch.setattr(shutdown, 'stop_owned_children', lambda: None)
    def broken():
        """Represent a native object disposed by the operating system before UI completion."""
        raise RuntimeError('disposed window')
    controller = shutdown.Shutdown(lambda: True, broken, timeout=.05, exit_process=lambda code: expired.set())
    controller.request()
    assert expired.wait(1)


def test_bind_user_close_waits_but_os_session_close_is_never_cancelled(monkeypatch):
    """Windows/Qt system termination can proceed while the shared bounded drain is running."""
    release, done = threading.Event(), threading.Event()
    controllers = []
    real = shutdown.Shutdown
    def create(*args, **kwargs):
        """Observe the actual controller without allowing a unit test to exit pytest."""
        item = real(*args, **kwargs, exit_process=lambda code: None)
        controllers.append(item)
        return item
    monkeypatch.setattr(shutdown, 'Shutdown', create)
    monkeypatch.setattr(shutdown, 'SignalPipe', lambda request: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(platforms, 'install_platform', lambda *args: None)
    monkeypatch.setattr(sys, 'platform', 'linux')
    window = SimpleNamespace(events=SimpleNamespace(closing=Events(), before_show=Events()), destroy=done.set)
    restore = shutdown.bind_native_shutdown(window, lambda: release.wait(1))
    try:
        assert window.events.closing[0]() is False
        controller = controllers[0]
        controller.system_ending = True
        assert window.events.closing[0]() is None
        release.set()
        assert done.wait(1)
        assert window.events.closing[0]() is None
    finally:
        release.set()
        restore()
        controllers[0].watchdog.cancel()
    assert not window.events.closing and not window.events.before_show


def test_windows_shutdown_bypasses_the_renderers_cancelable_close(monkeypatch):
    """WindowsShutDown never calls the user-close veto or sets Cancel=True."""
    reasons = SimpleNamespace(WindowsShutDown=1, UserClosing=2)
    monkeypatch.setitem(sys.modules, 'System.Windows.Forms', SimpleNamespace(CloseReason=reasons))
    calls = []
    session = platforms.WindowsSession.__new__(platforms.WindowsSession)
    session.controller = SimpleNamespace(system_ending=False, request=lambda: calls.append('drain'))
    session.previous = lambda sender, args: calls.append('user-close')
    args = SimpleNamespace(CloseReason=1, Cancel=True)
    session.closing(None, args)
    assert session.controller.system_ending and args.Cancel is False
    assert calls == ['drain']
    session.closing(None, SimpleNamespace(CloseReason=2))
    assert calls == ['drain', 'user-close']


def test_qt_session_never_requests_interaction_or_cancels_logout():
    """The session manager receives no cancel, phase-two or modal interaction requests."""
    calls = []
    session = platforms.QtSession.__new__(platforms.QtSession)
    session.controller = SimpleNamespace(system_ending=False, request=lambda: calls.append('drain'))
    session.ending(object())
    assert session.controller.system_ending and calls == ['drain']


@pytest.mark.skipif(sys.platform != 'darwin', reason='AppKit adapter')
def test_cocoa_delegate_returns_later_then_now_without_an_os_veto():
    """The real Objective-C selector supports integer TerminateLater, not a coerced boolean Cancel."""
    import AppKit
    from Foundation import NSObject
    calls = []
    controller = SimpleNamespace(finished=threading.Event(), request=lambda: calls.append('drain'))
    bridge = platforms.delegate_type().alloc().init()
    bridge.previous = NSObject.alloc().init()
    bridge.owner = SimpleNamespace(controller=controller, pending=False)
    assert bridge.applicationShouldTerminate_(None) == AppKit.NSTerminateLater
    assert bridge.owner.pending and calls == ['drain']
    controller.finished.set()
    assert bridge.applicationShouldTerminate_(None) == AppKit.NSTerminateNow
    assert bridge.respondsToSelector_('description')


def test_watchdog_exit_preserves_committed_data_and_stops_only_own_children(tmp_path):
    """A genuinely stalled worker exits in a subprocess; committed SQLite data and unrelated processes survive."""
    database, receipt = tmp_path/'saved.sqlite3', tmp_path/'child.json'
    package = shutdown.__package__
    code = '''
import json, sqlite3, subprocess, sys, threading
from PACKAGE.native_shutdown import Shutdown
with sqlite3.connect(sys.argv[1]) as db:
 db.execute('PRAGMA synchronous=FULL')
 db.execute('CREATE TABLE saved(value TEXT)')
 db.execute("INSERT INTO saved VALUES('accepted')")
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
open(sys.argv[2], 'w').write(json.dumps(child.pid))
Shutdown(lambda: threading.Event().wait(30), lambda: None, timeout=.3).request()
threading.Event().wait(30)
'''.replace('PACKAGE', package)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-c', code, str(database), str(receipt)], timeout=5)
        assert result.returncode == 1 and time.monotonic() - started < 5
        assert unrelated.poll() is None
        import sqlite3
        with sqlite3.connect(database) as db:
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert db.execute('SELECT value FROM saved').fetchone()[0] == 'accepted'
        child_pid = json.loads(receipt.read_text())
        for _ in range(30):
            if not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
            time.sleep(.02)
        else:
            pytest.fail('owned child survived the shutdown deadline')
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.skipif(sys.platform != 'darwin', reason='AppKit run-loop modes')
def test_cocoa_completion_is_delivered_inside_deferred_quit_mode():
    """A successful drain cannot hang because AppKit is running its nested modal quit loop."""
    import AppKit
    from Foundation import NSDate, NSRunLoop
    completed = threading.Event()
    platforms.cocoa_dispatch(completed.set)
    NSRunLoop.mainRunLoop().runMode_beforeDate_(AppKit.NSModalPanelRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(.2))
    assert completed.is_set()


@pytest.mark.skipif(sys.platform != 'darwin', reason='CoreFoundation signal source')
def test_native_signal_wakeup_does_not_need_an_idle_polling_timer():
    """TERM wakes the real native descriptor callback while Python is inside the Cocoa run loop."""
    import CoreFoundation as CF
    seen = threading.Event()
    pipe = shutdown.SignalPipe(seen.set)
    owner = platforms.CocoaSession.__new__(platforms.CocoaSession)
    owner.signals = pipe
    descriptor = CF.CFFileDescriptorCreate(None, pipe.read_fd, False, owner._signal_ready, None)
    source = CF.CFFileDescriptorCreateRunLoopSource(None, descriptor, 0)
    CF.CFRunLoopAddSource(CF.CFRunLoopGetMain(), source, CF.kCFRunLoopDefaultMode)
    CF.CFFileDescriptorEnableCallBacks(descriptor, CF.kCFFileDescriptorReadCallBack)
    try:
        sender = threading.Timer(.02, lambda: os.kill(os.getpid(), signal.SIGTERM))
        sender.start()
        CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, .1, False)
        sender.join()
        assert seen.is_set()
    finally:
        CF.CFFileDescriptorInvalidate(descriptor)
        pipe.close()


@pytest.mark.skipif(sys.platform != 'darwin', reason='AppKit delegate bridge')
def test_cocoa_delegate_is_installed_before_first_window_and_restored(monkeypatch):
    """An OS Quit during window startup must already use TerminateLater, not the renderer's veto."""
    from Foundation import NSObject
    previous = NSObject.alloc().init()
    app = SimpleNamespace(value=previous)
    app.delegate = lambda: app.value
    app.setDelegate_ = lambda value: setattr(app, 'value', value)
    backend = SimpleNamespace(_shared_app_delegate=None, AppDelegate=SimpleNamespace(alloc=lambda: SimpleNamespace(init=lambda: previous)))
    monkeypatch.setitem(sys.modules, 'webview.platforms.cocoa', SimpleNamespace(BrowserView=backend))
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=lambda: app),
                       NSTerminateNow=1, NSTerminateLater=2))
    controller = SimpleNamespace(finished=threading.Event(), request=lambda: None)
    session = platforms.CocoaSession(SimpleNamespace(), controller, SimpleNamespace(read_fd=None))
    assert app.delegate() is session.delegate
    assert backend._shared_app_delegate is session.delegate
    assert session.delegate.applicationShouldTerminate_(None) == 2
    session.close()
    assert app.delegate() is previous and backend._shared_app_delegate is previous


def test_failure_to_arm_watchdog_cannot_leave_a_deferred_quit_waiting(monkeypatch):
    """Thread exhaustion must allow immediate process termination rather than an unbounded OS quit delay."""
    calls = []
    class BrokenTimer:
        """Model a runtime that cannot allocate another timer thread."""
        def __init__(self, *args): pass
        def start(self): raise RuntimeError('cannot start new thread')
    monkeypatch.setattr(shutdown.threading, 'Timer', BrokenTimer)
    monkeypatch.setattr(shutdown, 'stop_owned_children', lambda: calls.append('children'))
    controller = shutdown.Shutdown(lambda: True, lambda: None, exit_process=calls.append)
    controller.request()
    assert calls == ['children', 1]
