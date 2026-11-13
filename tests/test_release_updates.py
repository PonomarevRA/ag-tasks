"""Release hints are bounded, fail open, and remain throttled across app restarts."""

from io import BytesIO
import json
import os

import pytest

from ag_tasks.release_updates import RELEASES_URL, REMINDER_INTERVAL, ReleaseUpdates
from desktop_support import updates


def release(version='v0.8.4'):
    return {'version': version, 'available': True, 'url': f'{RELEASES_URL}/tag/{version}',
            'changelog': {'ru': ['Быстрее открываются задачи.'], 'en': ['Tasks open faster.']}}


def test_github_stable_feed_validates_identity_and_projects_plain_notes(monkeypatch):
    url = f'{RELEASES_URL}/tag/v0.8.4'
    payload = {'tag_name': 'v0.8.4', 'html_url': url, 'draft': False, 'prerelease': False,
               'body': '## Русский\n- Быстрее открываются задачи.\n- [Не показывать](https://example.invalid)\n'
                       '## English\n- Tasks open faster.\n- <img>\n- https://example.invalid\n'}

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url == 'https://api.github.com/repos/PonomarevRA/ag-tasks/releases/latest'
            assert timeout == 8 and not request.has_header('Authorization')
            return BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(updates, 'build_opener', lambda *_: Opener())
    assert updates.check_release('ag-tasks', '0.8.3') == release()
    assert updates.check_release('ag-tasks', '0.10.0')['available'] is False
    for key, invalid in [('html_url', 'https://example.invalid'), ('tag_name', 'v0.8.4-rc.1'),
                         ('draft', True), ('prerelease', True), ('tag_name', None)]:
        with monkeypatch.context() as patch:
            patch.setitem(payload, key, invalid)
            with pytest.raises(ValueError):
                updates.check_release('ag-tasks', '0.8.3')


def test_reminder_repeats_only_after_72_hours_across_restart(tmp_path):
    now = 1_800_000_000
    def owner():
        return ReleaseUpdates(tmp_path, '0.8.3', fetch=lambda *_: release(), clock=lambda: now)

    monitor = owner()
    assert monitor.recommendation() is None
    monitor.check(force=True)
    assert monitor.recommendation()['delivery'] == 'final_response_only'
    assert monitor.recommendation() is None
    if os.name != 'nt':
        assert (tmp_path / 'release-reminders.json').stat().st_mode & 0o777 == 0o600

    now += REMINDER_INTERVAL - 1
    monitor = owner()
    monitor.check()
    assert monitor.recommendation() is None
    now += 1
    assert monitor.recommendation()['version'] == 'v0.8.4'


def test_failed_reminder_write_never_appears_as_task_failure(tmp_path):
    path = tmp_path / 'release-reminders.json'
    path.mkdir()
    monitor = ReleaseUpdates(tmp_path, '0.8.3', fetch=lambda *_: release())
    monitor.check()
    assert monitor.recommendation() is None
    assert path.is_dir()
