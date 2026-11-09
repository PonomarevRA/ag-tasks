"""Optional task deadlines, separate from acceptance definitions and work ownership."""

from datetime import date
import re


def due_date(value):
    """
    Validate an optional inclusive UTC calendar deadline.

    Args:
        value: None clears the deadline; otherwise an ASCII YYYY-MM-DD date.
    Returns:
        The unchanged date string or None.
    Raises:
        DomainError: The input is not a real, strictly formatted calendar date.
    """
    from .issues import DomainError
    if value is None:
        return None
    if type(value) is not str or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise DomainError('invalid_due_date', 'Use a UTC calendar date YYYY-MM-DD, or null to clear.')
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise DomainError('invalid_due_date', 'Choose a valid calendar date.') from error
    return value


class Scheduling:
    """Store scheduling metadata without changing definition revisions or leases."""

    def __init__(self, store):
        """
        Initialize the additive sidecar under the owner's transaction.

        Args:
            store: The IssueStore that owns the database and its process lock.
        """
        self.store = store
        with store.db.transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS issue_management (
                issue_id TEXT PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
                due_date TEXT
            )''')

    def read(self, issue_id):
        """
        Read a deadline using the caller's existing store lock.

        Args:
            issue_id: Canonical issue identifier.
        Returns:
            The stored calendar date, or None for unscheduled cards.
        """
        row = self.store.db.connection.execute(
            'SELECT due_date FROM issue_management WHERE issue_id=?', (issue_id,)).fetchone()
        return row['due_date'] if row else None

    def write(self, connection, issue_id, value):
        """
        Upsert a validated deadline inside the issue mutation's transaction.

        Args:
            connection: The active issue transaction; this method never commits it.
            issue_id: Existing canonical issue identifier.
            value: Validated date string or None to clear.
        """
        connection.execute('''INSERT INTO issue_management(issue_id,due_date) VALUES(?,?)
            ON CONFLICT(issue_id) DO UPDATE SET due_date=excluded.due_date''', (issue_id, value))
