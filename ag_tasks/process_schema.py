"""Additive, independently versioned state for task execution evidence and runs."""


def check_compatible(connection):
    """Refuse future process semantics before legacy startup can reconcile work."""
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='process_schema'").fetchone()
    if exists:
        rows = connection.execute('SELECT version FROM process_schema').fetchall()
        if len(rows) != 1 or rows[0][0] != 1:
            raise ValueError('Unsupported process schema version')


def migrate(db):
    """Create v1 tables atomically; refuse future state before changing any rows."""
    with db.transaction() as connection:
        check_compatible(connection)
        connection.execute('CREATE TABLE IF NOT EXISTS process_schema(version INTEGER NOT NULL)')
        version = connection.execute('SELECT version FROM process_schema').fetchone()
        if version and version[0] != 1:
            raise ValueError('Unsupported process schema version')
        statements = [
            '''CREATE TABLE IF NOT EXISTS process_evidence(
                id TEXT PRIMARY KEY,source_key TEXT NOT NULL UNIQUE,issue_id TEXT NOT NULL REFERENCES issues(id),
                digest TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS process_runs(
                id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES issues(id),purpose TEXT NOT NULL,
                state TEXT NOT NULL,owner TEXT NOT NULL,definition_revision INTEGER NOT NULL,
                result_generation INTEGER NOT NULL,payload TEXT NOT NULL)'''
        ]
        for statement in statements:
            connection.execute(statement)
        if not version:
            connection.execute('INSERT INTO process_schema VALUES(1)')
