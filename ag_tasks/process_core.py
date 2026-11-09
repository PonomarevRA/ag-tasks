"""Small domain coordinator; external adapters own every model invocation."""

from .issues import DomainError
from .process_schema import migrate
from .process_evidence import ProcessEvidence
from .process_runs import ProcessRuns


OPERATIONS = ('process.evidence.capture', 'process.run.reserve', 'process.run.heartbeat',
              'process.run.cancel', 'process.run.finish', 'process.run.observe', 'process.run.commit')


class ProcessCore:
    """Reuse the command journal and task ownership rather than start a second scheduler."""

    def __init__(self, store):
        """Compose additive storage and bounded evidence/run responsibilities."""
        migrate(store.db)
        self.store = store
        self.evidence = ProcessEvidence(store)
        self.runs = ProcessRuns(store, self.evidence)
        self.last_error = None

    def apply(self, operation, payload, actor):
        """Dispatch a fixed advertised domain operation, never arbitrary Python or task text."""
        if actor != 'agent':
            raise DomainError('worker_required', 'Use the agent process command surface.', 403)
        actions = {'process.evidence.capture': self.evidence.capture,
                   'process.run.reserve': self.runs.reserve, 'process.run.heartbeat': self.runs.heartbeat,
                   'process.run.cancel': self.runs.cancel, 'process.run.finish': self.runs.finish,
                   'process.run.observe': self.runs.observe, 'process.run.commit': self.runs.commit}
        action = actions.get(operation)
        if action is None:
            raise DomainError('unsupported_operation', 'Use an advertised process operation.')
        return action(payload)

    def sweep(self, *, restart=False):
        """Reconcile bounded liveness only; maintenance never runs models or exports project artifacts."""
        with self.store.db.transaction():
            runs = self.runs.sweep(restart=restart)
        return {'runs': runs}
