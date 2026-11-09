"""Independent ag-tasks identity and service-owned state paths; no observer dependency."""

import os
from pathlib import Path
import sys

from . import __version__

VERSION = __version__
STATUSES = ('backlog', 'ready', 'in_progress', 'blocked', 'in_review', 'rework', 'done', 'cancelled', 'stale')
PRIORITIES = ('low', 'normal', 'high', 'urgent')


def default_data_dir():
    """Resolve the native user state directory; an explicit override changes only this service's data."""
    override = os.environ.get('AG_TASKS_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    from desktop_support import configured_data_dir
    if sys.platform == 'darwin':
        legacy = Path.home() / 'Library/Application Support/AgTasks'
    elif os.name == 'nt':
        legacy = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'AgTasks'
    else:
        legacy = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'ag-tasks'
    return configured_data_dir('ag-tasks', legacy)
