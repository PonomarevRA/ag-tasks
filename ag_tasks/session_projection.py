"""Shared observation state and codec dispatch; storage has no protocol-specific lifecycle rules."""


def initial():
    """An empty observed source proves neither idleness nor safe workspace ownership."""
    return {'version': 1, 'submissionState': 'unknown', 'foregroundState': 'unknown',
            'outcome': None, 'ownedActivityState': 'unknown', 'continuity': 'source_unverified'}


def project(connection, session, event, state):
    """Delegate recognized source formats; unsupported sources remain archived without guessed semantics."""
    if session['protocol'] == 'acp':
        from .acp_projection import project as project_acp
        return project_acp(connection, session, event, state)
    return state


def validate_payload(protocol, payload):
    """Validate only a recognized wire envelope; unknown event bodies remain generic JSON objects."""
    if protocol == 'acp':
        from .acp_projection import validate
        validate(payload)
