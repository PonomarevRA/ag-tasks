"""Verify that frozen bundles contain the same OS renderer used by the desktop launcher."""

from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('platform,renderer', [('darwin', 'cocoa'), ('win32', 'winforms'), ('linux', 'qt')])
def test_selected_native_renderer_is_not_excluded(monkeypatch, platform, renderer):
    """Evaluate the real spec without freezing: an excluded renderer can never receive shutdown events."""
    spec = Path(__file__).resolve().parents[1] / 'packaging/ag_tasks.spec'
    hooks = SimpleNamespace(collect_data_files=lambda name: [], collect_submodules=lambda name: [], copy_metadata=lambda name: [])
    monkeypatch.setitem(sys.modules, 'PyInstaller.utils.hooks', hooks)
    monkeypatch.setattr(sys, 'platform', platform)
    captured = {}
    def analysis(*args, **kwargs):
        """Capture the actual dependency policy passed to the freezer."""
        captured.update(kwargs)
        return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
    no_op = lambda *args, **kwargs: None
    runpy.run_path(str(spec), init_globals={'SPECPATH': str(spec.parent), 'Analysis': analysis,
                                          'PYZ': no_op, 'EXE': no_op, 'COLLECT': no_op, 'BUNDLE': no_op})
    selected = 'webview.platforms.' + renderer
    assert selected in captured['hiddenimports'] and selected not in captured['excludes']
    if platform == 'linux':
        assert 'PyQt6' not in captured['excludes']
    if platform == 'win32':
        assert 'webview.platforms.edgechromium' in captured['hiddenimports']
