"""Explicit pause metadata; a blocker is different from a rejected implementation."""

from .issues import DomainError, bounded_text


def pause_details(value, credentials):
    """Keep old stop payloads valid; blocked work requires a useful recovery action."""
    if type(value) is not dict or set(value) - {'agentId', 'sessionId', 'sessionKey', 'reason', 'nextAction'}:
        raise DomainError('invalid_work_session', 'Invalid work pause fields.')
    supplied = credentials({key: value[key] for key in ('agentId', 'sessionId', 'sessionKey') if key in value})
    reason = value.get('reason', 'paused')
    if reason not in ('paused', 'blocked'):
        raise DomainError('invalid_pause', 'Choose paused or blocked.')
    action = bounded_text(value.get('nextAction', ''), 2048, empty=reason != 'blocked')
    return supplied, reason, action
