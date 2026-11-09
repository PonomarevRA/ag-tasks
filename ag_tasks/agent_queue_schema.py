"""Additive agent-queue storage; startup never infers that an external runtime stopped."""

SCHEMA = [
    'CREATE TABLE IF NOT EXISTS agent_queue_version(version INTEGER NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS agent_lanes(
        number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,title TEXT NOT NULL,
        profile TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,draining INTEGER NOT NULL DEFAULT 0,
        active_attempt_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS agent_queue_items(
        number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        lane_id TEXT NOT NULL REFERENCES agent_lanes(id),issue_id TEXT REFERENCES issues(id),
        session_id TEXT REFERENCES service_sessions(id),text TEXT NOT NULL,state TEXT NOT NULL,
        position INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS agent_queue_order ON agent_queue_items(lane_id,state,position,number)',
    '''CREATE TABLE IF NOT EXISTS agent_attempts(
        number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        lane_id TEXT NOT NULL REFERENCES agent_lanes(id),item_id TEXT NOT NULL REFERENCES agent_queue_items(id),
        worker_id TEXT NOT NULL,worker_epoch TEXT NOT NULL,incarnation TEXT NOT NULL,
        profile TEXT NOT NULL,state TEXT NOT NULL,cancel_requested INTEGER NOT NULL DEFAULT 0,
        lease_expires_at TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,outcome TEXT,release_proof TEXT,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
    "CREATE UNIQUE INDEX IF NOT EXISTS agent_attempt_active ON agent_attempts(lane_id) WHERE state!='released'",
    'CREATE INDEX IF NOT EXISTS agent_attempt_item ON agent_attempts(item_id,state)',
]


def check_compatible(connection):
    """Reject unknown queue semantics before legacy startup can change the user's database."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_queue_version'").fetchone()
    if exists:
        rows = connection.execute('SELECT version FROM agent_queue_version').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise ValueError('Unsupported future agent queue schema')


def migrate(connection):
    """Install storage and quarantine old claims atomically; preserve their exclusive lane reservation."""
    check_compatible(connection)
    connection.execute('SAVEPOINT agent_queue_migration')
    try:
        for statement in SCHEMA:
            connection.execute(statement)
        if connection.execute('SELECT version FROM agent_queue_version').fetchone() is None:
            connection.execute('INSERT INTO agent_queue_version VALUES(1)')
        connection.execute("UPDATE agent_queue_items SET state='unknown',revision=revision+1 WHERE id IN (SELECT item_id FROM agent_attempts WHERE state NOT IN ('released','unknown'))")
        connection.execute("UPDATE agent_attempts SET state='unknown',revision=revision+1 WHERE state NOT IN ('released','unknown')")
        connection.execute('RELEASE agent_queue_migration')
    except BaseException:
        connection.execute('ROLLBACK TO agent_queue_migration')
        connection.execute('RELEASE agent_queue_migration')
        raise
