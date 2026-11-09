"""Bounded read-only discovery of local Codex writers and recorded turn lifecycle."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from threading import Lock
import time

THREAD_ID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
TAIL_BYTES = 4 * 1024 * 1024
MAX_WRITERS = 100


def writer_threads(home):
    """An existing lock file is insufficient; require a live codex process holding it open."""
    executable = shutil.which('lsof')
    directory = home / 'thread-writer-locks'
    if executable is None or not directory.is_dir():
        raise OSError('Codex writer discovery unavailable')
    result = subprocess.run([executable, '-n', '-P', '-a', '-c', 'codex', '-Fpcn', '+d', str(directory)],
                            capture_output=True, timeout=2, check=False)
    if result.returncode not in (0, 1) or len(result.stdout) > 1024 * 1024:
        raise OSError('Codex writer discovery failed')
    # lsof uses exit 1 for a successful search with no matching open files.
    if result.returncode == 1 and result.stderr:
        raise OSError('Codex writer discovery failed')
    threads, command = set(), None
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if line.startswith('p'):
            command = None
        elif line.startswith('c'):
            command = line[1:]
        elif line.startswith('n') and command == 'codex':
            path = Path(line[1:])
            if path.parent == directory and path.suffix == '.lock' and THREAD_ID.fullmatch(path.stem):
                threads.add(path.stem)
    return sorted(threads)


class CodexSessions:
    """Read only metadata/lifecycle, caching bounded tails without copying transcripts into Tasks."""

    def __init__(self, home=None, *, writers=writer_threads, clock=time.monotonic, now=None):
        """Allow explicit source injection in tests; production reads the user's local Codex home."""
        self.home = Path(home) if home is not None else Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        self.writers, self.clock = writers, clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock, self.tails = Lock(), {}
        self.cached, self.cached_at = None, float('-inf')

    def snapshot(self):
        """Sample at most once per ten seconds; a failed source is visibly unavailable, never empty success."""
        with self.lock:
            if self.cached is not None and self.clock() - self.cached_at < 10:
                return self.cached
            try:
                self.cached = self._read()
            except (OSError, sqlite3.Error, ValueError, TypeError, subprocess.SubprocessError):
                self.cached = {'items': [], 'source': 'codex-local', 'available': False,
                               'reason': 'source_unavailable', 'truncated': False}
            self.cached_at = self.clock()
            return self.cached

    def _read(self):
        """Join live writer identities to selected SQLite metadata with a strictly read-only connection."""
        identities = self.writers(self.home)
        if not identities:
            return {'items': [], 'source': 'codex-local', 'available': True, 'truncated': False}
        selected = identities[:MAX_WRITERS]
        uri = (self.home / 'state_5.sqlite').resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=0.1)
        try:
            connection.execute('PRAGMA query_only=ON')
            placeholders = ','.join('?' for _ in selected)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
            # Modern Codex uses name for the visible chat title; title may contain the initial prompt.
            name_column = 'name' if 'name' in columns else 'NULL'
            rows = connection.execute('SELECT id,' + name_column + ',rollout_path,updated_at FROM threads WHERE archived=0 AND id IN (' +
                                      placeholders + ') ORDER BY updated_at DESC', selected).fetchall()
        finally:
            connection.close()
        items = []
        for thread_id, title, filename, updated_at in rows:
            observation = self._lifecycle(thread_id, Path(filename))
            items.append({'id': thread_id, 'title': title[:240] if isinstance(title, str) and title.strip() else 'Чат Codex ' + thread_id[:8], 'agent': 'Codex', **observation,
                          'updatedAt': metadata_time(updated_at), 'source': 'codex-local', 'connected': True})
        self.tails = {key: value for key, value in self.tails.items() if key in selected}
        return {'items': items, 'source': 'codex-local', 'available': True,
                'truncated': len(identities) > MAX_WRITERS}

    def _lifecycle(self, thread_id, path):
        """Keep only explicit turn start/end facts; truncated or stale running evidence stays unknown."""
        unknown = {'state': 'unknown', 'activityAt': None, 'evidence': 'writer_only'}
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to((self.home / 'sessions').resolve()) or resolved.suffix != '.jsonl':
                return unknown
            stat = resolved.stat()
            prior = self.tails.get(thread_id)
            continuing = prior and prior['inode'] == stat.st_ino and prior['offset'] <= stat.st_size
            if continuing:
                state = dict(prior)
            else:
                state = {'inode': stat.st_ino, 'offset': max(0, stat.st_size - TAIL_BYTES),
                         'state': 'unknown', 'activityAt': None, 'turnId': None}
            with resolved.open('rb') as stream:
                stream.seek(state['offset'])
                if state['offset'] and not continuing:
                    boundary = stream.readline(TAIL_BYTES)
                    if not boundary.endswith(b'\n'):
                        return unknown  # A huge partial record cannot justify an unbounded read.
                content = stream.read(TAIL_BYTES)
                end = content.rfind(b'\n') + 1
                for line in content[:end].splitlines():
                    if len(line) > 262144:
                        continue
                    self._record(state, line)
                state['offset'] = stream.tell() - len(content) + end
            self.tails[thread_id] = state
            observed = state['state']
            if observed == 'running':
                if state['activityAt'] is None:
                    observed = 'unknown'
                else:
                    age = (self.now() - datetime.fromisoformat(state['activityAt'].replace('Z', '+00:00'))).total_seconds()
                    if not -5 <= age <= 90:
                        observed = 'unknown'
            return {'state': observed, 'activityAt': state['activityAt'],
                    'evidence': 'rollout_lifecycle' if state['state'] != 'unknown' else 'writer_only'}
        except (OSError, ValueError, TypeError):
            return unknown

    def _record(self, state, line):
        """Discard item bodies immediately; retain only lifecycle kind, turn identity and event time."""
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(record, dict) or record.get('type') != 'event_msg':
            return
        payload = record.get('payload')
        if not isinstance(payload, dict):
            return
        kind, turn_id = payload.get('type'), payload.get('turn_id')
        different_turn = turn_id is not None and state['turnId'] is not None and turn_id != state['turnId']
        if kind == 'task_started':
            state['state'], state['turnId'], state['activityAt'] = 'running', turn_id, None
        elif different_turn:
            # A terminal from another turn cannot complete the current one.
            state['state'], state['turnId'], state['activityAt'] = 'unknown', turn_id, None
        elif kind in ('task_complete', 'task_completed', 'turn_aborted'):
            if turn_id == state['turnId'] or state['turnId'] is None:
                state['state'] = 'idle'
        event_time = record.get('timestamp')
        if isinstance(event_time, str):
            try:
                parsed = datetime.fromisoformat(event_time.replace('Z', '+00:00'))
                if parsed.tzinfo is not None and (state['activityAt'] is None or event_time > state['activityAt']):
                    state['activityAt'] = event_time
            except ValueError:
                pass


def metadata_time(value):
    """
    Convert Codex's metadata epoch seconds to an aware timestamp for list sorting.

    Args:
        value: Source updated_at scalar; malformed or unsupported values stay unknown.
    Returns:
        UTC ISO timestamp or None, without changing source files or inferring a turn state.
    """
    if type(value) not in (int, float):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None
