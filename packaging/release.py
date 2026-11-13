"""Validate release inputs and stage only the three expected installers and their manifests."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tomllib

from build_installer import read_product


ROOT = Path(__file__).resolve().parents[1]
VERSION = r'(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})'
LANGUAGES = ('Русский', 'English')
INSTALLERS = (
    ('macos-arm64.dmg', 'darwin', {'arm64', 'aarch64'}),
    ('windows-x64.exe', 'win32', {'amd64', 'x86_64'}),
    ('linux-x64.tar.gz', 'linux', {'amd64', 'x86_64'}),
)


def release_notes(changelog, version):
    """Extract the tagged bilingual section as plain, bounded GitHub release notes."""
    heading = f'## [{version}]'
    lines = changelog.splitlines()
    if lines.count(heading) != 1:
        raise ValueError(f'Expected exactly one changelog entry for {version}')
    language = None
    bullets = {}
    for line in lines[lines.index(heading) + 1:]:
        if line.startswith('## '):
            break
        if not line.strip():
            continue
        if line.startswith('### ') and line[4:] in LANGUAGES and line[4:] not in bullets:
            language = line[4:]
            bullets[language] = []
            continue
        if language is None or not line.startswith('- '):
            raise ValueError(f'Unexpected changelog line for {version}')
        sentence = line[2:]
        if (not 1 <= len(sentence) <= 160 or sentence != sentence.strip()
                or not sentence.isprintable() or any(char in sentence for char in '<>[]`*@\\')
                or '://' in sentence):
            raise ValueError('Release notes must be short plain-text bullets')
        bullets[language].append(line)
    if tuple(bullets) != LANGUAGES or any(not 1 <= len(lines) <= 5 for lines in bullets.values()):
        raise ValueError(f'Expected 1–5 Russian and English bullets for {version}')
    return '\n\n'.join('\n'.join((f'## {name}', *lines)) for name, lines in bullets.items()) + '\n'


def validate_version(root, product, tag):
    """Check every shipped version string before any runner starts building."""
    version = product['version']
    if not re.fullmatch(VERSION, version) or tag is not None and tag != f'v{version}':
        raise ValueError('Release tag must match the stable product version')
    metadata = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    module = (root / 'ag_tasks/__init__.py').read_text(encoding='utf-8')
    spec = (root / 'packaging/ag_tasks.spec').read_text(encoding='utf-8')
    if (metadata['project']['version'] != version
            or re.findall(r'^__version__ = "(.*?)"$', module, re.M) != [version]
            or re.findall(r"\bversion='([^']*)'", spec) != [version]):
        raise ValueError('Package, runtime, installer and native bundle versions differ')
    return release_notes((root / 'CHANGELOG.md').read_text(encoding='utf-8'), version)


def verify_installers(artifacts, product):
    """Validate payload identity and digest before exposing any file for upload."""
    version = product['version']
    entries = {}
    for suffix, platform, machines in INSTALLERS:
        name = f"{product['appId']}-{version}-{suffix}"
        asset = artifacts / name
        metadata = json.loads((artifacts / (name + '.json')).read_text(encoding='utf-8'))
        with asset.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        expected = {'schemaVersion': 'ag-desktop/release-v1', 'appId': product['appId'],
                    'version': version, 'platform': platform, 'asset': name, 'sha256': digest,
                    'repository': product['repository']}
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ValueError(f'Invalid installer metadata for {name}')
        if metadata.get('architecture', '').lower() not in machines:
            raise ValueError(f'Unexpected installer architecture for {name}')
        checksum = f'{digest}  {name}\n'
        if (artifacts / (name + '.sha256')).read_text(encoding='utf-8') != checksum:
            raise ValueError(f'Installer checksum differs for {name}')
        entries[name] = checksum
    return entries


def stage_release(artifacts, output, product, notes):
    """Copy only checked assets into a fresh output folder; leave notes outside upload glob."""
    entries = verify_installers(artifacts, product)
    output.mkdir(parents=True)
    upload = output / 'upload'
    upload.mkdir()
    for name in entries:
        for filename in (name, name + '.json'):
            shutil.copyfile(artifacts / filename, upload / filename)
    (upload / 'SHA256SUMS').write_text(''.join(entries.values()), encoding='utf-8')
    (output / 'notes.md').write_text(notes, encoding='utf-8')


def main():
    """Validate for all builds, or stage an exact tagged release from native artifacts."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', help='Exact tag ref name, for example v0.8.3')
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if bool(args.artifacts) != bool(args.output) or args.artifacts and not args.tag:
        parser.error('Release staging requires --tag, --artifacts and --output')
    product = read_product(ROOT / 'packaging/desktop-product.json')
    notes = validate_version(ROOT, product, args.tag)
    if args.artifacts:
        stage_release(args.artifacts, args.output, product, notes)


if __name__ == '__main__':
    main()
