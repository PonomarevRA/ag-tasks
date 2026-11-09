"""Native session-ending adapters; none asks the operating system to cancel shutdown."""

import logging
import sys

_delegate_class = None


def cocoa_dispatch(callback):
    """Deliver callbacks even inside AppKit's modal run loop used for deferred termination."""
    import AppKit
    import Foundation
    modes = [Foundation.NSRunLoopCommonModes, AppKit.NSModalPanelRunLoopMode]
    Foundation.NSRunLoop.mainRunLoop().performInModes_block_(modes, callback)


def delegate_type():
    """Forward the renderer delegate's other selectors; own only the termination decision."""
    global _delegate_class
    if _delegate_class is None:
        import AppKit
        import objc
        from Foundation import NSObject

        class StandaloneTerminationDelegate(NSObject):
            """Retain the existing delegate while taking responsibility for bounded asynchronous Quit."""

            def applicationShouldTerminate_(self, app):
                """Return Later only while draining, and always reply true when it finishes."""
                if self.owner.controller.finished.is_set():
                    return AppKit.NSTerminateNow
                self.owner.pending = True
                self.owner.controller.request()
                return AppKit.NSTerminateLater

            def respondsToSelector_(self, selector):
                """Preserve the previous delegate's optional application capabilities."""
                return (objc.super(StandaloneTerminationDelegate, self).respondsToSelector_(selector)
                        or self.previous.respondsToSelector_(selector))

            def forwardingTargetForSelector_(self, selector):
                """Leave open-file, restoration and other renderer lifecycle messages intact."""
                return self.previous

        _delegate_class = StandaloneTerminationDelegate
    return _delegate_class


class CocoaSession:
    """Bridge app Quit/logout, window close and a signal descriptor into one shutdown controller."""

    def __init__(self, window, controller, signals):
        """Install on AppKit's main thread before NSWindow; never replace renderer window delegates."""
        self.window, self.controller, self.signals = window, controller, signals
        self.app = self.delegate = self.descriptor = None
        self.pending = self.closed = False
        import threading
        if threading.current_thread() is threading.main_thread():
            self._install()
        else:
            cocoa_dispatch(self._install)

    def _install(self):
        """Retain the old app delegate and watch the signal pipe in normal and modal modes."""
        if self.closed:
            return
        import AppKit
        import CoreFoundation as CF
        from webview.platforms.cocoa import BrowserView
        self.app = AppKit.NSApplication.sharedApplication()
        previous = BrowserView._shared_app_delegate or BrowserView.AppDelegate.alloc().init()
        self.delegate = delegate_type().alloc().init()
        self.delegate.previous = previous
        self.delegate.owner = self
        # Install before the first NSWindow: the renderer reuses this retained delegate.
        BrowserView._shared_app_delegate = self.delegate
        self.app.setDelegate_(self.delegate)
        if self.signals.read_fd is not None:
            self.descriptor = CF.CFFileDescriptorCreate(None, self.signals.read_fd, False, self._signal_ready, None)
            self.source = CF.CFFileDescriptorCreateRunLoopSource(None, self.descriptor, 0)
            for mode in (CF.kCFRunLoopCommonModes, AppKit.NSModalPanelRunLoopMode):
                CF.CFRunLoopAddSource(CF.CFRunLoopGetMain(), self.source, mode)
            CF.CFFileDescriptorEnableCallBacks(self.descriptor, CF.kCFFileDescriptorReadCallBack)

    def _signal_ready(self, descriptor, kinds, context):
        """Read before rearming the one-shot descriptor callback, with no idle polling timer."""
        import CoreFoundation as CF
        self.signals.ready()
        CF.CFFileDescriptorEnableCallBacks(descriptor, CF.kCFFileDescriptorReadCallBack)

    def dispatch(self, callback):
        """Use modes served by both ordinary window events and deferred application termination."""
        cocoa_dispatch(callback)

    def request_quit(self):
        """Route red close through the app delegate so logout never sees a window-close veto."""
        cocoa_dispatch(lambda: self.app.terminate_(None) if self.app else self.controller.request())

    def finish(self):
        """Resolve a deferred Quit, or start normal app termination after a TERM/INT drain."""
        if self.app:
            if self.pending:
                self.pending = False
                self.app.replyToApplicationShouldTerminate_(True)
            else:
                self.app.terminate_(None)
        else:
            self.window.destroy()

    def close(self):
        """Invalidate the descriptor before the pipe closes and restore only our own delegate."""
        self.closed = True
        import CoreFoundation as CF
        if self.descriptor:
            CF.CFFileDescriptorInvalidate(self.descriptor)
        if self.app and self.app.delegate() == self.delegate:
            self.app.setDelegate_(self.delegate.previous)
        from webview.platforms.cocoa import BrowserView
        if BrowserView._shared_app_delegate == self.delegate:
            BrowserView._shared_app_delegate = self.delegate.previous


