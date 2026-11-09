"""Bounded task classification shared by HTTP, MCP, results and generated code reviews."""

import json
import unicodedata


def labels(value, field):
    """Normalize up to sixteen literal labels; preserve spelling, deduplicate ignoring case, never interpret paths."""
    from .issues import DomainError
    if type(value) is not list or len(value) > 16:
        raise DomainError('invalid_labels', f'{field} must be an array of at most sixteen labels.')
    result, seen = [], set()
    for item in value:
        if type(item) is not str:
            raise DomainError('invalid_labels', f'{field} labels must be strings.')
        item = unicodedata.normalize('NFC', item.strip())
        if not item or len(item) > 64 or any(unicodedata.category(char).startswith('C') for char in item):
            raise DomainError('invalid_labels', f'{field} labels must contain 1–64 visible characters.')
        if item.casefold() not in seen:
            seen.add(item.casefold())
            result.append(item)
    return result


def validate(values):
    """Repositories or language labels imply code; draft code tasks may still lack execution context."""
    from .issues import DomainError
    code = values.get('codeTask', False)
    if type(code) is not bool:
        raise DomainError('invalid_code_task', 'codeTask must be a boolean.')
    languages = labels(values.get('languages', []), 'languages')
    systems = labels(values.get('systems', []), 'systems')
    return {'codeTask': bool(code or values.get('repoId') or languages), 'languages': languages, 'systems': systems}


def snapshot(row):
    """Read classification from the transaction row without issuing additional card queries."""
    return {'codeTask': bool(row['code_task']), 'languages': json.loads(row['languages']), 'systems': json.loads(row['systems'])}


def require_complete(context):
    """Require reported languages and systems before code results are accepted for review."""
    from .issues import DomainError
    if context['codeTask'] and (not context['languages'] or not context['systems']):
        raise DomainError('code_context_required', 'Code results require at least one language and one system label.', 409)


def stage(issue):
    """Expose the current review gate, preserving current-result provenance owned by Results.project."""
    if not issue['codeTask'] or issue['kind'] == 'review':
        return None
    if issue.get('storage', {}).get('tier') == 'zip':
        return 'unknown'
    report = issue.get('criticReview')
    if report:
        return 'passed' if report['verdict'] == 'pass' else 'rework'
    if issue['status'] == 'in_review':
        return 'in_review'
    return 'pending'


def migrate(connection):
    """Add classification atomically; retain issue identities and classify existing repository work as code."""
    connection.execute('BEGIN IMMEDIATE')
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(issues)')}
        for name, declaration in {'code_task': 'INTEGER NOT NULL DEFAULT 0', 'languages': "TEXT NOT NULL DEFAULT '[]'", 'systems': "TEXT NOT NULL DEFAULT '[]'"}.items():
            if name not in columns:
                connection.execute(f'ALTER TABLE issues ADD COLUMN {name} {declaration}')
        connection.execute('UPDATE issues SET code_task=1 WHERE repo_id IS NOT NULL AND code_task=0')
        connection.execute('PRAGMA user_version=4')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
