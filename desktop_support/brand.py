"""Resolve only the packaged service identity assets, independently of cwd and sibling projects."""
from pathlib import Path
import sys


def icon_path(name):
    """Return an existing owned icon or None for the normal platform fallback; callers cannot select arbitrary paths."""
    if name not in {'app.png', 'mark.png', 'app.ico'}:
        raise ValueError('Unknown application icon asset')
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    path = root / 'web' / 'brand' / name
    return path if path.is_file() else None
