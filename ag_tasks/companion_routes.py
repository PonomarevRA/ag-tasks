"""HTTP read adapter for the passive task/chat registry; mutations use domain commands."""

from fastapi import Query


def register(app, store):
    """Expose fixed registry/context reads without accepting model or process commands."""

    @app.get('/api/v1/conversations/resolve')
    def resolve(harness: str, instanceId: str, remoteSessionId: str):
        """Recover an explicit external chat identity; MCP connection names are never guessed."""
        return store.service.conversations.resolve(harness, instanceId, remoteSessionId)

    @app.get('/api/v1/conversations')
    def conversations(limit: int = Query(20, ge=1, le=50), after: int = Query(0, ge=0), issueId: str | None = None):
        """Page all chats or one task's chat history, including continuation provenance."""
        return store.service.conversations.list(limit, after, issueId)

    @app.get('/api/v1/conversations/{conversation_id}')
    def conversation(conversation_id: str):
        """Read a persistent association, not an execution lease or activity claim."""
        return store.service.conversations.get(conversation_id)

    @app.get('/api/v1/conversations/{conversation_id}/context')
    def conversation_context(conversation_id: str, maxBytes: int = Query(16384, ge=8192, le=32768)):
        """Read the current bound task after reconnect or from another harness."""
        return store.service.task_context.get(conversation_id=conversation_id, max_bytes=maxBytes)

    @app.get('/api/v1/issues/{issue_id}/context')
    def task_context(issue_id: str, maxBytes: int = Query(16384, ge=8192, le=32768)):
        """Let any authorized local client continue a task without owning its previous chat."""
        return store.service.task_context.get(issue_id, max_bytes=maxBytes)
