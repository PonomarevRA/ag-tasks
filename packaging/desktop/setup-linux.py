#!/usr/bin/env python3
"""Per-user Linux setup UI; frozen by the builder so installed Python is not required."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime, timezone


def canonical(value):
    """Reject aliases and relative paths before any application or storage mutation."""
    path = Path(value).expanduser()
    if not path.is_absolute() or path.resolve() != path or path == Path('/'):
        raise ValueError('Choose absolute paths without symbolic links.')
    for ancestor in (path, *path.parents):
        if (ancestor / '.git').exists() or ancestor.suffix == '.app':
            raise ValueError('Storage must be outside repositories and application bundles.')
    return path


def install(product, values, background, login):
    """Replace only inactive executables; retain old payload for rollback and all user data."""
    app, data, backup = [canonical(value) for value in values]
    for index, first in enumerate((app, data, backup)):
        for second in (app, data, backup)[index + 1:]:
            if first == second or first in second.parents or second in first.parents:
                raise ValueError('Application, data and backups must be separate folders.')
    if app.is_dir() and any(app.iterdir()):
        marker = app / '.ag-desktop-product.json'
        try:
            identity = json.loads(marker.read_text())
        except (OSError, ValueError):
            raise ValueError('Existing application has no installation identity. Choose an empty destination.') from None
        if identity.get('appId') != product['appId'] or identity.get('repository') != product['repository']:
            raise ValueError('The selected folder belongs to another product.')
    locator = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'ag-desktop' / product['appId'] / 'installation.json'
    previous = json.loads(locator.read_text()) if locator.exists() else None
    old_data = Path(previous['dataDirectory']) if previous else Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / product['defaultDataName']
    if old_data != data and old_data.is_dir() and any(old_data.iterdir()):
        raise ValueError('Existing data found at ' + str(old_data) + '. Keep that folder; migration is separate.')
    # /proc gives exact executable identity; never kill a process or adopt its port.
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            executable = (process / 'exe').resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if app == executable or app in executable.parents:
            raise ValueError('Quit the application from its tray icon before updating.')
    for path in (app.parent, data, backup, locator.parent):
        path.mkdir(parents=True, exist_ok=True)
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    staging = Path(tempfile.mkdtemp(prefix='.' + product['appId'] + '-', dir=app.parent))
    old = app.with_name(app.name + '.previous-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'))
    moved = False
    try:
        shutil.copytree(root / 'payload', staging, dirs_exist_ok=True, symlinks=True)
        if app.exists():
            app.rename(old)
            moved = True
        staging.rename(app)
        config = {'schemaVersion': 'ag-desktop/install-v1', 'appId': product['appId'],
                  'applicationPath': str(app), 'dataDirectory': str(data), 'backupDirectory': str(backup),
                  'closeToTray': background, 'launchAtLogin': login}
        if previous and 'preferredPort' in previous:
            port = previous['preferredPort']
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError('Invalid existing preferred port.')
            config['preferredPort'] = port
        temporary = locator.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(config, handle)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, locator)
    except Exception:
        if moved and old.exists():
            shutil.rmtree(app, ignore_errors=True)
            old.rename(app)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    executable = str(app / product['binary'])
    entry = '[Desktop Entry]\nType=Application\nName=' + product['name'] + '\nExec="' + executable.replace('"', '\\"') + '"\nTerminal=false\n'
    # PyInstaller keeps static assets inside _internal; retain the legacy flat layout fallback.
    icon = next((candidate for candidate in (app / '_internal/web/brand/app.png', app / 'web/brand/app.png') if candidate.is_file()), None)
    if icon is not None:
        entry += 'Icon=' + str(icon) + '\n'
    shortcut = Path.home() / '.local/share/applications' / (product['appId'] + '.desktop')
    shortcut.parent.mkdir(parents=True, exist_ok=True); shortcut.write_text(entry)
    autostart = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart' / shortcut.name
    if login:
        autostart.parent.mkdir(parents=True, exist_ok=True); autostart.write_text(entry)
    else:
        autostart.unlink(missing_ok=True)


def main():
    """Offer application, artifact, backup, tray and login choices using accessible native Tk controls."""
    root_path = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    product = json.loads((root_path / 'desktop-product.json').read_text())
    locator = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'ag-desktop' / product['appId'] / 'installation.json'
    previous = json.loads(locator.read_text()) if locator.exists() else {}
    window = tk.Tk(); window.title('Install ' + product['name'] + ' ' + product['version'])
    values = [tk.StringVar(value=previous.get('applicationPath', str(Path.home() / '.local/opt' / product['appId']))),
              tk.StringVar(value=previous.get('dataDirectory', str(Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / product['defaultDataName']))),
              tk.StringVar(value=previous.get('backupDirectory', str(Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / (product['defaultDataName'] + '-backups'))))]
    for index, label in enumerate(('Application', 'Artifacts and data', 'Backups')):
        tk.Label(window, text=label).grid(row=index, column=0, padx=12, pady=8, sticky='w')
        tk.Entry(window, textvariable=values[index], width=55).grid(row=index, column=1)
        def choose(value=values[index]):
            """A canceled folder dialog leaves the current path unchanged."""
            selected = filedialog.askdirectory()
            if selected:
                value.set(selected)
        tk.Button(window, text='Choose…', command=choose).grid(row=index, column=2, padx=12)
    background = tk.BooleanVar(value=previous.get('closeToTray', True))
    login = tk.BooleanVar(value=previous.get('launchAtLogin', False))
    tk.Checkbutton(window, text='Keep running in tray when window closes', variable=background).grid(row=3, columnspan=3, sticky='w', padx=12)
    tk.Checkbutton(window, text='Start when I sign in', variable=login).grid(row=4, columnspan=3, sticky='w', padx=12)
    def apply():
        """Report failures without falling back to another data directory."""
        try:
            install(product, [value.get() for value in values], background.get(), login.get())
            messagebox.showinfo(product['name'], 'Installed. Existing data was preserved.'); window.destroy()
        except Exception as error:
            messagebox.showerror('Setup failed', str(error))
    tk.Button(window, text='Install', command=apply).grid(row=5, column=2, padx=12, pady=12)
    window.mainloop()


if __name__ == '__main__':
    main()
