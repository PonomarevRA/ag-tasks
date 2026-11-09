"""Additive durable chat bindings, independent of transient observations and execution."""


def check_compatible(connection):
    """
    Reject unknown chat schemas before startup can reconcile legacy work.

    Args:
        connection: Owning SQLite connection, before startup mutations.
    Raises:
        ValueError: A stored chat schema is not the supported version.
    """
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_version' AND type='table'").fetchone()
    if exists:
        versions = connection.execute('SELECT version FROM conversation_version').fetchall()
        if len(versions) != 1 or versions[0][0] != 1:
            raise ValueError('Unsupported future conversation schema')


def migrate(connection):
    """
    Add stable chat identities without changing old sessions or workflow records.

    Args:
        connection: Owning connection inside the service migration savepoint.
    """
    check_compatible(connection)
    connection.execute('CREATE TABLE IF NOT EXISTS conversation_version(version INTEGER NOT NULL)')
    connection.execute('''CREATE TABLE IF NOT EXISTS conversations(
        number INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
        harness TEXT NOT NULL,instance_id TEXT NOT NULL,remote_id TEXT NOT NULL,
        title TEXT NOT NULL,workspace TEXT NOT NULL,issue_id TEXT REFERENCES issues(id),
        connection_id TEXT NOT NULL,owner_incarnation TEXT NOT NULL,
        continued_from TEXT REFERENCES conversations(id),
        revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(harness,instance_id,remote_id))''')
    connection.execute('CREATE INDEX IF NOT EXISTS conversations_issue ON conversations(issue_id,number)')
    if not connection.execute('SELECT 1 FROM conversation_version').fetchone():
        connection.execute('INSERT INTO conversation_version VALUES(1)')
