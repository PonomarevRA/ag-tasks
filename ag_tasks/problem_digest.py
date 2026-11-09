"""Bounded cross-task problem evidence for future improvements, without inferred causes or model calls."""

import json
from .issues import DomainError


def read(store, *, limit=20, offset=0, space_id=None):
    """Return at most 100 attributable reported problems; grouping is exact normalized title within this page only."""
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 9223372036854775807:
        raise DomainError('invalid_page', 'Problem digest page is invalid.')
    clauses, args = ['i.deleted_at IS NULL'], []
    if space_id is not None:
        from .issues import bounded_text
        args.append(bounded_text(space_id,128,empty=False))
        clauses.append('i.space_id=?')
    with store.db.lock:
        rows = store.db.connection.execute("SELECT p.id,p.issue_id,p.title,p.state,p.severity,p.actor,p.updated_at,p.details_preview,p.resolution_preview,i.storage_zip,i.intended_role,i.languages,i.systems,i.status AS issue_status,i.space_number,s.prefix,i.result_generation FROM problems p JOIN issues i ON i.id=p.issue_id JOIN spaces s ON s.id=i.space_id WHERE " + ' AND '.join(clauses) + ' ORDER BY p.updated_at DESC,p.id DESC LIMIT ? OFFSET ?', (*args,limit+1,offset)).fetchall()
    items, groups = [], {}
    for row in rows[:limit]:
        key = f"{row['prefix']}-{row['space_number']}"
        group = ' '.join(row['title'].casefold().split())
        groups[group] = groups.get(group,0) + 1
        items.append({'issueId':row['issue_id'],'key':key,'problemId':row['id'],'title':row['title'],
                      'state':row['state'],'severity':row['severity'],'details':row['details_preview'],
                      'resolution':row['resolution_preview'], 'actor':row['actor'],'role':row['intended_role'],
                      'languages':json.loads(row['languages']),'systems':json.loads(row['systems']),
                      'taskStatus':row['issue_status'],'resultGeneration':row['result_generation'],
                      'updatedAt':row['updated_at'],'summaryOnly':True,'contentAvailable':not row['storage_zip'] or bool(row['details_preview'] or row['resolution_preview'])})
    return {'contract':'ag-tasks-problems/v1','items':items,'groupsInPage':groups,'grouping':'exact-normalized-title; page only',
            'hasMore':len(rows)>limit,'nextOffset':offset+limit if len(rows)>limit else None,'provenance':'agent-reported evidence; causes and successful fixes are not inferred'}


def migrate(connection):
    """Add small hot summaries once; old cold content remains explicitly unavailable until a normal restore."""
    connection.execute('BEGIN IMMEDIATE')
    try:
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(problems)')}
        for name, source in [('details_preview','details'),('resolution_preview','resolution')]:
            if name not in columns:
                connection.execute(f"ALTER TABLE problems ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
                connection.execute(f"UPDATE problems SET {name}=substr({source},1,1024)")
        connection.execute('CREATE INDEX IF NOT EXISTS problems_digest ON problems(updated_at DESC,id DESC)')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
