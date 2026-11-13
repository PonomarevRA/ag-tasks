# Native independent standalone bundle; no dependency on Atlas, Observer or external Python.
from pathlib import Path
import sys
import ssl
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
# Include the renderer selected by desktop.py, and omit unused native GUI stacks.
gui_imports = {'darwin': ['webview.platforms.cocoa'],
               'win32': ['webview.platforms.winforms', 'webview.platforms.edgechromium']}.get(sys.platform, ['webview.platforms.qt'])
gui_excludes = ['webview.platforms.gtk', 'PyQt5', 'PySide2', 'PySide6']
if sys.platform != 'linux':
    gui_excludes += ['PyQt6', 'webview.platforms.qt']
if sys.platform != 'darwin':
    gui_excludes += ['webview.platforms.cocoa']
if sys.platform != 'win32':
    gui_excludes += ['webview.platforms.winforms', 'webview.platforms.edgechromium']
hiddenimports = collect_submodules('ag_tasks') + collect_submodules('desktop_support') + collect_submodules('service_telemetry') + gui_imports + [
    'uvicorn.logging', 'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on', 'sqlite3', '_sqlite3',
]
datas = [(str(root / 'web'), 'web')] + collect_data_files('webview')
# The bundled Python runtime may load certificate policy beside ssl.py. Keep that
# policy in frozen builds rather than disabling TLS verification in the MCP client.
ssl_policy = Path(ssl.__file__).parent / 'codex-rejected-cas.pem'
if ssl_policy.is_file():
    datas.append((str(ssl_policy), '.'))
for name in ['fastapi', 'uvicorn', 'httpx', 'pywebview']:
    datas += copy_metadata(name)
analysis = Analysis([str(root / 'packaging/entrypoint.py')], pathex=[str(root)], datas=datas,
    binaries=[], hiddenimports=hiddenimports,
    excludes=gui_excludes + ['atlas', 'ag_observer', 'pytest', 'IPython', 'jupyter', 'notebook', 'tkinter', 'torch',
              'langgraph', 'langchain', 'langchain_core'], noarchive=False)
archive = PYZ(analysis.pure)
executable = EXE(archive, analysis.scripts, [], exclude_binaries=True, name='AgTasks', console=True,
                 icon=str(root / 'packaging/icons/app.ico') if sys.platform == 'win32' else None,
                 target_arch=None, codesign_identity=None, entitlements_file=None)
collection = COLLECT(executable, analysis.binaries, analysis.datas, name='AgTasks')
if sys.platform == 'darwin':
    bundle = BUNDLE(collection, icon=str(root / 'packaging/icons/AppIcon.icns'), name='Ag Tasks.app', bundle_identifier='local.ag-tasks.desktop', version='0.8.4',
        info_plist={'CFBundleDisplayName': 'ag-tasks', 'NSHighResolutionCapable': True,
                    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}})
