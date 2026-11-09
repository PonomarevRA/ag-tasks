"""Activity ordering with bounded, filter-bound continuation tokens for observed sessions."""

import base64
from datetime import datetime
import json

from .issues import DomainError


def ordering(sort):
    """
    Return the fixed SQL direction and continuation comparison for newest/oldest.

    Args:
        sort: Public order name, either newest (default) or oldest.
    Returns:
        SQL direction and tuple comparison operator, chosen only from constants.
    Raises:
        DomainError: The order is not supported.
    """
    if sort == 'newest':
        return 'DESC', '<'
    if sort == 'oldest':
        return 'ASC', '>'
    raise DomainError('invalid_sort', 'Use newest or oldest session ordering.')


def continuation(after, sort, issue_id, active_only, connection):
    """
    Decode an activity/id boundary, rejecting tokens reused with different filters.

    Args:
        after: Zero for the first page, an opaque token, or a legacy numeric rowid.
        sort: Validated order name included in the token's scope.
        issue_id: Resolved local task ID or None for all tasks.
        active_only: Whether only live MCP connections are included.
        connection: Owning SQLite connection for resolving legacy rowids.
    Returns:
        Activity timestamp and ID, or None for the first page.
    Raises:
        DomainError: The cursor is malformed, out of scope, or no longer resolvable.
    """
    if type(after) not in (int, str) or len(str(after)) > 1024:
        raise DomainError('invalid_page', 'Use a returned session continuation token.')
    if str(after) == '0':
        return None
    try:
        if str(after).isdigit():
            number = int(after)
            if not 0 < number <= 2**63 - 1:
                raise ValueError('Invalid rowid')
            row = connection.execute('SELECT updated_at,id FROM service_sessions WHERE rowid=?', (number,)).fetchone()
            if row is None:
                raise ValueError('Missing cursor')
            return row['updated_at'], row['id']
        decoded = base64.b64decode(after, altchars=b'-_', validate=True)
        value = json.loads(decoded)
        if type(value) is not list or len(value) != 6:
            raise ValueError('Invalid token')
        version, scope, date, session_id, task, active = value
        if version != 1 or scope != sort or task != issue_id or type(active) is not bool or active != active_only:
            raise ValueError('Different cursor scope')
        if type(date) is not str or datetime.fromisoformat(date).tzinfo is None:
            raise ValueError('Invalid timestamp')
        if type(session_id) is not str or not 1 <= len(session_id) <= 128:
            raise ValueError('Invalid identity')
        return date, session_id
    except (ValueError, TypeError, UnicodeError, OverflowError) as error:
        raise DomainError('invalid_page', 'Use the returned token with the same session filters and order.') from error


def cursor(row, sort, issue_id, active_only):
    """
    Encode the last row's activity and identity without depending on a retained row.

    Args:
        row: Last emitted SQLite session row, including updated_at and id.
        sort: Validated order name.
        issue_id: Resolved task filter or None.
        active_only: Live-connection filter for the page.
    Returns:
        URL-safe opaque continuation token accepted after retention deletes the row.
    """
    value = [1, sort, row['updated_at'], row['id'], issue_id, active_only]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()
