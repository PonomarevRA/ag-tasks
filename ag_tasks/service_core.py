"""One domain command surface shared by HTTP, MCP and UI; protocols are data adapters."""

from .command_core import CommandJournal
from .memory_recommendation import CONTRACT as MEMORY_RECOMMENDATION_CONTRACT
from .issues import DomainError
from .service_events import SessionEvents
from .service_values import fields
from .agent_queue import QueueCore, OPERATIONS as QUEUE_OPERATIONS
from .conversations import Conversations, OPERATIONS as CONVERSATION_OPERATIONS
from .process_core import ProcessCore, OPERATIONS as PROCESS_OPERATIONS

OPERATIONS = ('issue.create', 'issue.update', 'issue.comment', 'issue.link',
              'session.register', 'session.link', 'session.rebuild', 'events.append',
              'project.ensure', 'project.move_tree') + QUEUE_OPERATIONS + CONVERSATION_OPERATIONS + PROCESS_OPERATIONS


class ServiceCore:
    """Keep domain commands and durable observation storage independent of the caller's transport."""

    def __init__(self, store):
        """Compose existing task semantics with shared command identity and generic sessions."""
        self.store = store
        self.commands = CommandJournal(store.db)
        self.sessions = SessionEvents(store)
        from .session_retention import SessionRetention
        self.session_retention = SessionRetention(store)
        self.queue = QueueCore(store)
        self.conversations = Conversations(store)
        from .task_context import TaskContext
        self.task_context = TaskContext(store)
        from .project_spaces import ProjectSpaces
        from .project_migration import ProjectMigration
        self.projects = ProjectSpaces(store)
        self.project_migration = ProjectMigration(store)
        self.process = ProcessCore(store)

    def execute(self, envelope, actor='agent'):
        """Journal one caller intent; queue commands never launch an external runtime themselves."""
        fields(envelope, {'clientId', 'commandId', 'operation', 'payload'},
               {'clientId', 'commandId', 'operation', 'payload'})
        if envelope['operation'] not in OPERATIONS or type(envelope['payload']) is not dict:
            raise DomainError('unsupported_operation', 'Use an advertised service data operation.')
        if envelope['operation'] in PROCESS_OPERATIONS:
            self.process.sweep()
        return self.commands.execute(envelope['clientId'], envelope['commandId'], envelope['operation'],
                                     envelope['payload'], lambda operation, payload: self.apply(operation, payload, actor))

    def apply(self, operation, payload, actor='agent'):
        """Dispatch the fixed domain vocabulary without interpreting captured instructions or arbitrary URLs."""
        if operation in PROCESS_OPERATIONS:
            return self.process.apply(operation, payload, actor)
        if operation in QUEUE_OPERATIONS:
            return self.queue.apply(operation, payload, actor)
        if operation == 'conversation.bind':
            return self.conversations.bind(payload)
        if operation == 'conversation.reconnect':
            return self.conversations.reconnect(payload)
        if operation == 'project.ensure':
            return self.projects.ensure(payload)
        if operation == 'project.move_tree':
            return self.project_migration.move(payload, actor)
        if operation == 'session.register':
            return self.sessions.register(payload)
        if operation == 'session.link':
            return self.sessions.link(payload)
        if operation == 'session.rebuild':
            return self.sessions.rebuild(payload)
        if operation == 'events.append':
            return self.sessions.append(payload)
        if operation == 'issue.create':
            return self.store.create(payload, actor=actor)
        if operation == 'issue.update':
            fields(payload, {'issueId', 'changes'}, {'issueId', 'changes'})
            if type(payload['changes']) is not dict:
                raise DomainError('invalid_fields', 'Issue changes must be an object.')
            return self.store.update(payload['issueId'], payload['changes'], actor=actor)
        if operation == 'issue.comment':
            fields(payload, {'issueId', 'text'}, {'issueId', 'text'})
            return self.store.comment(payload['issueId'], payload['text'], actor=actor)
        fields(payload, {'issueId', 'relatedId', 'kind'}, {'issueId', 'relatedId', 'kind'})
        return self.store.link(payload['issueId'], payload['relatedId'], payload['kind'], actor=actor)

    def capabilities(self):
        """Advertise queue coordination separately from optional workers and automatic process scheduling."""
        return {'version': 'ag-tasks-service-data/v1', 'operations': list(OPERATIONS),
                'process': {'version': 'ag-tasks-process/v1', 'workerProtocol': 'ag-tasks-worker/v1',
                            'artifactExport': False, 'memoryAndLearning': False,
                            'memoryRecommendation': MEMORY_RECOMMENDATION_CONTRACT,
                            'transports': ['jsonl', 'http'], 'executionOwner': 'external_agent',
                            'startsProcesses': False, 'vendorDependency': None,
                            'maintenanceError': self.process.last_error},
                'projectSpaces': {'version': 1, 'explicitIdentity': 'projectKey',
                                  'agentProjectRootsRequireProject': True, 'childrenInheritSpace': True,
                                  'atomicTreeMove': True, 'preservesOldKeys': True,
                                  'crossSpaceDependencies': True, 'movePreservesWorkAndAcceptance': True},
                'sessionModes': ['observed'], 'managedExecution': False,
                'companion': {'version': 'ag-tasks-companion/v1', 'role': 'task_execution_registry',
                              'executionOwner': 'external_harness', 'startsProcesses': False,
                              'stableChatIdentity': ['harness', 'instanceId', 'remoteSessionId'],
                              'reconnect': 'revision_cas', 'bindingRetention': 'until_explicit_change',
                              'taskContext': {'minBytes': 8192, 'maxBytes': 32768,
                                              'freshReads': True, 'includesTranscripts': False}},
                'sessionHistory': {'sortOrders': ['newest', 'oldest'], 'defaultSort': 'newest',
                                   'sortField': 'updatedAt', 'cursor': 'activity_id_v1',
                                   'retentionDays': 3, 'sweepSeconds': 60,
                                   'protectActive': True, 'sourceChatsDeleted': False,
                                   'maintenanceError': self.session_retention.last_error},
                'mcpPresence': {'automaticRegistration': True, 'transport': 'stdio',
                                'leaseSeconds': 90, 'heartbeatSeconds': 25, 'durableHeartbeats': False},
                'codexSessions': {'readOnly': True, 'source': 'local_open_writers_and_rollout_lifecycle',
                                  'refreshSeconds': 10, 'activityFreshSeconds': 90, 'maxSessions': 100},
                'agentQueue': {'version': 'ag-tasks-agent-queue/v1', 'leaseSeconds': 90,
                               'automaticDispatch': False, 'expiredAttempt': 'quarantined',
                               'completionReleasesLane': False, 'steering': False,
                               'reconciliationActor': 'user'},
                'externalWorker': {'protocolVersions': [1], 'automaticLaunch': False,
                                   'qualifiedProviders': [], 'module': 'ag_tasks.acp_worker'},
                'idempotency': {'namespace': ['clientId', 'commandId'], 'fingerprint': 'json-v1',
                                'atomicEffectAndOutcome': True, 'automaticExpiry': False},
                'events': {'maxBatchBytes': 49152, 'maxEventBytes': 16384, 'maxBatchEvents': 100,
                           'maxPageSize': 50, 'producerSequenceStartsAt': 1},
                'projection': {'version': 1, 'acpObservationVersions': [1, 2],
                               'workspaceReleaseProof': False, 'rebuildSideEffects': False}}
