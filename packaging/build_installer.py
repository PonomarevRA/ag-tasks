"""Build target-native installers from an already tested self-contained app payload."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile

REQUIRED = {'appId', 'name', 'version', 'binary', 'bundleId', 'repository', 'defaultDataName'}
APP_IDS = {'code-atlas', 'agmemory', 'ag-observer', 'ag-tasks', 'ag-design'}


def read_product(path):
    """Validate build-time identity; product metadata never supplies shell commands."""
    value = json.loads(Path(path).read_text())
    if not REQUIRED <= value.keys() or value['appId'] not in APP_IDS:
        raise ValueError('Incomplete desktop product manifest')
    import re
    if not re.fullmatch(r'\d+\.\d+\.\d+', value['version']):
        raise ValueError('Version must be stable semver')
    for key in REQUIRED:
        if not isinstance(value[key], str) or not value[key] or any(char in value[key] for char in '\n\r\x00"'):
            raise ValueError('Invalid product field: ' + key)
    for key in ('binary', 'defaultDataName', 'appId'):
        if '/' in value[key] or '\\' in value[key] or value[key] in {'.', '..'}:
            raise ValueError('Product filenames must be simple names')
    if value['repository'] != 'PonomarevRA/' + {'agmemory': 'ag-memmory'}.get(value['appId'], value['appId']):
        raise ValueError('Release repository does not match product identity')
    return value


def run(arguments, **kwargs):
    """Execute fixed build tools with literal argument boundaries."""
    subprocess.run([str(arg) for arg in arguments], check=True, **kwargs)


def macos(product, source, output, templates, widgets_source=None):
    """Package an ad-hoc signed Setup.app containing a self-contained application payload."""
    binary = source / 'Contents/MacOS' / product['binary']
    if not binary.is_file():
        raise ValueError('macOS payload executable is missing')
    with tempfile.TemporaryDirectory(prefix='ag-setup-') as folder:
        image = Path(folder) / 'image'
        setup = image / (product['name'] + ' Setup.app')
        contents = setup / 'Contents'
        resources = contents / 'Resources'
        (contents / 'MacOS').mkdir(parents=True)
        resources.mkdir()
        source_info = plistlib.loads((source / 'Contents/Info.plist').read_bytes())
        if source_info.get('CFBundleIdentifier') != product['bundleId']:
            raise ValueError('Payload bundle identity does not match product manifest')
        run(['/usr/bin/ditto', source, resources / 'Payload' / (product['name'] + '.app')])
        if widgets_source is not None:
            widget_info = plistlib.loads((widgets_source / 'Contents/Info.plist').read_bytes())
            if widget_info.get('CFBundleIdentifier') != 'io.github.ponomarevra.agdesktop.widgets':
                raise ValueError('Unexpected widget companion identity')
            run(['/usr/bin/codesign', '--verify', '--deep', '--strict', widgets_source])
            run(['/usr/bin/ditto', widgets_source, resources / 'Widgets/AG Requests Widgets.app'])
        (resources / 'desktop-product.json').write_text(json.dumps(product))
        info = {'CFBundleExecutable': 'AGSetup', 'CFBundleIdentifier': product['bundleId'] + '.setup',
                'CFBundleName': product['name'] + ' Setup', 'CFBundlePackageType': 'APPL',
                'CFBundleShortVersionString': product['version'], 'CFBundleVersion': product['version'],
                'LSMinimumSystemVersion': '13.0', 'NSHighResolutionCapable': True}
        payload_info = plistlib.loads((source / 'Contents/Info.plist').read_bytes())
        icon_name = payload_info.get('CFBundleIconFile', '')
        if icon_name and not icon_name.endswith('.icns'):
            icon_name += '.icns'
        icon = source / 'Contents/Resources' / icon_name
        if icon_name and icon.is_file():
            shutil.copy2(icon, resources / 'Setup.icns')
            info['CFBundleIconFile'] = 'Setup.icns'
        (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
        environment = dict(os.environ)
        clt = Path('/Library/Developer/CommandLineTools')
        if clt.is_dir():
            environment.setdefault('DEVELOPER_DIR', str(clt))
        run(['xcrun', 'swiftc', '-O', '-parse-as-library', '-module-cache-path', str(Path(tempfile.gettempdir()) / 'ag-setup-swift-cache'),
             templates / 'Setup.swift', '-o', contents / 'MacOS/AGSetup'], env=environment)
        run(['/usr/bin/codesign', '--force', '--sign', '-', setup])
        (image / 'READ ME.txt').write_text('Unsigned community build. This installer is not notarized.\n'
            'Quit the application from its menu-bar icon before updating.\n'
            'Setup selects application, artifact/data, and backup folders. Existing data is preserved.\n'
            'System-widget availability depends on operating system registration and signing.\n')
        target = output / artifact_name(product, 'macos', 'dmg')
        run(['/usr/bin/hdiutil', 'create', '-quiet', '-volname', product['name'] + ' Setup', '-srcfolder', image,
             '-format', 'UDZO', target])
        return target


def artifact_name(product, os_name, suffix):
    """Name downloads with explicit product, version, platform and actual build architecture."""
    machine = platform.machine().lower()
    architecture = {'aarch64': 'arm64', 'amd64': 'x64', 'x86_64': 'x64'}.get(machine, machine)
    return f"{product['appId']}-{product['version']}-{os_name}-{architecture}.{suffix}"


def windows(product, source, output, templates):
    """Compile a GUI Inno Setup installer on Windows; no cross-build claims."""
    compiler = shutil.which('ISCC') or str(Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Inno Setup 6/ISCC.exe')
    if not Path(compiler).is_file():
        raise RuntimeError('Install Inno Setup 6 on the Windows build runner')
    definitions = {'ProductName': product['name'], 'ProductId': product['appId'], 'ProductVersion': product['version'],
                   'ProductBinary': product.get('windowsBinary', product['binary'] + '.exe'),
                   'DataName': product['defaultDataName'], 'Payload': str(source), 'Output': str(output),
                   'OutputName': artifact_name(product, 'windows', 'exe')[:-4]}
    with tempfile.TemporaryDirectory(prefix='ag-win-setup-') as folder:
        manifest = Path(folder) / 'desktop-product.json'
        manifest.write_text(json.dumps(product))
        definitions['ProductManifest'] = str(manifest)
        run([compiler] + ['/D' + key + '=' + value for key, value in definitions.items()] + [templates / 'setup.iss'])
    return output / (definitions['OutputName'] + '.exe')


def linux(product, source, output, templates):
    """Ship a self-contained payload and native desktop-registration setup script for Linux."""
    if not (source / product['binary']).is_file():
        raise ValueError('Linux payload executable is missing')
    target = output / artifact_name(product, 'linux', 'tar.gz')
    with tempfile.TemporaryDirectory(prefix='ag-linux-setup-') as folder:
        root = Path(folder) / product['appId']
        shutil.copytree(source, root / 'payload', symlinks=True)
        (root / 'payload/.ag-desktop-product.json').write_text(json.dumps(product))
        run([sys.executable, '-m', 'PyInstaller', '--clean', '--noconfirm', '--onefile', '--windowed',
             '--name', 'Setup', '--distpath', root, '--workpath', Path(folder) / 'work',
             '--specpath', Path(folder), templates / 'setup-linux.py'])
        (root / 'desktop-product.json').write_text(json.dumps(product))
        with tarfile.open(target, 'w:gz') as archive:
            archive.add(root, arcname=product['appId'])
    return target


def main():
    """Build one native installer and its immutable digest/release identity metadata."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--widgets-source', type=Path, help='Optional verified native macOS widget companion bundle')
    args = parser.parse_args()
    product = read_product(args.manifest)
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    if source == output or source in output.parents:
        raise ValueError('Installer output must be outside the payload')
    output.mkdir(parents=True, exist_ok=True)
    templates = Path(__file__).resolve().parent / 'desktop'
    builder = {'darwin': macos, 'win32': windows}.get(sys.platform, linux)
    if args.widgets_source and sys.platform != 'darwin':
        raise ValueError('macOS widget companion requires the macOS builder')
    target = builder(product, source, output, templates, args.widgets_source.resolve(strict=True)) if args.widgets_source else builder(product, source, output, templates)
    with target.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    (output / (target.name + '.sha256')).write_text(f'{digest}  {target.name}\n')
    metadata = {'schemaVersion': 'ag-desktop/release-v1', 'appId': product['appId'], 'version': product['version'],
                'platform': sys.platform, 'architecture': platform.machine(), 'unsigned': True,
                'signing': 'ad-hoc' if sys.platform == 'darwin' else 'unsigned', 'notarized': False,
                'asset': target.name, 'sha256': digest, 'repository': product['repository']}
    (output / (target.name + '.json')).write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata))


if __name__ == '__main__':
    main()
