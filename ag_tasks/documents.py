"""Bounded Markdown documents and service-owned file exports; no arbitrary filesystem path API."""

from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from markdown_it import MarkdownIt

from .issues import DomainError, bounded_text, timestamp

MAX_DOCUMENT_BYTES = 48 * 1024
MAX_DOCUMENTS = 32


def render_markdown(content):
    """Render headings, tables, lists and fenced code with raw HTML/images disabled and safe link schemes only."""
    parser = MarkdownIt('commonmark', {'html': False, 'maxNesting': 20}).enable(['table', 'strikethrough']).disable('image')
    tokens = parser.parse(content)
    for token in tokens:
        for child in token.children or []:
            if child.type == 'link_open':
                href = child.attrGet('href') or ''
                try:
                    parsed = urlsplit(href)
                    scheme = parsed.scheme.lower()
                    if parsed.username or parsed.password:
                        child.attrSet('href', '#')
                        continue
                except ValueError:
                    child.attrSet('href', '#')
                    continue
                if scheme not in ('http', 'https') and not href.startswith('#') and not re.fullmatch(r'[\w .-]+\.md', href, re.IGNORECASE):
                    child.attrSet('href', '#')
                if scheme in ('http', 'https'):
                    child.attrSet('rel', 'noreferrer noopener')
                    child.attrSet('target', '_blank')
    return parser.renderer.render(tokens, parser.options, {})


def owned_directory(root, *segments):
    """Create fixed service directories without accepting symlink components or paths outside the data owner."""
    path = Path(root)
    for segment in segments:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', segment):
            raise DomainError('invalid_storage_path', 'Invalid owned storage identifier.')
        path = path / segment
        if path.is_symlink():
            raise DomainError('unsafe_storage_path', 'Service storage cannot use symlink directories.', 409)
        path.mkdir(mode=0o700, exist_ok=True)
    return path


