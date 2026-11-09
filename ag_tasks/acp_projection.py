"""Pure ACP observation codec; it neither schedules work nor declares a workspace safe."""

import json

from .issues import DomainError


def validate(message):
    """Reject malformed JSON-RPC envelopes without dropping unknown extension fields or methods."""
    if message.get('jsonrpc') != '2.0' or ('method' not in message and 'id' not in message):
        raise DomainError('invalid_event', 'ACP observations require a JSON-RPC 2.0 envelope.')
    if 'method' in message and type(message['method']) is not str:
        raise DomainError('invalid_event', 'JSON-RPC method must be text.')
    if 'method' in message and ('result' in message or 'error' in message):
        raise DomainError('invalid_event', 'An RPC request cannot also be a response.')
    if 'method' not in message and (('result' in message) == ('error' in message)):
        raise DomainError('invalid_event', 'An RPC response needs exactly one result or error.')
    if 'id' in message and message['id'] is not None and type(message['id']) not in (str, int):
        raise DomainError('invalid_event', 'JSON-RPC identity must be text, integer or null.')


def project(connection, session, event, state):
    """Apply recognized live ACP facts; replay may fill entities but cannot change current lifecycle."""
    if session['protocol'] != 'acp' or session['protocol_version'] not in (1, 2):
        return state
    message = event['payload']
    if not isinstance(message, dict):
        return state
    live = event['origin'] == 'live'
    method = message.get('method')
    params = message.get('params')
    if method == 'session/update' and isinstance(params, dict):
        # Never attribute an embedded foreign session's activity to this root binding.
        if params.get('sessionId') != session['remote_id']:
            return state
        update = params.get('update')
        if isinstance(update, dict):
            entity(connection, session['id'], event, update)
            if live and session['protocol_version'] == 2 and update.get('sessionUpdate') == 'state_update':
                if update.get('state') in ('running', 'requires_action', 'idle'):
                    state['foregroundState'] = update['state']
                    state['outcome'] = update.get('stopReason') if update['state'] == 'idle' else None
    if not live:
        return state
    rpc_id = message.get('id')
    if 'id' not in message:
        return state
    rpc_key = json.dumps(rpc_id, ensure_ascii=False)
    if method == 'session/prompt' and isinstance(params, dict) and params.get('sessionId') == session['remote_id']:
        connection.execute('INSERT OR REPLACE INTO service_requests VALUES(?,?,?,?)',
                           (session['id'], event['direction'], rpc_key, method))
        state['submissionState'] = 'submitted'
        state['outcome'] = None
        state.pop('messageId', None)
        return state
    if method is not None:
        return state
    opposite = 'agent_to_client' if event['direction'] == 'client_to_agent' else 'client_to_agent'
    request = connection.execute('SELECT method FROM service_requests WHERE session_id=? AND direction=? AND rpc_id=?',
                                 (session['id'], opposite, rpc_key)).fetchone()
    if request is None:
        return state
    connection.execute('DELETE FROM service_requests WHERE session_id=? AND direction=? AND rpc_id=?',
                       (session['id'], opposite, rpc_key))
    if 'error' in message:
        state.update(submissionState='rejected' if session['protocol_version'] == 2 else 'answered', outcome='rpc_error')
    elif isinstance(message.get('result'), dict):
        result = message['result']
        if session['protocol_version'] == 2 and isinstance(result.get('messageId'), str):
            state.update(submissionState='accepted', messageId=result['messageId'])
        elif session['protocol_version'] == 1 and isinstance(result.get('stopReason'), str):
            state.update(submissionState='answered', outcome=result['stopReason'])
    # No ACP response, including cancelled, proves owned background work stopped.
    return state


def entity(connection, session_id, event, update):
    """Merge scoped entity patches; history cannot roll a live entity back to an earlier state."""
    variant = update.get('sessionUpdate', '')
    kinds = {'tool_call': ('tool', 'toolCallId'), 'tool_call_update': ('tool', 'toolCallId'),
             'agent_message': ('agent_message', 'messageId'), 'user_message': ('user_message', 'messageId'),
             'plan_update': ('plan', 'planId'), 'terminal_update': ('terminal', 'terminalId')}
    if variant not in kinds:
        return
    kind, field = kinds[variant]
    remote_id = update.get(field)
    if type(remote_id) is not str or len(remote_id) > 512:
        return
    old = connection.execute('SELECT * FROM service_entities WHERE session_id=? AND kind=? AND remote_id=?',
                             (session_id, kind, remote_id)).fetchone()
    live = event['origin'] == 'live'
    if old and old['live'] and not live:
        return
    payload = json.loads(old['payload']) if old else {}
    payload.update(update)
    # Raw patches stay in the journal; a bounded projection cannot grow without limit.
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > 32768:
        payload = {field: remote_id, 'sessionUpdate': variant, 'projectionOmitted': True}
    connection.execute('INSERT OR REPLACE INTO service_entities VALUES(?,?,?,?,?,?)',
                       (session_id, kind, remote_id, json.dumps(payload, ensure_ascii=False), event['cursor'], int(live)))
