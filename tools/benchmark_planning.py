"""Reproduce local planning-storage timings on disposable synthetic state, never a user's task database."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import platform
import sqlite3
import statistics
import tempfile
import time

from ag_tasks.issues import IssueStore


def measure(action, repeats=12):
    """Time completed synchronous operations; warm-up and run count remain explicit in the report."""
    action()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        action()
        samples.append((time.perf_counter() - start) * 1000)
    ordered = sorted(samples)
    return {'runs': repeats, 'warmups': 1, 'medianMs': round(statistics.median(samples), 3),
            'p95Ms': round(ordered[min(len(samples) - 1, int(len(samples) * .95))], 3)}


def run(count):
    """Measure a10k-card index page plus Markdown ZIP roundtrips; this is not an end-to-end native/LLM benchmark."""
    with tempfile.TemporaryDirectory(prefix='ag-tasks-planning-benchmark-') as directory:
        store = IssueStore(directory)
        try:
            with store.db.transaction() as connection:
                for index in range(count):
                    store._insert({'title': f'Synthetic task {index}', 'description': 'Bounded task metadata. ' * 20,
                                   'estimatedComplexity': {'points': 3, 'reason': 'Synthetic fixture'}}, 'benchmark', connection)
            fixtures = []
            for index in range(3):
                task = store.planning.create_plan({'title': f'Archive fixture {index}', 'markdown': '# Planning\n' + 'Repeated evidence and validation.\n' * 1000})
                for number in range(20):
                    store.comment(task['id'], f'Comment {number}: ' + 'Repeated diagnostic. ' * 100)
                fixtures.append(task['id'])
            page = measure(lambda: store.list(space_id='default', limit=200))
            columns = store.db.connection.execute('EXPLAIN QUERY PLAN SELECT id FROM issues WHERE space_id=? AND deleted_at IS NULL AND archived_at IS NULL ORDER BY number DESC LIMIT 200', ('default',)).fetchall()
            before = store.db.connection.execute('PRAGMA freelist_count').fetchone()[0]
            archive_samples, restore_samples = [], []
            cutoff = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            for identifier in fixtures:
                with store.db.transaction() as connection:
                    connection.execute('UPDATE issues SET updated_at=? WHERE id=?', (cutoff, identifier))
                start = time.perf_counter()
                assert store.cold.sweep(limit=1) == [identifier]
                archive_samples.append((time.perf_counter() - start) * 1000)
                assert next(item for item in store.list(limit=200) if item['id'] == identifier)['storage']['tier'] == 'zip'
                start = time.perf_counter()
                restored = store.get(identifier)
                restore_samples.append((time.perf_counter() - start) * 1000)
                assert restored['commentsCount'] == 20 and len(store.documents.get(identifier, restored['documents'][0]['id'])['content']) > 30000
            return {'scope': 'isolated local Python/SQLite; no installed apps or user state modified',
                    'environment': {'os': platform.platform(), 'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
                    'fixtureTasks': count + 3, 'pageSize': 200, 'metadataPage': page,
                    'archiveMs': [round(item, 3) for item in archive_samples], 'restoreMs': [round(item, 3) for item in restore_samples],
                    'queryPlan': [list(row) for row in columns], 'freePagesBefore': before,
                    'sqlitePolicy': 'WAL + synchronous FULL; freed pages reused, no blocking VACUUM or weaker durability',
                    'limitations': 'Synthetic metadata/Markdown only; p95 is a small-sample order statistic. No native RSS, browser heap, saturation or multi-user DB claim.'}
        finally:
            store.close()


def main():
    """Write the reproducible report outside any task data directory selected by the application."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--tasks', type=int, default=10000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 200 <= args.tasks <= 100000:
        parser.error('--tasks must be200..100000')
    result = run(args.tasks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
