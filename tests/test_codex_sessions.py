"""Local chat discovery combines real writer evidence with bounded, explicit lifecycle facts."""

from datetime import datetime, timezone
import json
import sqlite3

from ag_tasks.codex_sessions import CodexSessions

THREAD = '01a1781b-2ca0-7111-abac-d9a47b1941ca'
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


def fixture(home, path=None):
    """Make an isolated minimal source database; never use the live user store as a test fixture."""
    (home / 'sessions').mkdir()
    rollout = path or home / 'sessions/one.jsonl'
    connection = sqlite3.connect(home / 'state_5.sqlite')
    connection.execute('CREATE TABLE threads(id TEXT,title TEXT,rollout_path TEXT,archived INTEGER,updated_at INTEGER)')
    connection.execute('INSERT INTO threads VALUES(?,?,?,?,?)', (THREAD, 'Private title', str(rollout), 0, 1))
    connection.commit();connection.close()
    return rollout


def event(kind, turn='one', when='2026-10-26T11:59:50Z'):
    """Return lifecycle metadata and an intentionally private body that must never escape the adapter."""
    return json.dumps({'type':'event_msg', 'timestamp':when,
                       'payload':{'type':kind,'turn_id':turn,'private_body':'do-not-export'}}) + '\n'


def test_existing_files_without_live_writers_are_not_sessions(tmp_path):
    rollout = fixture(tmp_path);rollout.write_text(event('task_started'))
    source = CodexSessions(tmp_path, writers=lambda _: [], now=lambda: NOW)
    assert source.snapshot() == {'items':[], 'source':'codex-local', 'available':True, 'truncated':False}


def test_incremental_lifecycle_partial_lines_and_stale_running(tmp_path):
    rollout = fixture(tmp_path);rollout.write_text(event('task_started'))
    clock = [0]
    source = CodexSessions(tmp_path, writers=lambda _: [THREAD], clock=lambda: clock[0], now=lambda: NOW)
    observed = source.snapshot()
    assert observed['items'][0]['state'] == 'running'
    assert 'private_body' not in json.dumps(observed)
    assert 'do-not-export' not in json.dumps(source.tails)
    completed = event('task_complete')
    with rollout.open('a') as stream:stream.write(completed[:-1])
    clock[0] += 11
    assert source.snapshot()['items'][0]['state'] == 'running'
    with rollout.open('a') as stream:stream.write('\n')
    clock[0] += 11
    assert source.snapshot()['items'][0]['state'] == 'idle'
    with rollout.open('a') as stream:stream.write(event('task_started', 'two', '2026-10-26T11:00:00Z'))
    clock[0] += 11
    assert source.snapshot()['items'][0]['state'] == 'unknown'


def test_source_failure_and_path_escape_do_not_become_empty_success_or_transcript_import(tmp_path):
    rollout = fixture(tmp_path, tmp_path / 'outside.jsonl');rollout.write_text(event('task_started'))
    source = CodexSessions(tmp_path, writers=lambda _: [THREAD], now=lambda: NOW)
    assert source.snapshot()['items'][0]['state'] == 'unknown'
    def unavailable(_):
        raise OSError('source unavailable')
    assert CodexSessions(tmp_path, writers=unavailable).snapshot()['available'] is False


def test_unknown_lifecycle_and_wrong_turn_terminal_do_not_claim_execution(tmp_path):
    rollout=fixture(tmp_path);rollout.write_text(event('item_completed'))
    source=CodexSessions(tmp_path, writers=lambda _:[THREAD], now=lambda:NOW)
    assert source.snapshot()['items'][0]['state']=='unknown'
    rollout.write_text(event('task_started')+event('task_complete','other'))
    source=CodexSessions(tmp_path,writers=lambda _:[THREAD],now=lambda:NOW)
    assert source.snapshot()['items'][0]['state']=='unknown'


def test_visible_name_is_used_without_exporting_initial_prompt(tmp_path):
    """The SQLite title column can hold prompt text; only the visible name belongs in the session list."""
    rollout=fixture(tmp_path);rollout.write_text(event('task_started'))
    with sqlite3.connect(tmp_path/'state_5.sqlite') as connection:
        connection.execute('ALTER TABLE threads ADD COLUMN name TEXT')
        connection.execute('UPDATE threads SET name=?,title=?', ('Visible chat name', 'private-initial-prompt'))
    snapshot=CodexSessions(tmp_path,writers=lambda _:[THREAD],now=lambda:NOW).snapshot()
    assert snapshot['items'][0]['title']=='Visible chat name'
    assert 'private-initial-prompt' not in json.dumps(snapshot)
