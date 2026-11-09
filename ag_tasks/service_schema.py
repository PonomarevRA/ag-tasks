"""Additive storage for transport-independent commands, sessions and observed events."""

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS service_data_version(version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS service_commands(
        client_id TEXT NOT NULL,command_id TEXT NOT NULL,operation TEXT NOT NULL,
        fingerprint TEXT NOT NULL,response TEXT NOT NULL,status INTEGER NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(client_id,command_id))""",
    """CREATE TABLE IF NOT EXISTS service_changes(
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,client_id TEXT NOT NULL,
        command_id TEXT NOT NULL,operation TEXT NOT NULL,status INTEGER NOT NULL,subject_id TEXT,
        created_at TEXT NOT NULL,UNIQUE(client_id,command_id))""",
    """CREATE TABLE IF NOT EXISTS service_sessions(
        id TEXT PRIMARY KEY,title TEXT NOT NULL,agent TEXT NOT NULL,protocol TEXT NOT NULL,
        protocol_version INTEGER NOT NULL,remote_id TEXT NOT NULL,connection_id TEXT NOT NULL,
        issue_id TEXT REFERENCES issues(id),capabilities TEXT NOT NULL,projection TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS service_events(
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES service_sessions(id),producer_id TEXT NOT NULL,
        epoch TEXT NOT NULL,sequence INTEGER NOT NULL,fingerprint TEXT NOT NULL,
        direction TEXT NOT NULL,origin TEXT NOT NULL,payload TEXT NOT NULL,redactions TEXT NOT NULL,
        received_at TEXT NOT NULL,UNIQUE(session_id,producer_id,epoch,sequence))""",
    "CREATE INDEX IF NOT EXISTS service_events_session ON service_events(session_id,cursor)",
    "CREATE INDEX IF NOT EXISTS service_sessions_activity ON service_sessions(julianday(updated_at),id)",
    "CREATE INDEX IF NOT EXISTS service_sessions_activity_exact ON service_sessions(updated_at,id)",
    """CREATE TABLE IF NOT EXISTS service_streams(
        session_id TEXT NOT NULL REFERENCES service_sessions(id),producer_id TEXT NOT NULL,epoch TEXT NOT NULL,
        next_sequence INTEGER NOT NULL,PRIMARY KEY(session_id,producer_id,epoch))""",
    """CREATE TABLE IF NOT EXISTS service_entities(
        session_id TEXT NOT NULL REFERENCES service_sessions(id),kind TEXT NOT NULL,remote_id TEXT NOT NULL,
        payload TEXT NOT NULL,last_cursor INTEGER NOT NULL,live INTEGER NOT NULL,
        PRIMARY KEY(session_id,kind,remote_id))""",
    """CREATE TABLE IF NOT EXISTS service_requests(
        session_id TEXT NOT NULL REFERENCES service_sessions(id),direction TEXT NOT NULL,rpc_id TEXT NOT NULL,
        method TEXT NOT NULL,PRIMARY KEY(session_id,direction,rpc_id))""",
]


def check_compatible(connection):
    """Reject a future feature schema before any legacy startup reconciliation mutates the database."""
    from .conversation_schema import check_compatible as check_conversations
    check_conversations(connection)
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_data_version'").fetchone()
    if exists:
        rows = connection.execute('SELECT version FROM service_data_version').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise ValueError('Unsupported future service data schema')


def migrate(connection):
    """Install one additive feature schema atomically; refuse unknown future data semantics."""
    check_compatible(connection)
    connection.execute('SAVEPOINT service_data_migration')
    try:
        for statement in SCHEMA:
            connection.execute(statement)
        from .conversation_schema import migrate as migrate_conversations
        migrate_conversations(connection)
        row = connection.execute('SELECT version FROM service_data_version').fetchone()
        if row and row[0] != 1:
            raise ValueError('Unsupported future service data schema')
        if row is None:
            connection.execute('INSERT INTO service_data_version VALUES(1)')
        connection.execute('RELEASE service_data_migration')
    except BaseException:
        connection.execute('ROLLBACK TO service_data_migration')
        connection.execute('RELEASE service_data_migration')
        raise
