"""Additive planning migration; issue identities, execution history and Git approval remain intact."""


def migrate(connection):
    """Install planning metadata and indexed content tables as one recoverable SQLite transaction."""
    connection.execute('BEGIN IMMEDIATE')
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(issues)')}
        for name, declaration in {
            'estimated_complexity': 'TEXT', 'actual_complexity': 'TEXT', 'space_number': 'INTEGER',
            'storage_zip': 'TEXT', 'storage_archived_at': 'TEXT', 'storage_sha256': 'TEXT', 'storage_retry_at': 'TEXT',
        }.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE issues ADD COLUMN {name} {declaration}')
        connection.execute('''CREATE TABLE IF NOT EXISTS documents(
            id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES issues(id),name TEXT NOT NULL,
            purpose TEXT NOT NULL,content TEXT NOT NULL,sha256 TEXT NOT NULL,revision INTEGER NOT NULL,
            actor TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            UNIQUE(issue_id,name))''')
        connection.execute('CREATE INDEX IF NOT EXISTS documents_issue ON documents(issue_id,created_at,id)')
        connection.execute('''CREATE TABLE IF NOT EXISTS problems(
            id TEXT PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES issues(id),title TEXT NOT NULL,
            severity TEXT NOT NULL,state TEXT NOT NULL,details TEXT NOT NULL,resolution TEXT NOT NULL,
            actor TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        connection.execute('CREATE INDEX IF NOT EXISTS problems_issue ON problems(issue_id,created_at,id)')
        connection.execute('CREATE INDEX IF NOT EXISTS issues_cold_candidates ON issues(updated_at) WHERE storage_zip IS NULL AND deleted_at IS NULL')
        connection.execute('CREATE INDEX IF NOT EXISTS issues_hot_page ON issues(space_id,number DESC) WHERE deleted_at IS NULL AND archived_at IS NULL')
        space_columns = {row['name'] for row in connection.execute('PRAGMA table_info(spaces)')}
        if 'prefix' not in space_columns:
            connection.execute('ALTER TABLE spaces ADD COLUMN prefix TEXT')
        connection.execute('CREATE TABLE IF NOT EXISTS space_prefixes(prefix TEXT PRIMARY KEY COLLATE NOCASE,space_id TEXT NOT NULL REFERENCES spaces(id))')
        connection.execute('CREATE TABLE IF NOT EXISTS issue_keys(key TEXT PRIMARY KEY COLLATE NOCASE,issue_id TEXT NOT NULL REFERENCES issues(id))')
        from .prefixes import suggest, reserve, remember_key, issue_key
        for space in connection.execute('SELECT * FROM spaces ORDER BY created_at,id').fetchall():
            if not space['prefix']:
                chosen = 'AGT' if space['id'] == 'default' else suggest(space['name'], connection)
                connection.execute('UPDATE spaces SET prefix=? WHERE id=?', (reserve(connection, chosen, space['id']), space['id']))
        for row in connection.execute('SELECT * FROM issues WHERE space_number IS NULL ORDER BY number').fetchall():
            # Preserve every already published AGT key, even for an older non-default space.
            remember_key(connection, row['id'], 'AGT-' + str(row['number']))
            connection.execute('UPDATE issues SET space_number=? WHERE id=?', (row['number'], row['id']))
            updated = connection.execute('SELECT * FROM issues WHERE id=?', (row['id'],)).fetchone()
            remember_key(connection, row['id'], issue_key(connection, updated))
        connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS issues_space_number ON issues(space_id,space_number)')
        connection.execute('PRAGMA user_version=3')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
