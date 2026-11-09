"""Crash-safe ZIP tier for inactive task content; metadata, identities and graph relations remain queryable."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import threading
from uuid import uuid4
import zipfile

from .documents import owned_directory
from .issues import DomainError, timestamp

MAX_PAYLOAD = 8 * 1024 * 1024
FIELDS = {'comments': ('text',), 'history': ('payload',), 'results': ('payload',),
          'documents': ('content',), 'problems': ('details', 'resolution')}


class ColdStorage:
    """One low-priority sweep compresses bounded inactive issues; a normal detail read restores content transactionally."""

    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.worker = None
        self.last_error = None

    def start(self):
        """Run at most one task per minute, starting after a quiet minute; never wake an agent/model."""
        def run():
            while not self.stop_event.wait(60):
                try:
                    self.store.service.process.sweep()
                    self.store.service.process.last_error = None
                except Exception as error:
                    self.store.service.process.last_error = type(error).__name__
                try:
                    self.store.service.session_retention.sweep()
                    self.store.service.session_retention.last_error = None
                except Exception as error:
                    self.store.service.session_retention.last_error = type(error).__name__
                try:
                    self.store.execution.expire_all()
                    self.store.aging.sweep()
                    self.store.aging.last_error = None
                except Exception as error:
                    self.store.aging.last_error = type(error).__name__
                try:
                    self.sweep(limit=1)
                    self.last_error = None
                except Exception as error:
                    # Keep source data on every failure; expose the safe type through service status.
                    self.last_error = type(error).__name__
        self.worker = threading.Thread(target=run, name='ag-tasks-cold-storage', daemon=True)
        self.worker.start()

    def stop(self):
        """Wake a sleeping worker immediately; finish an already admitted bounded transaction before closing SQLite."""
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)

    def sweep(self, *, now=None, limit=1):
        """Compress tasks unchanged for seven days; running work and current review are always excluded."""
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('Sweep limit must be1..10')
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=7)).isoformat()
        with self.store.db.lock:
            identifiers = [row[0] for row in self.store.db.connection.execute('''SELECT id FROM issues
                WHERE updated_at<? AND storage_zip IS NULL AND deleted_at IS NULL AND status NOT IN ('in_progress','in_review')
                AND (storage_retry_at IS NULL OR storage_retry_at<?)
                AND NOT EXISTS(SELECT 1 FROM work_sessions w WHERE w.issue_id=issues.id AND w.state='running')
                ORDER BY updated_at LIMIT ?''', (cutoff, (now or datetime.now(timezone.utc)).isoformat(), limit))]
        archived = []
        for identifier in identifiers:
            if self._archive(identifier, cutoff):
                archived.append(identifier)
        return archived

    def _archive(self, issue_id, cutoff):
        """Compress one opaque issue older than the UTC cutoff, returning whether it moved to ZIP.

        Recheck eligibility under the write lock. Oversized content is deferred intact;
        filesystem errors roll back SQL and leave its source bodies available.
        """
        with self.store.db.transaction() as connection:
            issue = connection.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone()
            if issue is None:
                return False
            if issue['storage_zip'] or issue['deleted_at']:
                return False
            if issue['updated_at'] >= cutoff or issue['status'] in ('in_progress', 'in_review'):
                return False
            # Eligibility can change after sweep selected the candidate; live work always wins.
            active = connection.execute("SELECT 1 FROM work_sessions WHERE issue_id=? AND state='running'", (issue_id,)).fetchone()
            if active:
                return False
            payload = {'version': 1, 'issueId': issue_id, 'description': issue['description'], 'tables': {}}
            used = len(issue['description'].encode())
            for table, columns in FIELDS.items():
                entries = []
                cursor = connection.execute(f"SELECT id,{','.join(columns)} FROM {table} WHERE issue_id=?", (issue_id,))
                for row in cursor:
                    item = dict(row)
                    used += len(json.dumps(item, ensure_ascii=False).encode())
                    if len(entries) >= 10000 or used > MAX_PAYLOAD - 65536:
                        self._defer_large(connection, issue_id)
                        return False
                    entries.append(item)
                payload['tables'][table] = entries
            raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
            if len(raw) > MAX_PAYLOAD:
                self._defer_large(connection, issue_id)
                return False
            directory = owned_directory(self.store.data_dir, 'task-archives')
            filename = issue_id + '-' + uuid4().hex + '.zip'
            target, temporary = directory / filename, directory / (filename + '.tmp')
            try:
                with temporary.open('xb') as stream:
                    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                        archive.writestr('payload.json', raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
                if os.name != 'nt':
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                digest = sha256(target.read_bytes()).hexdigest()
                connection.execute('UPDATE problems SET details_preview=substr(details,1,1024),resolution_preview=substr(resolution,1,1024) WHERE issue_id=?',(issue_id,))
                # Keep NOT NULL/JSON column contracts valid while list projections skip cold bodies.
                for table, columns in FIELDS.items():
                    assignments = ','.join(column + '=?' for column in columns)
                    empty = tuple('{}' if column == 'payload' else '' for column in columns)
                    connection.execute(f'UPDATE {table} SET {assignments} WHERE issue_id=?', (*empty, issue_id))
                connection.execute("UPDATE issues SET description='',storage_zip=?,storage_sha256=?,storage_archived_at=? WHERE id=?", (filename, digest, timestamp(), issue_id))
                self.store._audit(connection, issue_id, 'storage_compressed', 'system', {'sha256': digest, 'uncompressedBytes': len(raw)})
            finally:
                temporary.unlink(missing_ok=True)
        # Reclaim only matching service exports; user-edited files are never removed.
        self._prune_exports(issue_id, payload['tables']['documents'])
        self._prune_orphans(directory)
        return True

    @staticmethod
    def _defer_large(connection, issue_id):
        """Retry oversized tasks tomorrow instead of starving smaller inactive tasks behind the oldest candidate."""
        retry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        connection.execute('UPDATE issues SET storage_retry_at=? WHERE id=?', (retry, issue_id))

    def restore(self, issue_id, connection):
        """Validate a single owned ZIP member and checksum, then restore all content in the caller's transaction."""
        with self.store.db.transaction() if not connection.in_transaction else nullcontext(connection):
            issue = connection.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone()
            if not issue or not issue['storage_zip']:
                return
            filename = issue['storage_zip']
            if '/' in filename or '\\' in filename or not filename.startswith(issue_id + '-') or not filename.endswith('.zip'):
                raise DomainError('archive_unavailable', 'Task archive metadata is invalid.', 503)
            target = owned_directory(self.store.data_dir, 'task-archives') / filename
            try:
                if target.is_symlink() or target.stat().st_size > MAX_PAYLOAD + 65536:
                    raise ValueError('Unsafe archive')
                encoded = target.read_bytes()
                if sha256(encoded).hexdigest() != issue['storage_sha256']:
                    raise ValueError('Archive checksum mismatch')
                with zipfile.ZipFile(target) as archive:
                    info = archive.infolist()
                    if len(info) != 1 or info[0].filename != 'payload.json' or info[0].file_size > MAX_PAYLOAD:
                        raise ValueError('Invalid archive member')
                    payload = json.loads(archive.read('payload.json'))
                if payload['version'] != 1 or payload['issueId'] != issue_id or set(payload['tables']) != set(FIELDS):
                    raise ValueError('Invalid archive identity')
                for table, columns in FIELDS.items():
                    assignments = ','.join(column + '=?' for column in columns)
                    for row in payload['tables'][table]:
                        if set(row) != {'id', *columns}:
                            raise ValueError('Invalid archive fields')
                        connection.execute(f'UPDATE {table} SET {assignments} WHERE id=? AND issue_id=?', (*[row[key] for key in columns], row['id'], issue_id))
                connection.execute('UPDATE problems SET details_preview=substr(details,1,1024),resolution_preview=substr(resolution,1,1024) WHERE issue_id=?',(issue_id,))
                connection.execute("UPDATE issues SET merged_head=COALESCE(merged_head,(SELECT json_extract(payload,'$.head') FROM history WHERE issue_id=? AND action='merge_approved' ORDER BY id DESC LIMIT 1)) WHERE id=?",(issue_id,issue_id))
                connection.execute('UPDATE issues SET description=?,storage_zip=NULL,storage_sha256=NULL,storage_archived_at=NULL,updated_at=? WHERE id=?',
                                   (payload['description'], timestamp(), issue_id))
                self.store._audit(connection, issue_id, 'storage_restored', 'system', {'sha256': issue['storage_sha256']})
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
                raise DomainError('archive_unavailable', 'Task archive is unavailable or damaged; source ZIP was retained.', 503) from error

    def _prune_exports(self, issue_id, documents):
        """Remove only exact generated mirrors after commit; an edited export belongs to the user."""
        candidate = self.store.data_dir / 'artifacts' / 'tasks' / issue_id
        if not candidate.exists():
            return
        directory = owned_directory(self.store.data_dir, 'artifacts', 'tasks', issue_id)
        with self.store.db.lock:
            for item in documents:
                row = self.store.db.connection.execute('SELECT name,sha256 FROM documents WHERE id=?', (item['id'],)).fetchone()
                target = directory / row['name']
                if target.is_file() and not target.is_symlink() and target.stat().st_size <= 48 * 1024 and sha256(target.read_bytes()).hexdigest() == row['sha256']:
                    target.unlink()

    def _prune_orphans(self, directory):
        """Bound cleanup to100 old unreferenced ZIPs; live archives are never aged out or removed by restore."""
        cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        with self.store.db.lock:
            for index, path in enumerate(directory.glob('*.zip')):
                if index >= 100:
                    break
                if not path.is_symlink() and path.stat().st_mtime < cutoff and not self.store.db.connection.execute('SELECT 1 FROM issues WHERE storage_zip=?', (path.name,)).fetchone():
                    path.unlink()
