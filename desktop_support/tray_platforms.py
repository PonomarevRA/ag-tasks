"""Small lazy native adapters using the renderer's existing desktop dependencies."""

import sys

from .brand import icon_path
from threading import Thread


def actions(handle):
    """Keep the same command vocabulary across menu-bar and tray implementations."""
    return [('Open ' + handle.title, handle.show), ('Agent requests', handle.activity),
            ('Storage locations', handle.storage), ('Check for updates…', handle.updates),
            ('Quit ' + handle.title, handle.on_quit)]


def install_tray(handle):
    """Select only the currently packaged renderer; missing tray support never hides windows."""
    if sys.platform == 'darwin':
        from PyObjCTools import AppHelper
        handle.dispatch = AppHelper.callAfter
        AppHelper.callAfter(CocoaTray, handle)
    elif sys.platform == 'win32':
        form = handle.window.native
        from System import Action
        handle.dispatch = lambda callback: form.BeginInvoke(Action(callback))
        handle.dispatch(lambda: WindowsTray(handle))
    else:
        QtTray.schedule(handle)


from .cocoa_tray import CocoaTray as SharedCocoaTray


class CocoaTray(SharedCocoaTray):
    """Retain product branding while sharing the ag-systems native menu."""
    def __init__(self, handle):
        """The application icon remains independent of the elected shared menu owner."""
        super().__init__(handle)
        import AppKit
        path = icon_path('app.png')
        if path:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(path))
            if image is not None:
                self.app.setApplicationIconImage_(image)


class WindowsTray:
    """Use Windows Forms NotifyIcon already present in pywebview's Edge renderer."""
    def __init__(self, handle):
        """Create and retain the icon on the Forms UI thread."""
        from System.Drawing import Icon, SystemIcons
        from System.Windows.Forms import NotifyIcon, ContextMenuStrip, ToolStripMenuItem
        self.icon = NotifyIcon()
        path = icon_path('app.ico')
        self.owned_icon = Icon(str(path)) if path else None
        self.icon.Icon = self.owned_icon if self.owned_icon is not None else SystemIcons.Application
        self.icon.Text = handle.title[:63]
        self.menu = ContextMenuStrip()
        self.callbacks = []
        for label, callback in actions(handle):
            item = ToolStripMenuItem(label)
            def invoke(sender, event, action=callback):
                """Do not run HTTP/update work on the Forms message pump."""
                Thread(target=action, daemon=True).start()
            self.callbacks.append(invoke)
            item.Click += invoke
            self.menu.Items.Add(item)
        self.icon.ContextMenuStrip = self.menu
        self.icon.Visible = True
        handle.native, handle.ready = self, not handle.closed
        if handle.closed:
            self.close()

    def close(self):
        """Dispose native resources while the Forms loop still exists."""
        self.icon.Visible = False
        self.icon.Dispose()
        self.menu.Dispose()
        if self.owned_icon is not None:
            self.owned_icon.Dispose()


class QtTray:
    """Use a queued QObject receiver so Linux tray objects belong to QApplication."""
    @staticmethod
    def schedule(handle):
        """Retain the bridge until installation/cleanup complete."""
        from qtpy.QtCore import QObject, Signal, Slot, Qt
        from qtpy.QtWidgets import QApplication
        class Dispatch(QObject):
            """Deliver native work on the application thread."""
            invoke = Signal(object)
            @Slot(object)
            def run(self, callback):
                """Execute a queued adapter callback."""
                callback()
        bridge = Dispatch()
        bridge.moveToThread(QApplication.instance().thread())
        bridge.invoke.connect(bridge.run, Qt.QueuedConnection)
        handle._qt_bridge = bridge
        handle.dispatch = bridge.invoke.emit
        handle.dispatch(lambda: QtTray(handle))

    def __init__(self, handle):
        """Register only when the desktop environment actually exposes a system tray."""
        from qtpy.QtWidgets import QSystemTrayIcon, QMenu, QApplication, QStyle
        from qtpy.QtGui import QIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        path = icon_path('app.png')
        image = QIcon(str(path)) if path else QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
        if path:
            QApplication.instance().setWindowIcon(image)
        self.icon = QSystemTrayIcon(image)
        self.icon.setToolTip(handle.title)
        self.menu = QMenu()
        for label, callback in actions(handle):
            action = self.menu.addAction(label)
            action.triggered.connect(lambda checked=False, callback=callback: Thread(target=callback, daemon=True).start())
        self.icon.setContextMenu(self.menu)
        self.icon.show()
        handle.native, handle.ready = self, not handle.closed
        if handle.closed:
            self.close()

    def close(self):
        """Delete GUI resources on their owning thread."""
        self.icon.hide()
        self.icon.deleteLater()
        self.menu.deleteLater()
