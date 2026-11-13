"""Immutable result generations and explicit critic reports, separate from human Git approval."""

import json
from uuid import uuid4

from .memory_recommendation import memory_recommendation
from .criteria import bounded_json, failures, result as validate_result, strings
from .issues import DomainError, bounded_text, timestamp
from .code_context import snapshot, require_complete


class Results:
    """Bind each critic decision to exact result/definition revisions and the reviewed Git head."""

    def __init__(self, store):
        """Reuse the issue store's transaction, scope and fixed route actor without owning another database."""
        self.store = store

    def project(self, issue_id, *, details=False, compact=False, issue_row=None):
        """Project current evidence; only detail reads decode bounded history. An optional row shares the caller's locked snapshot."""
        connection = self.store.db.connection
        limit = 10 if details else 1
        rows = connection.execute('SELECT *,(SELECT COUNT(*) FROM results WHERE issue_id=?) AS total,(SELECT id FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1) AS latest_work_id FROM results WHERE issue_id=? ORDER BY generation DESC LIMIT ?', (issue_id, issue_id, issue_id, limit)).fetchall()
        total = rows[0]['total'] if rows else 0
        current = self._result(rows[0]) if rows else None
        projected = self._history(rows, current) if details else []
        if compact and current:
            summary = current['summary'][:512]
            current = {key: current[key] for key in ('generation', 'definitionRevision', 'producerAgentId', 'reviewedHead', 'createdAt')}
            current['summary'] = summary
        report = connection.execute('SELECT * FROM review_reports WHERE parent_id=? ORDER BY id DESC LIMIT 1', (issue_id,)).fetchone()
        issue = issue_row if issue_row is not None else connection.execute('SELECT * FROM issues WHERE id=?', (issue_id,)).fetchone()
        latest_work = {'id': rows[0]['latest_work_id']} if rows and rows[0]['latest_work_id'] else None
        if report and (not rows or report['issue_id'] != issue['critic_review_id'] or report['generation'] != rows[0]['generation'] or
                       report['definition_revision'] != issue['definition_revision'] or rows[0]['definition_revision'] != issue['definition_revision'] or
                       report['reviewed_head'] != rows[0]['reviewed_head'] or rows[0]['reviewed_head'] != issue['reviewed_head'] or
                       latest_work is None or latest_work['id'] != rows[0]['producer_session_id']):
            report = None
        fresh = bool(rows and latest_work and latest_work['id'] == rows[0]['producer_session_id'] and
                     rows[0]['definition_revision'] == issue['definition_revision'] and rows[0]['reviewed_head'] == issue['reviewed_head'])
        output = {'currentResult': current, 'currentResultIsCurrent': fresh, 'resultsCount': total, 'criticReview': self._report(report) if report else None}
        if compact and output['criticReview']:
            output['criticReview'] = {key: output['criticReview'][key] for key in ('verdict', 'generation', 'definitionRevision', 'reviewedHead', 'agentId', 'createdAt')}
            output['criticReview']['summary'] = report['summary'][:512]
        if details:
            reports = connection.execute('SELECT * FROM review_reports WHERE parent_id=? ORDER BY id DESC LIMIT 11', (issue_id,)).fetchall()
            history, size = [], 0
            for saved in reports[:10]:
                entry = self._report(saved)
                amount = len(json.dumps(entry, ensure_ascii=True).encode('ascii'))
                if size + amount > 128 * 1024:
                    break
                history.append(entry)
                size += amount
            output.update(results=projected, resultsTruncated=len(projected) < total, reviewReports=history, reviewReportsTruncated=len(reports) > len(history))
        return output

    def _history(self, rows, current):
        """Decode newest-first detail history within 128KiB, reusing the already decoded current result."""
        projected, size = [], 0
        for index, row in enumerate(rows):
            value = current if index == 0 else self._result(row)
            amount = len(json.dumps(value, ensure_ascii=True).encode('ascii'))
            if size + amount > 128 * 1024:
                break
            size += amount
            projected.append(value)
        return projected

    @staticmethod
    def _result(row):
        """Project immutable content with exact provenance used by critic freshness checks."""
        return {**json.loads(row['payload']), 'generation': row['generation'], 'definitionRevision': row['definition_revision'],
                'producerAgentId': row['producer_agent_id'], 'reviewedHead': row['reviewed_head'], 'createdAt': row['created_at']}

    @staticmethod
    def _report(row):
        """Reports remain declared evidence; no shared-agent caller is labelled authenticated or human-approved."""
        return {'verdict': row['verdict'], 'summary': row['summary'], 'findings': json.loads(row['findings']),
                'evidence': row['evidence'], 'generation': row['generation'], 'definitionRevision': row['definition_revision'],
                'reviewedHead': row['reviewed_head'], 'agentId': row['agent_id'], 'createdAt': row['created_at']}

    def supersede(self, connection, parent_id, reason, generation):
        """Stop only obsolete generated critic work; preserve ordinary children, completed reports and audit."""
        rows = connection.execute("SELECT * FROM issues WHERE parent_id=? AND kind='review' AND status NOT IN ('done','cancelled')", (parent_id,)).fetchall()
        for old in rows:
            session = connection.execute("SELECT * FROM work_sessions WHERE issue_id=? AND state='running'", (old['id'],)).fetchone()
            if session:
                self.store.execution._close(connection, session)
            connection.execute("UPDATE issues SET status='cancelled',updated_at=? WHERE id=?", (timestamp(), old['id']))
            self.store.reviews.cancel_waiting(connection, old['id'])
            self.store._audit(connection, old['id'], 'critic_superseded', 'system', {'newGeneration': generation, 'reason': reason, 'parentId': parent_id,
                'criticReviewId': old['id'], 'fromStatus': old['status'], 'toStatus': 'cancelled'})

    def guard(self, issue_id, connection, *, complete=False, proposed=None):
        """Apply identical completion rules to generic status edits and final Git merge admission."""
        issue = self.store._row(issue_id, connection)
        if issue['kind'] == 'review':
            raise DomainError('critic_report_required', 'Use the dedicated critic report operation.', 409)
        require_complete(snapshot(issue))
        values = proposed or {'dod': json.loads(issue['dod'])}
        if not (proposed or {}).get('intendedRole', issue['intended_role']):
            raise DomainError('role_required', 'An intended role is required before review/completion.', 409)
        problems = failures(values['dod'])
        current = connection.execute('SELECT * FROM results WHERE issue_id=? ORDER BY generation DESC LIMIT 1', (issue_id,)).fetchone()
        latest_work = connection.execute('SELECT id,state FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (issue_id,)).fetchone()
        if problems or current is None or current['definition_revision'] != issue['definition_revision']:
            raise DomainError('result_required', 'Submit current structured results with a nonempty passed Definition of Done.', 409)
        if latest_work is None or latest_work['state'] == 'running' or latest_work['id'] != current['producer_session_id']:
            raise DomainError('stale_result', 'New producer work requires a fresh structured result and critic review.', 409)
        if issue['repo_id'] and current['reviewed_head'] != issue['reviewed_head']:
            raise DomainError('stale_result', 'Capture and submit results for the latest reviewed Git head.', 409)
        if not complete:
            return
        unfinished = connection.execute("SELECT 1 FROM issues WHERE parent_id=? AND kind!='review' AND status NOT IN ('done','cancelled') LIMIT 1", (issue_id,)).fetchone()
        if unfinished:
            raise DomainError('children_unfinished', 'Finish all ordinary child issues before completing this parent.', 409)
        report = connection.execute('SELECT * FROM review_reports WHERE parent_id=? ORDER BY id DESC LIMIT 1', (issue_id,)).fetchone()
        if (report is None or report['verdict'] != 'pass' or report['generation'] != current['generation'] or
                report['definition_revision'] != issue['definition_revision'] or report['reviewed_head'] != current['reviewed_head']):
            raise DomainError('critic_review_required', 'A current distinct declared critic must pass this result generation.', 409)

    def submit(self, issue_id, value, actor='agent'):
        """Close real producer work and append one immutable result plus a fresh critic child, or persist definition rework."""
        if type(value) is not dict or set(value) != {'agentId', 'sessionId', 'sessionKey', 'result'}:
            raise DomainError('invalid_result', 'Explicit producer work session and structured result are required.')
        supplied = self.store.execution.credentials({key: value[key] for key in ('agentId', 'sessionId', 'sessionKey')})
        content = validate_result(value['result'])
        self.store.execution.expire(issue_id)
        # A repository result captures real current commit metadata, never a supplied SHA.
        issue_before = self.store.get(issue_id)
        if issue_before['repoId']:
            from .git_workflow import GitWorkflow
            GitWorkflow(self.store).review(issue_id)
        error = None
        with self.store.db.transaction() as connection:
            issue = self.store._row(issue_id, connection)
            self.store.execution.editable(issue)
            if issue['kind'] == 'review':
                raise DomainError('critic_report_required', 'Critic tasks use review-report, not producer results.', 409)
            session = self.store.execution.session(connection, issue_id, supplied)
            context = snapshot(issue)
            require_complete(context)
            content['workContext'] = context
            problems = failures(json.loads(issue['dod']))
            if not issue['intended_role']:
                problems.append({'id': 'intendedRole', 'text': 'Intended role is required', 'reason': 'role_required'})
            if problems:
                error = self.store.execution.rework(connection, issue_id, actor, problems, 'completion_definition_failed')
            else:
                now, generation = timestamp(), issue['result_generation'] + 1
                self.store.execution._close(connection, session)
                self.supersede(connection, issue_id, 'new_result', generation)
                connection.execute('INSERT INTO results(issue_id,generation,definition_revision,producer_agent_id,reviewed_head,payload,created_at,producer_session_id) VALUES(?,?,?,?,?,?,?,?)',
                                   (issue_id, generation, issue['definition_revision'], supplied['agentId'], issue['reviewed_head'], json.dumps(content), now, session['id']))
                critic = uuid4().hex
                definition = [{'id': 'current-result', 'text': 'Review the latest result and its evidence independently', 'passed': True,
                               'evidence': f'Result generation {generation}; definition revision {issue["definition_revision"]}'}]
                completion = [{'id': 'critic-report', 'text': 'Declare verdict, findings and supporting evidence', 'passed': False, 'evidence': ''},
                              {'id': 'current-revision', 'text': 'Bind the report to the current result, definition and Git head', 'passed': False, 'evidence': ''}]
                connection.execute('INSERT INTO issues(id,title,description,status,priority,parent_id,external_refs,created_at,updated_at,space_id,kind,intended_role,dor,dod) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                   (critic, ('Code review: ' if context['codeTask'] else 'Critic review: ') + issue['title'][:230], 'Declared independent review; current PASS completes work, but never approves a merge.', 'ready', issue['priority'], issue_id,
                                    '[]', now, now, issue['space_id'], 'review', 'critic', json.dumps(definition), json.dumps(completion)))
                connection.execute('UPDATE issues SET code_task=?,languages=?,systems=? WHERE id=?',
                                   (int(context['codeTask']), json.dumps(context['languages']), json.dumps(context['systems']), critic))
                from .prefixes import allocate, issue_key, remember_key
                connection.execute('UPDATE issues SET space_number=? WHERE id=?', (allocate(connection, issue['space_id']), critic))
                remember_key(connection, critic, issue_key(connection, self.store._row(critic, connection)))
                connection.execute("UPDATE issues SET result_generation=?,critic_review_id=?,status='in_review',updated_at=? WHERE id=?", (generation, critic, now, issue_id))
                self.store._audit(connection, issue_id, 'result_submitted', actor, {'generation': generation, 'criticReviewId': critic, 'definitionRevision': issue['definition_revision'],
                    'agentId': session['agent_id'], 'sessionId': session['id'], 'fromStatus': issue['status'], 'toStatus': 'in_review'})
                self.store._audit(connection, critic, 'critic_review_created', 'system', {'generation': generation, 'parentId': issue_id,
                    'definitionRevision': issue['definition_revision'], 'criticReviewId': critic, 'toStatus': 'ready'})
                self.store.reviews.route_new(critic, issue)
        if error:
            raise error
        self.store.execution.prune()
        response = self.store.get(issue_id)
        response['memoryRecommendation'] = memory_recommendation()
        return response

    def report(self, critic_id, value, actor='agent'):
        """Accept only a current-generation critic's explicit work session and distinct declared identity."""
        fields = {'agentId', 'sessionId', 'sessionKey', 'generation', 'definitionRevision', 'reviewedHead', 'verdict', 'summary', 'findings', 'evidence'}
        if type(value) is not dict or set(value) != fields or value['verdict'] not in ('pass', 'rework'):
            raise DomainError('invalid_review_report', 'Invalid critic report fields.')
        for key in ('generation', 'definitionRevision'):
            if type(value[key]) is not int or value[key] < 0:
                raise DomainError('invalid_review_report', 'Exact nonnegative revision numbers are required.')
        summary = bounded_text(value['summary'], 4096, empty=False)
        evidence = bounded_text(value['evidence'], 4096, empty=False)
        findings = strings(value['findings'], 50, 2048)
        if value['verdict'] == 'rework' and not findings:
            raise DomainError('review_findings_required', 'A rejected review requires at least one concrete reason for rework.')
        bounded_json({'summary': summary, 'findings': findings, 'evidence': evidence}, 'invalid_review_report')
        supplied = self.store.execution.credentials({key: value[key] for key in ('agentId', 'sessionId', 'sessionKey')})
        self.store.execution.expire(critic_id)
        before = self.store.get(critic_id)
        if before['kind'] != 'review' or not before['parentId']:
            raise DomainError('critic_task_required', 'Only a generated critic child accepts a report.', 409)
        parent_before = self.store.get(before['parentId'])
        if parent_before['repoId']:
            from .git_workflow import GitWorkflow
            GitWorkflow(self.store).review(parent_before['id'])
        with self.store.db.transaction() as connection:
            critic = self.store._row(critic_id, connection)
            parent = self.store._row(critic['parent_id'], connection)
            self.store.execution.editable(parent)
            self.store.execution.editable(critic)
            session = self.store.execution.session(connection, critic_id, supplied)
            current = connection.execute('SELECT * FROM results WHERE issue_id=? ORDER BY generation DESC LIMIT 1', (parent['id'],)).fetchone()
            latest_work = connection.execute('SELECT id FROM work_sessions WHERE issue_id=? ORDER BY rowid DESC LIMIT 1', (parent['id'],)).fetchone()
            if (current is None or parent['critic_review_id'] != critic_id or value['generation'] != current['generation'] or
                    value['definitionRevision'] != parent['definition_revision'] or current['definition_revision'] != parent['definition_revision'] or
                    value['reviewedHead'] != current['reviewed_head'] or current['reviewed_head'] != parent['reviewed_head'] or
                    parent['status'] != 'in_review' or latest_work is None or latest_work['id'] != current['producer_session_id']):
                raise DomainError('stale_review', 'Result, definition or Git head changed; a fresh critic task is required.', 409)
            if supplied['agentId'] == current['producer_agent_id']:
                raise DomainError('distinct_critic_required', 'Declare a critic identity different from the result producer.', 409)
            connection.execute('INSERT INTO review_reports(issue_id,parent_id,generation,definition_revision,reviewed_head,agent_id,verdict,summary,findings,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                               (critic_id, parent['id'], current['generation'], parent['definition_revision'], current['reviewed_head'], supplied['agentId'], value['verdict'], summary, json.dumps(findings), evidence, timestamp()))
            self.store.execution._close(connection, session)
            self.store.reviews.cancel_waiting(connection, critic_id)
            completion = json.loads(critic['dod'])
            for item in completion:
                item['passed'] = True
                item['evidence'] = ('Declared report: ' + evidence)[:2048] if item['id'] == 'critic-report' else f'Server matched result {current["generation"]}, definition {parent["definition_revision"]}, head {current["reviewed_head"] or "non-code"}'
            connection.execute("UPDATE issues SET status='done',dod=?,updated_at=? WHERE id=?", (json.dumps(completion), timestamp(), critic_id))
            if value['verdict'] == 'rework':
                connection.execute("UPDATE issues SET status='rework',updated_at=? WHERE id=?", (timestamp(), parent['id']))
            self.store._audit(connection, parent['id'], 'critic_reported', actor, {'verdict': value['verdict'], 'generation': current['generation'], 'agentId': supplied['agentId'], 'summary': summary, 'findings': findings,
                'sessionId': session['id'], 'criticReviewId': critic_id, 'definitionRevision': parent['definition_revision'],
                'reason': 'critic_pass' if value['verdict'] == 'pass' else 'critic_rework',
                'fromStatus': parent['status'], 'toStatus': 'rework' if value['verdict'] == 'rework' else parent['status']})
            if value['verdict'] == 'pass':
                from .completion import complete_ready
                complete_ready(self.store, connection, parent['id'])
            self.store.service.process.evidence.capture({
                'issueId': parent['id'], 'sourceId': 'critic:' + critic_id,
                'outcome': 'accepted' if value['verdict'] == 'pass' else 'rework',
                'coverage': 'Current result and distinct declared critic report; tool artifacts require separate verification.'})
        self.store.execution.prune()
        response = self.store.get(parent['id'])
        response['memoryRecommendation'] = memory_recommendation()
        return response
