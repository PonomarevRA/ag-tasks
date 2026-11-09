"""Native macOS responder menus; safe to copy into another standalone desktop package.

AppKit is imported lazily after the native window exists. The caller
owns its local navigation callbacks; no JavaScript evaluation or service imports
are required. Non-macOS callers retain their renderer's normal menus.
"""

import logging
import sys
from threading import Thread

from .native_shutdown import bind_native_shutdown


# Native selectors deliberately have no target: AppKit resolves the current first
# responder and automatically disables editing/window actions when unavailable.
_target_class = None

RESPONDER_MENUS = (
    ("File", (("Close Window", "performClose:", "w"),)),
    ("Edit", (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"),
              ("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
              ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a"))),
    ("View", (("Enter Full Screen", "toggleFullScreen:", "f"),)),
    ("Window", (("Minimize", "performMiniaturize:", "m"),
                ("Zoom", "performZoom:", ""), ("Bring All to Front", "arrangeInFront:", ""))),
    ("Help", ()),
)


def install_native_menu(window, app_name: str, *, file_actions=(), help_actions=(), sidebar_action=None, view_actions=()) -> None:
    """Schedule standard menus after pywebview creates its NSWindow; callbacks run off the Cocoa thread.

    file_actions/help_actions contain (title, zero-argument callable) pairs owned
    by the application. Reinstallation after page navigation is idempotent.
    """
    if sys.platform != "darwin":
        return
    from PyObjCTools import AppHelper
    AppHelper.callAfter(_install_on_main, window, app_name, file_actions, help_actions, sidebar_action, view_actions)


def _install_on_main(window, app_name, file_actions, help_actions, sidebar_action=None, view_actions=()) -> None:
    """Keep pywebview's application menu and replace the remaining top-level menus on Cocoa's main thread."""
    import AppKit
    app = AppKit.NSApplication.sharedApplication()
    menu = app.mainMenu()
    if menu is None or menu.numberOfItems() == 0:
        raise RuntimeError("Native application menu is unavailable")
    while menu.numberOfItems() > 1:
        menu.removeItemAtIndex_(1)
    target = _callback_target_type().alloc().init()
    target.callbacks = []
    # NSMenuItem targets are weak; retain the bridge for this window's lifetime.
    window._standalone_menu_target = target
    for title, actions in RESPONDER_MENUS:
        submenu = AppKit.NSMenu.alloc().initWithTitle_(title)
        parent = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        parent.setSubmenu_(submenu)
        menu.addItem_(parent)
        custom = file_actions if title == "File" else help_actions if title == "Help" else ()
        for label, callback in custom:
            item = submenu.addItemWithTitle_action_keyEquivalent_(label, "invoke:", "")
            item.setTarget_(target)
            item.setTag_(len(target.callbacks))
            target.callbacks.append(callback)
        if title == "View":
            for label, callback in view_actions:
                _add_callback(submenu, target, label, callback)
            if sidebar_action is not None:
                toggle = _add_callback(submenu, target, "Toggle Sidebar", sidebar_action, "s")
                toggle.setKeyEquivalentModifierMask_(AppKit.NSCommandKeyMask | AppKit.NSShiftKeyMask)
            _add_callback(submenu, target, "Reload", lambda: window.load_url(window.get_current_url()), "r")
        for label, selector, shortcut in actions:
            item = submenu.addItemWithTitle_action_keyEquivalent_(label, selector, shortcut)
            if selector == "toggleFullScreen:":
                item.setKeyEquivalentModifierMask_(AppKit.NSControlKeyMask | AppKit.NSCommandKeyMask)
        if title == "Window":
            app.setWindowsMenu_(submenu)
        elif title == "Help":
            app.setHelpMenu_(submenu)
    menu.itemAtIndex_(0).setTitle_(app_name)
    menu.update()


def _add_callback(menu, target, title, callback, key=""):
    """Attach a retained callback and return its NSMenuItem for native shortcut configuration."""
    item = menu.addItemWithTitle_action_keyEquivalent_(title, "invoke:", key)
    item.setTarget_(target)
    item.setTag_(len(target.callbacks))
    target.callbacks.append(callback)
    return item


def _invoke(callback) -> None:
    """Keep callback failures observable without killing the Cocoa event loop."""
    try:
        callback()
    except Exception:
        logging.getLogger(__name__).exception("Native menu action failed")


def _callback_target_type():
    """Register one Objective-C bridge class per process, including after page reloads."""
    global _target_class
    if _target_class is None:
        from Foundation import NSObject

        class StandaloneMenuTarget(NSObject):
            """Retain explicit callbacks; native selectors continue through the first responder."""

            def invoke_(self, sender):
                """Run modal-capable callbacks off the Cocoa thread to avoid picker deadlocks."""
                callback = self.callbacks[int(sender.tag())]
                Thread(target=_invoke, args=(callback,), name="native-menu-action", daemon=True).start()

        _target_class = StandaloneMenuTarget
    return _target_class