class Documents:
    """SQLite is authoritative; Markdown files are explicit, reproducible exports for the system file manager."""

    def __init__(self, store):
        self.store = store

    @staticmethod
    def validate_content(content):
        """Require valid UTF-8 Markdown within 48 KiB, leaving all source text inert."""
        content = bounded_text(content, MAX_DOCUMENT_BYTES)
        if len(content.encode('utf-8')) > MAX_DOCUMENT_BYTES:
            raise DomainError('document_too_large', 'Markdown exceeds 48 KiB.')
        return content

    def metadata(self, issue_id):
        """Read at most32 attachment chips without fetching document content or restoring cold storage."""
        rows = self.store.db.connection.execute('SELECT id,name,purpose,sha256,revision,actor,created_at,updated_at FROM documents WHERE issue_id=? ORDER BY created_at,id LIMIT 32', (issue_id,)).fetchall()
        return [dict(row) for row in rows]

    def save_in(self, connection, issue_id, value, actor, *, invalidate=True):
        """Write one named document under the caller transaction; replacement requires the exact prior revision."""
        if type(value) is not dict or set(value) - {'name', 'content', 'purpose', 'expectedRevision'} or not {'name', 'content'} <= set(value):
            raise DomainError('invalid_document', 'A Markdown document requires name and content.')
        name = bounded_text(value['name'], 128, empty=False)
        if len(name.encode('utf-8')) > 255:
            raise DomainError('invalid_document_name', 'The UTF-8 filename exceeds the filesystem limit.')
        if not re.fullmatch(r'[\w][\w .-]*\.md', name, re.IGNORECASE) or '..' in name or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.IGNORECASE):
            raise DomainError('invalid_document_name', 'Use a plain .md filename without directory segments.')
        content = self.validate_content(value['content'])
        purpose = value.get('purpose', 'attachment')
        if purpose not in ('plan', 'attachment', 'result'):
            raise DomainError('invalid_document', 'Invalid document purpose.')
        issue = self.store._row(issue_id, connection)
        if issue['archived_at'] or issue['kind'] == 'review':
            raise DomainError('issue_read_only', 'Restore an ordinary task before changing its documents.', 409)
        current = connection.execute('SELECT * FROM documents WHERE issue_id=? AND name=? COLLATE NOCASE', (issue_id, name)).fetchone()
        expected = value.get('expectedRevision')
        if expected is not None and type(expected) is not int:
            raise DomainError('invalid_revision', 'A document revision must be an integer.')
        current_revision = current['revision'] if current else 0
        supplied_revision = 0 if expected is None else expected
        if supplied_revision != current_revision:
            raise DomainError('document_conflict', 'Read the current document revision before replacing it.', 409)
        if not current and connection.execute('SELECT COUNT(*) FROM documents WHERE issue_id=?', (issue_id,)).fetchone()[0] >= MAX_DOCUMENTS:
            raise DomainError('document_limit', 'At most32 Markdown documents per task.', 409)
        identifier = current['id'] if current else uuid4().hex
        now, digest = timestamp(), sha256(content.encode()).hexdigest()
        if current:
            connection.execute('UPDATE documents SET content=?,sha256=?,revision=revision+1,purpose=?,actor=?,updated_at=? WHERE id=?',
                               (content, digest, purpose, actor, now, identifier))
        else:
            connection.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,1,?,?,?)',
                               (identifier, issue_id, name, purpose, content, digest, actor, now, now))
        was_plan = current is not None and current['purpose'] == 'plan'
        if invalidate and (purpose == 'plan' or was_plan):
            self.store.definition_changed(connection, issue, 'plan_changed')
        connection.execute('UPDATE issues SET updated_at=? WHERE id=?', (now, issue_id))
        self.store._audit(connection, issue_id, 'document_saved', actor,
                           {'documentId': identifier, 'name': name, 'sha256': digest, 'revision': current['revision'] + 1 if current else 1})
        return identifier

    def save(self, issue_id, value, actor='agent'):
        """Persist content first; export files never participate in execution or approval decisions."""
        with self.store.db.transaction() as connection:
            identifier = self.save_in(connection, issue_id, value, actor)
        return self.get(issue_id, identifier)

    def get(self, issue_id, document_id):
        """Restore a cold task on demand and return bounded Markdown/source HTML with exact content identity."""
        bounded_text(document_id, 128, empty=False)
        with self.store.db.lock:
            self.store._row(issue_id)
            row = self.store.db.connection.execute('SELECT * FROM documents WHERE issue_id=? AND id=?', (issue_id, document_id)).fetchone()
            if row is None:
                raise DomainError('document_not_found', 'Task document not found.', 404)
            result = dict(row)
        result['html'] = render_markdown(result['content'])
        return result

    def folder(self, issue_id):
        """Export current task documents into an owned folder, with atomic no-follow writes and no caller-selected path."""
        with self.store.db.lock:
            self.store._row(issue_id)
            directory = owned_directory(self.store.data_dir, 'artifacts', 'tasks', issue_id)
            rows = self.store.db.connection.execute('SELECT id,name,content FROM documents WHERE issue_id=?', (issue_id,)).fetchall()
            for row in rows:
                target = directory / row['name']
                if target.is_symlink():
                    raise DomainError('unsafe_storage_path', 'Document export cannot overwrite symlinks.', 409)
                if target.exists():
                    if not target.is_file():
                        raise DomainError('unsafe_storage_path', 'A document export cannot replace a directory.', 409)
                    same_size = target.stat().st_size == len(row['content'].encode('utf-8'))
                    if same_size and target.read_bytes() == row['content'].encode('utf-8'):
                        continue
                    # The folder may contain edits from an external editor; opening it is not overwrite consent.
                    preserved = directory / (row['id'] + '.local-' + uuid4().hex + '.md')
                    target.rename(preserved)
                temporary = directory / (uuid4().hex + '.tmp')
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                try:
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                        stream.write(row['content'])
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        return directory

    def open_folder(self, issue_id):
        """Human UI only: reveal this task's exported Markdown files in the OS file manager without a shell."""
        directory = self.folder(issue_id)
        try:
            if os.name == 'nt':
                os.startfile(str(directory))
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(directory)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        except OSError as error:
            raise DomainError('file_manager_unavailable', 'The system file manager is unavailable.', 503) from error
        return {'opened': True, 'path': str(directory)}
