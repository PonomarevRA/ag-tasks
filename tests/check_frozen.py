"""Verify actual frozen Python code and web resources against the current macOS build sources."""

import hashlib
import json
from pathlib import Path
from types import CodeType
from PyInstaller.archive.readers import CArchiveReader


def normalized(code):
    """Ignore only build-path filenames while retaining instructions, constants and line metadata."""
    constants = []
    for value in code.co_consts:
        constants.append(normalized(value) if isinstance(value, CodeType) else value)
    return code.replace(co_filename='source', co_consts=tuple(constants))


def main():
    """Fail on stale code/assets or unwanted runtime dependencies in the actual .app archive."""
    root = Path(__file__).resolve().parents[1]
    app = root/'dist/Ag Tasks.app'
    archive = CArchiveReader(str(app/'Contents/MacOS/AgTasks')).open_embedded_archive('PYZ.pyz')
    modules, assets = [], []
    for source in sorted((root/'ag_tasks').glob('*.py')):
        name = 'ag_tasks' if source.stem == '__init__' else 'ag_tasks.' + source.stem
        actual = archive.extract(name)
        expected = compile(source.read_bytes(), str(source), 'exec', dont_inherit=True)
        assert normalized(actual) == normalized(expected), name
        modules.append({'module': name, 'sourceSha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'compiledCodeMatches': True})
    for source in sorted((root/'web').glob('*')):
        target = app/'Contents/Resources/web'/source.name
        assert source.read_bytes() == target.read_bytes(), source.name
        assets.append(source.name)
    assert not any(name in {'ag_observer', 'atlas'} or name.startswith(('ag_observer.', 'atlas.')) for name in archive.toc)
    value = {'modules': modules, 'assets': assets, 'independentArchive': True, 'platform': 'macos-arm64'}
    (root/'evidence').mkdir(exist_ok=True)
    (root/'evidence/frozen-source-proof.json').write_text(json.dumps(value, indent=2))
    print(json.dumps({'compiledModulesMatch': len(modules), 'webAssetsMatch': len(assets), 'observerAtlasAbsent': True}))


if __name__ == '__main__':
    main()
