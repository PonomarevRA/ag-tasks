"""Fixed passive process reads; mutations use the existing command journal."""

from fastapi import Query


def register(app, store):
    """Publish bounded references, manifests and run receipts without executing models."""
    @app.get('/api/v1/process/evidence')
    def evidence(issueId: str, limit: int = Query(20, ge=1, le=50)):
        """List one task's retained manifests; a truncated page is explicit."""
        with store.db.lock:
            return store.service.process.evidence.list(issueId, limit)

    @app.get('/api/v1/process/evidence/{identifier}')
    def manifest(identifier: str):
        """Read one immutable source snapshot, including coverage and omissions."""
        with store.db.lock:
            return store.service.process.evidence.get(identifier)

    @app.get('/api/v1/process/runs/{identifier}')
    def run(identifier: str):
        """Read delivery state; a read is neither heartbeat nor permission to retry."""
        with store.db.lock:
            return store.service.process.runs.get(identifier)
