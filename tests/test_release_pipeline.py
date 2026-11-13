"""The publishing validator accepts only the documented notes and exact native assets."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRODUCT = json.loads((ROOT / 'packaging/desktop-product.json').read_text(encoding='utf-8'))
TAG = 'v' + PRODUCT['version']


def validate(*arguments, cwd=ROOT):
    """Exercise the exact workflow CLI without any access to GitHub."""
    return subprocess.run([sys.executable, str(cwd / 'packaging/release.py'), *arguments],
                          cwd=cwd, capture_output=True, text=True, encoding='utf-8')


def test_wrong_tag_is_rejected():
    """A tag that would advertise an unrelated package cannot pass validation."""
    result = validate('--tag', 'v0.0.0')
    assert result.returncode != 0
    assert 'Release tag must match' in result.stderr


@pytest.mark.parametrize('mutation', ['duplicate', 'markup', 'backslash', 'long', 'missing'])
def test_changelog_shape_is_enforced(tmp_path, mutation):
    """Malformed and ambiguous user-facing notes fail before native builds."""
    for filename in ('packaging/release.py', 'packaging/build_installer.py', 'packaging/ag_tasks.spec',
                     'packaging/desktop-product.json', 'pyproject.toml', 'ag_tasks/__init__.py'):
        target = tmp_path / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / filename).read_bytes())
    heading = '## [' + PRODUCT['version'] + ']\n'
    body = '### Русский\n\n- Полезное изменение.\n\n### English\n\n- Helpful change.\n'
    if mutation == 'duplicate':
        body += '\n' + heading + body
    if mutation == 'markup':
        body = body.replace('Helpful change.', '[Link](https://example.invalid)')
    if mutation == 'backslash':
        body = body.replace('Helpful change.', r'Path C:\not-a-public-note')
    if mutation == 'long':
        body = body.replace('Helpful change.', 'x' * 161)
    if mutation == 'missing':
        body = body.split('### English')[0]
    (tmp_path / 'CHANGELOG.md').write_text(heading + '\n' + body, encoding='utf-8')
    assert validate('--tag', TAG, cwd=tmp_path).returncode != 0


def make_installers(directory):
    """Create small distinguishable stand-ins for the three runner artifacts."""
    directory.mkdir()
    names = []
    for platform, suffix, machine in (
        ('darwin', 'macos-arm64.dmg', 'arm64'),
        ('win32', 'windows-x64.exe', 'AMD64'),
        ('linux', 'linux-x64.tar.gz', 'x86_64'),
    ):
        name = f"ag-tasks-{PRODUCT['version']}-{suffix}"
        names.append(name)
        payload = ('installer for ' + platform).encode()
        digest = hashlib.sha256(payload).hexdigest()
        (directory / name).write_bytes(payload)
        (directory / (name + '.sha256')).write_text(f'{digest}  {name}\n')
        metadata = {'schemaVersion': 'ag-desktop/release-v1', 'appId': PRODUCT['appId'],
                    'version': PRODUCT['version'], 'platform': platform, 'architecture': machine,
                    'asset': name, 'sha256': digest, 'repository': PRODUCT['repository']}
        (directory / (name + '.json')).write_text(json.dumps(metadata))
    return names


def test_valid_installers_and_short_notes_are_staged(tmp_path):
    """Publish exactly one asset set and matching checksums, even if inputs contain other files."""
    artifacts = tmp_path / 'artifacts'
    output = tmp_path / 'stage'
    names = make_installers(artifacts)
    (artifacts / 'unrelated.json').write_text('{}')
    result = validate('--tag', TAG, '--artifacts', str(artifacts), '--output', str(output))
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (output / 'upload').iterdir()) == sorted(
        names + [name + '.json' for name in names] + ['SHA256SUMS'])
    sums = ''.join((artifacts / (name + '.sha256')).read_text() for name in names)
    assert (output / 'upload/SHA256SUMS').read_text() == sums
    notes = (output / 'notes.md').read_text(encoding='utf-8')
    assert [line for line in notes.splitlines() if line.startswith('#')] == ['## Русский', '## English']


@pytest.mark.parametrize('corruption', ['missing', 'digest', 'version', 'platform', 'checksum'])
def test_incomplete_or_mismatched_installer_set_cannot_stage(tmp_path, corruption):
    """A failed native runner or mismatched download never produces an upload directory."""
    artifacts = tmp_path / 'artifacts'
    output = tmp_path / 'stage'
    name, *_ = make_installers(artifacts)
    metadata_path = artifacts / (name + '.json')
    if corruption == 'missing':
        (artifacts / name).unlink()
    if corruption == 'digest':
        (artifacts / name).write_bytes(b'corrupt installer')
    if corruption == 'checksum':
        (artifacts / (name + '.sha256')).write_text('different checksum')
    if corruption in ('version', 'platform'):
        metadata = json.loads(metadata_path.read_text())
        metadata[corruption] = 'incorrect'
        metadata_path.write_text(json.dumps(metadata))
    result = validate('--tag', TAG, '--artifacts', str(artifacts), '--output', str(output))
    assert result.returncode != 0
    assert not output.exists()
