"""Native menu structure and bridge ownership without requiring a live window server."""
from types import SimpleNamespace
import sys

from ag_tasks import native_menu


class NativeObject:
    """Implement Cocoa allocation used by the menu adapter while retaining explicit properties."""
    @classmethod
    def alloc(cls):
        """Allocate an isolated adapter object."""
        return cls()

    def init(self):
        """Return the allocated object as Cocoa does."""
        return self

    def initWithTitle_(self, title):
        """Initialize a native menu's observable title and ordered children."""
        self.title, self.items = title, []
        return self

    def initWithTitle_action_keyEquivalent_(self, title, action, key):
        """Retain selector and shortcut; a missing target uses the responder chain."""
        self.title, self.action, self.key, self.target = title, action, key, None
        return self

    def setSubmenu_(self, value): self.submenu = value
    def addItem_(self, value): self.items.append(value)
    def numberOfItems(self): return len(self.items)
    def removeItemAtIndex_(self, index): self.items.pop(index)
    def itemAtIndex_(self, index): return self.items[index]
    def setTitle_(self, value): self.title = value
    def setTarget_(self, value): self.target = value
    def setTag_(self, value): self.tag_value = value
    def setKeyEquivalentModifierMask_(self, value): self.mask = value
    def update(self): pass

    def addItemWithTitle_action_keyEquivalent_(self, title, selector, key):
        """Append one targetless native action in declaration order."""
        item = NativeObject().initWithTitle_action_keyEquivalent_(title, selector, key)
        self.items.append(item)
        return item


def test_responder_menus_preserve_application_menu_and_survive_reload(monkeypatch):
    """Edit/Window commands stay targetless and reloading does not register a duplicate Objective-C class."""
    main = NativeObject().initWithTitle_('main')
    application = NativeObject().initWithTitle_action_keyEquivalent_('app', None, '')
    main.addItem_(application)
    app = SimpleNamespace(mainMenu=lambda: main, setWindowsMenu_=lambda menu: None, setHelpMenu_=lambda menu: None)
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSApplication=SimpleNamespace(sharedApplication=lambda: app), NSMenu=NativeObject, NSMenuItem=NativeObject, NSControlKeyMask=1, NSCommandKeyMask=2))
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSObject=NativeObject))
    monkeypatch.setattr(native_menu, '_target_class', None)
    window = SimpleNamespace()
    for _ in range(2):
        native_menu._install_on_main(window, 'Tasks', (('Sources', lambda: None),), (('Help', lambda: None),))
        assert main.items[0] is application
        assert [item.title for item in main.items[1:]] == ['File', 'Edit', 'View', 'Window', 'Help']
        assert all(item.target is None for item in main.items[2].submenu.items)
        assert [item.action for item in main.items[2].submenu.items] == ['undo:', 'redo:', 'cut:', 'copy:', 'paste:', 'selectAll:']
        assert main.items[3].submenu.items[0].title == 'Reload'
        assert main.items[3].submenu.items[1].mask == 3
        assert len(window._standalone_menu_target.callbacks) == 3


def test_other_platforms_do_not_import_cocoa(monkeypatch):
    """Headless and non-macOS launchers retain their existing platform behavior."""
    monkeypatch.setattr(sys, 'platform', 'linux')
    native_menu.install_native_menu(None, 'Tasks')