class WindowsSession:
    """Handle FormClosing.WindowsShutDown explicitly; generic close callbacks must not veto logout."""

    def __init__(self, window, controller, signals):
        """Wrap only this renderer form's closing event, retaining its ordinary user-close handling."""
        from System import Action
        self.form, self.controller = window.native, controller
        self.previous = self.form.on_closing
        def install():
            self.form.FormClosing -= self.previous
            self.form.FormClosing += self.closing
        if self.form.InvokeRequired:
            self.form.Invoke(Action(install))
        else:
            install()

    def closing(self, sender, args):
        """A system request never shows confirmation or sets Cancel; user close keeps normal behavior."""
        from System.Windows.Forms import CloseReason
        if args.CloseReason == CloseReason.WindowsShutDown:
            self.controller.system_ending = True
            self.controller.request()
            args.Cancel = False
        else:
            self.previous(sender, args)

    def dispatch(self, callback):
        """Marshal completion to the form's own UI thread without blocking the worker."""
        from System import Action
        self.form.BeginInvoke(Action(callback))

    def finish(self):
        """Close this form only; the renderer owns application-loop teardown."""
        self.form.Close()

    def close(self):
        """Detach the form-specific subscription; no static SystemEvents handlers survive."""
        if not self.form.IsDisposed:
            self.form.FormClosing -= self.closing
            self.form.FormClosing += self.previous


class QtSession:
    """Use Qt's session-management signal plus POSIX wakeup FD; no interaction or cancel requests."""

    def __init__(self, window, controller, signals):
        """Install through a queued renderer signal because shown handlers run outside the Qt thread."""
        from qtpy.QtCore import QObject, Signal, Slot, Qt, QThread
        from qtpy.QtWidgets import QApplication
        self.window, self.controller, self.signals = window, controller, signals
        self.notifier = None
        self.installed = self.closed = False
        class Dispatch(QObject):
            """A QObject supplies queued main-thread execution for native installation and finish."""
            invoke = Signal(object)
            @Slot(object)
            def run(self, callback):
                """Execute callbacks on the QObject's Qt application thread."""
                callback()
        self.app = QApplication.instance()
        self.bridge = Dispatch()
        self.bridge.moveToThread(self.app.thread())
        self.bridge.invoke.connect(self.bridge.run, Qt.QueuedConnection)
        if QThread.currentThread() == self.app.thread():
            self._install()
        else:
            self.dispatch(self._install)

    def _install(self):
        """Connect session notifications and a readable socket notifier without idle polling."""
        if self.closed:
            return
        from qtpy.QtCore import QSocketNotifier, Qt
        self.installed = True
        self.app.commitDataRequest.connect(self.ending, Qt.DirectConnection)
        self.app.aboutToQuit.connect(self.controller.request)
        self.notifier = None
        if self.signals.read_fd is not None:
            self.notifier = QSocketNotifier(self.signals.read_fd, QSocketNotifier.Read)
            self.notifier.activated.connect(lambda *_: self.signals.ready())

    def ending(self, manager):
        """Do not call allowsInteraction, cancel, or requestPhase2 while the desktop session ends."""
        self.controller.system_ending = True
        self.controller.request()

    def dispatch(self, callback):
        """Queue work on the application thread using a retained QObject receiver."""
        self.bridge.invoke.emit(callback)

    def finish(self):
        """Use the renderer's normal window-close path after graceful cleanup."""
        self.window.destroy()

    def close(self):
        """Detach session notifications and disable the notifier before closing its file descriptor."""
        self.closed = True
        if not self.installed:
            return
        self.app.commitDataRequest.disconnect(self.ending)
        self.app.aboutToQuit.disconnect(self.controller.request)
        if self.notifier:
            self.notifier.setEnabled(False)


def install_platform(window, controller, signals):
    """Choose the packaged renderer's native adapter; unsupported renderers retain bounded close/signals."""
    if sys.platform == 'darwin':
        return CocoaSession(window, controller, signals)
    if sys.platform == 'win32':
        return WindowsSession(window, controller, signals)
    try:
        return QtSession(window, controller, signals)
    except ImportError:
        logging.getLogger(__name__).warning('Native session adapter unavailable; using bounded close and signals')
        return None
