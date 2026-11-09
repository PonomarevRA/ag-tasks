"""Stable issue aliases and unique human-selected space prefixes; four characters is advice, not a schema cap."""

from hashlib import sha256
import re

from .issues import DomainError, bounded_text


def prefix(value):
    """Normalize an explicit ASCII prefix case-insensitively, with a technical32-character envelope."""
    value = bounded_text(value, 32, empty=False).strip().upper()
    if not re.fullmatch('[A-Z][A-Z0-9]*', value):
        raise DomainError('invalid_prefix', 'Use Latin letters/numbers, starting with a letter. Up to4 characters is recommended.')
    return value


def suggest(name, connection):
    """Choose a readable short default; a reserved prefix is never silently reassigned to another space."""
    words = re.findall('[A-Za-z0-9]+', name)
    base = ''.join(word[0] for word in words)[:4] if len(words) > 1 else (words[0][:4] if words else 'SP')
    if not base or not base[0].isalpha():
        base = 'SP'
    base = base.upper()
    candidate = base
    for number in range(10000):
        if connection.execute('SELECT 1 FROM space_prefixes WHERE prefix=? COLLATE NOCASE', (candidate,)).fetchone() is None:
            return candidate
        candidate = base[:3] + str(number + 1)
    return 'SP' + sha256(name.encode()).hexdigest()[:12].upper()


def reserve(connection, value, space_id):
    """Keep both present and former prefixes reserved so old task keys remain unambiguous forever."""
    value = prefix(value)
    owner = connection.execute('SELECT space_id FROM space_prefixes WHERE prefix=? COLLATE NOCASE', (value,)).fetchone()
    if owner and owner[0] != space_id:
        raise DomainError('prefix_conflict', 'This prefix belongs to another space, including its historical aliases.', 409)
    connection.execute('INSERT OR IGNORE INTO space_prefixes(prefix,space_id) VALUES(?,?)', (value, space_id))
    return value


def issue_key(connection, row):
    """Project the current space prefix and per-space number; opaque IDs remain the canonical relation identity."""
    chosen = row['space_prefix'] if 'space_prefix' in row.keys() else connection.execute('SELECT prefix FROM spaces WHERE id=?', (row['space_id'],)).fetchone()[0]
    return f"{chosen}-{row['space_number']}"


def remember_key(connection, issue_id, key):
    """Persist a case-insensitive alias, rejecting a collision instead of redirecting an existing reference."""
    owner = connection.execute('SELECT issue_id FROM issue_keys WHERE key=? COLLATE NOCASE', (key,)).fetchone()
    if owner and owner[0] != issue_id:
        raise DomainError('key_conflict', 'The requested task key is already an alias for another task.', 409)
    connection.execute('INSERT OR IGNORE INTO issue_keys(key,issue_id) VALUES(?,?)', (key, issue_id))


def allocate(connection, space_id):
    """Allocate under the existing write transaction; include deleted issues so numbers are never reused."""
    number = connection.execute('SELECT COALESCE(MAX(space_number),0)+1 FROM issues WHERE space_id=?', (space_id,)).fetchone()[0]
    chosen = connection.execute('SELECT prefix FROM spaces WHERE id=?', (space_id,)).fetchone()[0]
    while connection.execute('SELECT 1 FROM issue_keys WHERE key=? COLLATE NOCASE', (f'{chosen}-{number}',)).fetchone():
        number += 1
    return number
