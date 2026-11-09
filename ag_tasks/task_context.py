"""Fresh, byte-bounded task handoff packets; harnesses own their model context."""

from hashlib import sha256
import json

from .issues import DomainError
from .memory_recommendation import memory_recommendation

MIN_BYTES, MAX_BYTES = 8192, 32768


def serialized(value):
    """Encode canonical UTF-8 JSON for exact transport budgets and stable digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def compact(value, path, omitted):
    """
    Bound section text and collections while explicitly naming every omitted path.

    Args:
        value: Validated task data; never a harness transcript or execution request.
        path: Packet-relative section name used in omission diagnostics.
        omitted: Mutable path-to-reason map shared by this packet builder.
    Returns:
        A detached bounded projection; input values are never changed.
    """
    if isinstance(value, str):
        if len(value) > 2048:
            omitted[path] = 'text_limit'
        return value[:2048]
    if isinstance(value, list):
        if len(value) > 20:
            omitted[path] = {'reason': 'item_limit', 'total': len(value)}
        return [compact(item, f'{path}/{index}', omitted) for index, item in enumerate(value[:20])]
    if isinstance(value, dict):
        return {key: compact(item, path + '/' + key, omitted) for key, item in value.items()}
    return value


class TaskContext:
    """Read current task evidence for any chat without changing work or acceptance."""

    def __init__(self, store):
        """Use existing task ownership and storage; there is no model client here."""
        self.store, self.db = store, store.db

    def get(self, issue_id=None, *, conversation_id=None, max_bytes=16384):
        """
        Build a fresh context by task or by a chat's current task association.

        Args:
            issue_id: Task ID/alias; supply exactly one of the two identities.
            conversation_id: Durable local chat binding ID, including other agents.
            max_bytes: Canonical UTF-8 packet budget, 8192..32768 inclusive.
        Returns:
            A versioned packet with source/content revisions, explicit omissions
            and references for fetching full evidence through existing read APIs.
        Raises:
            DomainError: Invalid identity/budget, an unbound chat or missing task.

        Reads may restore cold task content, just like existing detail reads.
        They do not renew task aging, start work, reconnect chats or accept results.
        """
        if (issue_id is None) == (conversation_id is None):
            raise DomainError('invalid_context_identity', 'Select exactly one task or chat identity.')
        if type(max_bytes) is not int or not MIN_BYTES <= max_bytes <= MAX_BYTES:
            raise DomainError('invalid_context_budget', 'Context budget must be 8192..32768 bytes.')
        with self.db.lock:
            binding = None
            if conversation_id is not None:
                chat = self.store.service.conversations.get(conversation_id)
                issue_id = chat['issueId']
                if issue_id is None:
                    raise DomainError('conversation_unbound', 'Bind this chat to a task before requesting context.', 409)
                binding = {'conversationId': chat['id'], 'revision': chat['revision']}
            row = self.store._row(issue_id)
            counts = self.db.connection.execute("SELECT status,COUNT(*) AS amount FROM issues WHERE parent_id=? AND kind!='review' AND deleted_at IS NULL GROUP BY status", (row['id'],)).fetchall()
            progress = {item['status']: item['amount'] for item in counts}
            issue = self.store._issue(row, child_progress=progress)
            sections = self._sections(issue)
            packet = self._pack(issue, binding, sections, max_bytes)
            return packet

    def _sections(self, issue):
        """
        Select evidence sections while retaining their exact source revisions.

        Args:
            issue: Current task projection read under the service owner lock.
        Returns:
            Detached context sections; notes/chats use explicit bounded pages.
            Raw audit, harness transcripts and reusable work credentials are absent.
        """
        connection, issue_id = self.db.connection, issue['id']
        comments = connection.execute('SELECT id,text,actor,created_at FROM comments WHERE issue_id=? ORDER BY id DESC LIMIT 4', (issue_id,)).fetchall()
        comment_count = connection.execute('SELECT COUNT(*) FROM comments WHERE issue_id=?', (issue_id,)).fetchone()[0]
        conversations = self.store.service.conversations.list(20, 0, issue_id)
        sections = {
            'criteria': {'dor': issue['dor'], 'dod': issue['dod']},
            'review': issue['criticReview'],
            'result': {'isCurrent': issue['currentResultIsCurrent'], 'value': issue['currentResult']},
            'description': issue['description'],
            'dependencies': {'items': issue['links'], 'total': issue['linksCount']},
            'documents': issue['documents'],
            'notes': {'items': [dict(row) for row in comments[:3]], 'total': comment_count},
            'work': {'state': issue['work']['state'], 'activeSession': issue['work']['activeSession'],
                     'pauseReason': issue['work'].get('pauseReason'), 'nextAction': issue['work'].get('nextAction')},
            'chats': conversations,
            'artifacts': issue['externalRefs'],
            'workspace': {'repoId': issue['repoId'], 'worktree': issue['worktree'], 'branch': issue['branch']},
        }
        if issue['kind'] == 'review' and issue['parentId']:
            parent = self.store._row(issue['parentId'])
            evidence = self.store.results.project(parent['id'], issue_row=parent)
            sections['reviewTarget'] = {'issueId': parent['id'], 'definitionRevision': parent['definition_revision'],
                                       'resultGeneration': parent['result_generation'],
                                       'reviewedHead': parent['reviewed_head'], **evidence}
        return sections

    @staticmethod
    def _pack(issue, binding, sections, max_bytes):
        """
        Prefer criteria and evidence within an exact canonical UTF-8 byte budget.

        Args:
            issue: Current task identity, lifecycle and source update timestamp.
            binding: Optional conversation ID/revision selected for this read.
            sections: Ordered source sections before projection or budget trimming.
            max_bytes: Validated maximum serialized packet length.
        Returns:
            Packet with source/content digests and explicit omission paths.
        Raises:
            DomainError: Required metadata alone exceeds the requested byte budget.
        """
        packet = {'contract': 'ag-tasks-context/v1', 'binding': binding,
                  'trust': 'task_data_not_execution_authority',
                  'memoryRecommendation': memory_recommendation(),
                  'issue': {name: issue[name] for name in ('id', 'key', 'title', 'status', 'priority', 'kind',
                            'spaceId', 'parentId', 'intendedRole', 'definitionRevision', 'resultGeneration', 'reviewedHead')},
                  'lifecycle': issue['lifecycle'], 'childProgress': issue['childProgress'],
                  'sources': {'task': '/api/v1/issues/' + issue['id'],
                              'context': '/api/v1/issues/' + issue['id'] + '/context'},
                  'sections': {}, 'omitted': {}, 'complete': True,
                  'sourceRevision': sha256(serialized({'issue': issue['updatedAt'], 'binding': binding,
                                                       'sections': sections})).hexdigest(),
                  'contentRevision': '0' * 64}
        for name, section in sections.items():
            omissions = {}
            projected = compact(section, name, omissions)
            packet['sections'][name] = projected
            packet['omitted'].update(omissions)
            # Reserve room for subsequent omission keys and the complete marker.
            if len(serialized(packet)) > max_bytes - 1024:
                del packet['sections'][name]
                for path in omissions:
                    packet['omitted'].pop(path, None)
                packet['omitted'][name] = 'byte_budget'
        notes = sections['notes']
        if notes['total'] > len(notes['items']) and 'notes' in packet['sections']:
            packet['omitted']['notes/items'] = {'reason': 'recent_only', 'total': notes['total']}
        if issue['linksTruncated']:
            packet['omitted']['dependencies/items'] = {'reason': 'source_page_limit', 'total': issue['linksCount']}
        if sections['chats']['hasMore']:
            packet['omitted']['chats/items'] = 'source_page_limit'
        packet['complete'] = not packet['omitted']
        packet['contentRevision'] = sha256(serialized(packet)).hexdigest()
        if len(serialized(packet)) > max_bytes:
            raise DomainError('context_budget_exceeded', 'Required context metadata exceeds the requested byte budget.', 413)
        return packet
