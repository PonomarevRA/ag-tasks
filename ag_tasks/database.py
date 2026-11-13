"""Transactional durable SQLite ownership for issues, relations, comments and append-only audit."""

from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3
import threading
from uuid import uuid4

SCHEMA = '''
CREATE TABLE IF NOT EXISTS repositories(
 id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, base_branch TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issues(
 number INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL,
 status TEXT NOT NULL, priority TEXT NOT NULL, parent_id TEXT REFERENCES issues(id), assignee TEXT,
 repo_id TEXT REFERENCES repositories(id), external_refs TEXT NOT NULL DEFAULT '[]',
 branch TEXT, worktree TEXT, base_branch TEXT, base TEXT, head TEXT, reviewed_head TEXT, reviewed_base TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS issues_parent ON issues(parent_id);
CREATE INDEX IF NOT EXISTS issues_status ON issues(status);
CREATE TABLE IF NOT EXISTS issue_links(
 left_id TEXT NOT NULL REFERENCES issues(id), right_id TEXT NOT NULL REFERENCES issues(id), kind TEXT NOT NULL,
 PRIMARY KEY(left_id,right_id,kind));
CREATE TABLE IF NOT EXISTS comments(
 id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT NOT NULL REFERENCES issues(id), text TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS comments_issue ON comments(issue_id,id);
CREATE TABLE IF NOT EXISTS history(
 id INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT NOT NULL REFERENCES issues(id), action TEXT NOT NULL,
 actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS history_issue ON history(issue_id,id);
'''


