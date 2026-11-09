"""Bounded read-only update discovery pinned to the application's official repository."""

import json
import re
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

REPOSITORIES = {
    'code-atlas': 'code-atlas', 'agmemory': 'ag-memmory', 'ag-observer': 'ag-observer',
    'ag-tasks': 'ag-tasks', 'ag-design': 'ag-design',
}


class NoRedirect(HTTPRedirectHandler):
    """Never follow a metadata response into an unreviewed host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def version_tuple(value):
    """Compare stable numeric releases only; reject draft/prerelease naming ambiguity."""
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError('Expected a stable semantic version')
    return tuple(int(part) for part in match.groups())


def check_release(app_id, current_version):
    """Return metadata only; installers are opened after a user action, never auto-executed."""
    repository = REPOSITORIES[app_id]
    url = f'https://api.github.com/repos/PonomarevRA/{repository}/releases/latest'
    request = Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'AGDesktop/1'})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=8) as response:
        payload = response.read(262145)
    if len(payload) > 262144:
        raise ValueError('Release metadata exceeds limit')
    release = json.loads(payload)
    version = release['tag_name']
    expected = f'https://github.com/PonomarevRA/{repository}/releases/tag/{version}'
    if release.get('html_url') != expected or release.get('draft') or release.get('prerelease'):
        raise ValueError('Unexpected release identity')
    return {'version': version, 'available': version_tuple(version) > version_tuple(current_version), 'url': expected}
