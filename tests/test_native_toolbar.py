"""Capability fallback stays accessible when optional native toolbar APIs are unavailable."""
from types import SimpleNamespace
import sys

from ag_tasks import native_toolbar


def test_unsupported_platform_does_not_import_cocoa(monkeypatch):
    """Headless/non-macOS renderers keep their ordinary web sidebar control."""
    monkeypatch.setattr(sys, 'platform', 'linux')
    native_toolbar.install_native_toolbar(None, lambda: None)


def test_missing_native_symbol_never_hides_browser_fallback(monkeypatch):
    """A missing system icon is an unavailable native capability, never a fake success."""
    markers = []
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSImage=SimpleNamespace(imageWithSystemSymbolName_accessibilityDescription_=lambda *args: None)))
    monkeypatch.setattr(native_toolbar, '_mark_capability', lambda *args: markers.append(args))
    native_toolbar._install_on_main(SimpleNamespace(), lambda: None)
    assert not markers
