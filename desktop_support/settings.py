"""Resolve installer-owned locations without coupling services to each other."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

APP_IDS = frozenset({'code-atlas', 'agmemory', 'ag-observer', 'ag-tasks', 'ag-design'})


@dataclass(frozen=True)
class Installation:
    """Validated per-user installation settings; runtime CLI/env overrides remain caller-owned."""
    app_id: str
    data_directory: Path
    backup_directory: Path
    application_path: Path | None = None
    close_to_tray: bool = True
    launch_at_login: bool = False
    preferred_port: int | None = None


def installation_path(app_id, *, platform=None, environ=None, home=None):
    """Locate config outside replaceable bundles and movable data directories."""
    if app_id not in APP_IDS:
        raise ValueError('Unknown desktop application')
    platform = platform or sys.platform
    environment = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == 'darwin':
        root = home / 'Library/Application Support/AGDesktop'
    elif platform == 'win32':
        root = Path(environment.get('LOCALAPPDATA') or home / 'AppData/Local') / 'AGDesktop'
    else:
        root = Path(environment.get('XDG_CONFIG_HOME') or home / '.config') / 'ag-desktop'
    if not root.is_absolute():
        raise ValueError('Desktop configuration root must be absolute')
    return root / app_id / 'installation.json'


def _absolute(value):
    """Reject ambiguous paths and symlink aliases before they can select another store."""
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ValueError('Invalid installation path')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('Installation paths must be absolute')
    if path.resolve() != path.absolute():
        raise ValueError('Installation paths must be canonical without symbolic links')
    return path


def read_installation(app_id, default_data, default_backup=None, *, platform=None, environ=None, home=None):
    """Fail visibly on corrupt config rather than silently opening a different empty database."""
    data = Path(default_data)
    backup = Path(default_backup) if default_backup is not None else data.parent / (data.name + '-backups')
    path = installation_path(app_id, platform=platform, environ=environ, home=home)
    if path.is_symlink():
        raise ValueError("Unsafe installation configuration")
    if not path.exists():
        return Installation(app_id, data, backup)
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError('Unsafe installation configuration')
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('schemaVersion') != 'ag-desktop/install-v1' or value.get('appId') != app_id:
        raise ValueError('Unsupported installation configuration')
    data = _absolute(value.get('dataDirectory'))
    backup = _absolute(value.get('backupDirectory'))
    application = _absolute(value.get('applicationPath'))
    paths = (data, backup, application)
    for index, parent in enumerate(paths):
        for child in paths[index + 1:]:
            if parent == child or parent in child.parents or child in parent.parents:
                raise ValueError('Application, artifacts and backups must be separate directories')
    for name in ('closeToTray', 'launchAtLogin'):
        if name in value and not isinstance(value[name], bool):
            raise ValueError('Installation flags must be boolean')
    port = value.get('preferredPort')
    if port is not None and (type(port) is not int or not 1024 <= port <= 65535):
        raise ValueError('Invalid preferred port')
    return Installation(app_id, data, backup, application, value.get('closeToTray', True),
                        value.get('launchAtLogin', False), port)


def configured_data_dir(app_id, default_data):
    """Return installer storage when no explicit service CLI/env override was supplied."""
    return read_installation(app_id, default_data).data_directory


def configured_backup_dir(app_id, default_data):
    """Return backup storage without creating directories or touching a live database."""
    return read_installation(app_id, default_data).backup_directory