class Database:
    """Shares one connection under a reentrant lock; write units use BEGIN IMMEDIATE and full commit durability."""

    def __init__(self, data_dir):
        """Create only the service database; existing symlink database files are refused."""
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / 'issues.sqlite3'
        if self.path.is_symlink():
            raise ValueError('Database symlinks are forbidden')
        self.lock = threading.RLock()
        self._transaction_depth = 0
        self.closed = False
        self.connection = None
        self.queue_incarnation = uuid4().hex
        lock_path = self.data_dir / 'ag-tasks.lock'
        if lock_path.is_symlink():
            raise RuntimeError('Service lock symlinks are forbidden')
        self.owner = lock_path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.owner.seek(0)
                if self.owner.read(1) == b'':
                    self.owner.write(b'0')
                    self.owner.flush()
                self.owner.seek(0)
                msvcrt.locking(self.owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=2)
            self.connection.row_factory = sqlite3.Row
            if self.connection.execute('PRAGMA user_version').fetchone()[0] > 6:
                raise ValueError('Unsupported future issue database version')
            from .service_schema import check_compatible
            check_compatible(self.connection)
            from .agent_queue_schema import check_compatible as check_queue_compatible
            check_queue_compatible(self.connection)
            from .process_schema import check_compatible as check_process_compatible
            check_process_compatible(self.connection)
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.executescript(SCHEMA)
            self._migrate_workflow()
            from .planning_schema import migrate
            migrate(self.connection)
            from .code_context import migrate as migrate_code_context
            migrate_code_context(self.connection)
            columns = {row['name'] for row in self.connection.execute('PRAGMA table_info(work_sessions)')}
            if 'intended_role' not in columns:
                self.connection.execute('ALTER TABLE work_sessions ADD COLUMN intended_role TEXT')
            from .problem_digest import migrate as migrate_problem_digest
            migrate_problem_digest(self.connection)
            from .completion import migrate as migrate_completion
            migrate_completion(self.connection)
            from .service_schema import migrate as migrate_service_data
            migrate_service_data(self.connection)
            from .agent_queue_schema import migrate as migrate_agent_queue
            migrate_agent_queue(self.connection)
            from .review_queue import migrate as migrate_review_queue
            migrate_review_queue(self.connection)
            from .execution_diagnostics import migrate as migrate_diagnostics
            migrate_diagnostics(self.connection)
            from .aging import migrate as migrate_aging
            migrate_aging(self.connection)
            from .project_spaces import migrate as migrate_project_spaces
            migrate_project_spaces(self.connection)
            self.connection.execute('PRAGMA user_version=6')
        except BaseException:
            if self.connection is not None:
                self.connection.close()
            self.owner.close()
            raise

    def _migrate_workflow(self):
        """Add v2 state atomically, keeping issue IDs/audit and never inferring historical work duration."""
        connection = self.connection
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,archived_at TEXT)')
            connection.execute("INSERT OR IGNORE INTO spaces(id,name,created_at,archived_at) VALUES('default','Default',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL)")
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(issues)')}
            additions = {'space_id': "TEXT NOT NULL DEFAULT 'default'", 'kind': "TEXT NOT NULL DEFAULT 'task'",
                         'intended_role': 'TEXT', 'dor': "TEXT NOT NULL DEFAULT '[]'", 'dod': "TEXT NOT NULL DEFAULT '[]'",
                         'definition_revision': 'INTEGER NOT NULL DEFAULT 0', 'result_generation': 'INTEGER NOT NULL DEFAULT 0',
                         'critic_review_id': 'TEXT', 'archived_at': 'TEXT', 'deleted_at': 'TEXT'}
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f'ALTER TABLE issues ADD COLUMN {name} {declaration}')
            connection.execute('CREATE INDEX IF NOT EXISTS issues_space ON issues(space_id,number)')
            connection.execute('''CREATE TABLE IF NOT EXISTS work_sessions(
                id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES issues(id),agent_id TEXT NOT NULL,session_key TEXT NOT NULL,
                started_at TEXT NOT NULL,last_heartbeat_at TEXT NOT NULL,lease_expires_at TEXT NOT NULL,
                elapsed_ms INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,ended_at TEXT,
                UNIQUE(issue_id,agent_id,session_key))''')
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS issue_active_work ON work_sessions(issue_id) WHERE state='running'")
            connection.execute('CREATE INDEX IF NOT EXISTS sessions_issue ON work_sessions(issue_id,started_at)')
            connection.execute('''CREATE TABLE IF NOT EXISTS results(
                id INTEGER PRIMARY KEY AUTOINCREMENT,issue_id TEXT NOT NULL REFERENCES issues(id),generation INTEGER NOT NULL,
                definition_revision INTEGER NOT NULL,producer_agent_id TEXT NOT NULL,reviewed_head TEXT,payload TEXT NOT NULL,created_at TEXT NOT NULL,producer_session_id TEXT,
                UNIQUE(issue_id,generation))''')
            if 'producer_session_id' not in {row['name'] for row in connection.execute('PRAGMA table_info(results)')}:
                connection.execute('ALTER TABLE results ADD COLUMN producer_session_id TEXT')
            connection.execute('''CREATE TABLE IF NOT EXISTS review_reports(
                id INTEGER PRIMARY KEY AUTOINCREMENT,issue_id TEXT NOT NULL REFERENCES issues(id),parent_id TEXT NOT NULL REFERENCES issues(id),
                generation INTEGER NOT NULL,definition_revision INTEGER NOT NULL,reviewed_head TEXT,agent_id TEXT NOT NULL,
                verdict TEXT NOT NULL,summary TEXT NOT NULL,findings TEXT NOT NULL,evidence TEXT NOT NULL,created_at TEXT NOT NULL)''')
            connection.execute('CREATE INDEX IF NOT EXISTS reports_parent ON review_reports(parent_id,id)')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version < 2:
                connection.execute("INSERT INTO history(issue_id,action,actor,payload,created_at) SELECT id,'legacy_work_suspended','system','{\"reason\":\"No explicit work session; historical duration unknown\"}',strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM issues WHERE status='in_progress'")
                connection.execute("UPDATE issues SET status='rework' WHERE status='in_progress'")
            # Work belongs to external agents, whose leases can outlive this HTTP
            # owner. Runtime expiry and explicit credentials govern their state.
            connection.execute('PRAGMA user_version=2')
            connection.execute('COMMIT')
        except BaseException:
            connection.execute('ROLLBACK')
            raise

    @contextmanager
    def transaction(self):
        """Atomically commit one validated domain mutation and its audit entry, rolling back on any error."""
        with self.lock:
            nested = self._transaction_depth > 0
            # Nested domain operations share the outer command's atomic effect/outcome boundary.
            if nested:
                self.connection.execute('SAVEPOINT domain_command')
            else:
                self.connection.execute('BEGIN IMMEDIATE')
            self._transaction_depth += 1
            try:
                yield self.connection
                self.connection.execute('RELEASE domain_command' if nested else 'COMMIT')
            except BaseException:
                if nested:
                    self.connection.execute('ROLLBACK TO domain_command')
                    self.connection.execute('RELEASE domain_command')
                else:
                    self.connection.execute('ROLLBACK')
                raise
            finally:
                self._transaction_depth -= 1

    def close(self):
        """Release the database and process data-owner lock exactly once, preserving all user issue state."""
        with self.lock:
            if not self.closed:
                self.closed = True
                self.connection.close()
                self.owner.close()
