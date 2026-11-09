"""Agent-neutral worker/v1 frames, validated identically by every transport."""

import math
import re
from pathlib import PurePosixPath

from .criteria import result as validate_result, strings
from .issues import DomainError, bounded_text
from .service_values import encode, fields, identifier

PROTOCOL = 'ag-tasks-worker/v1'
CAPABILITIES = {'freshContext', 'readArtifact', 'boundedWorkspace', 'cancel', 'releaseEvidence', 'usage'}
ACTIONS = {'producer': 'submit_result', 'critic': 'review_report'}


def validate_usage(value):
    """Require explicit unknowns and finite nonnegative counters; missing cost is never zero."""
    fields(value, {'inputTokens', 'outputTokens', 'cost'}, {'inputTokens', 'outputTokens', 'cost'})
    for key, amount in value.items():
        if amount is None:
            continue
        valid = type(amount) is int if key != 'cost' else type(amount) in (int, float)
        if not valid or not math.isfinite(amount) or amount < 0:
            raise DomainError('invalid_worker_usage', 'Usage must be nonnegative counters or null.')


def request(method, request_id, params):
    """Construct a bounded frame from host-owned values, never a model-selected tool URL."""
    identifier(request_id)
    if method not in ('negotiate', 'run', 'cancel') or type(params) is not dict:
        raise DomainError('invalid_worker_request', 'Unknown worker method.')
    if method == 'negotiate':
        fields(params, set())
    elif method == 'cancel':
        fields(params, {'runId'}, {'runId'})
        identifier(params['runId'])
    else:
        validate_run_params(params)
    frame = {'protocol': PROTOCOL, 'requestId': request_id, 'method': method, 'params': params}
    encode(frame, 24576)
    return frame


def validate_run_params(params):
    """Validate the public neutral wire input before either transport can start an adapter."""
    required = {'runId', 'role', 'scope', 'workspace', 'artifacts', 'input', 'budget'}
    fields(params, required, required)
    identifier(params['runId'])
    if type(params['role']) is not str or params['role'] not in ACTIONS:
        raise DomainError('invalid_worker_role', 'Unknown role.')
    fields(params['scope'], {'issueId', 'spaceId', 'definitionRevision', 'resultGeneration'},
           {'issueId', 'spaceId', 'definitionRevision', 'resultGeneration'})
    for key in ('issueId', 'spaceId'):
        identifier(params['scope'][key])
    for key in ('definitionRevision', 'resultGeneration'):
        if type(params['scope'][key]) is not int or params['scope'][key] < 0:
            raise DomainError('invalid_worker_scope', 'Version pins must be nonnegative integers.')
    fields(params['workspace'], {'id', 'path'}, {'id', 'path'})
    identifier(params['workspace']['id'])
    bounded_text(params['workspace']['path'], 2048, empty=False)
    fields(params['budget'], {'timeoutSeconds', 'maxOutputBytes'}, {'timeoutSeconds', 'maxOutputBytes'})
    for name, maximum in (('timeoutSeconds', 120), ('maxOutputBytes', 16384)):
        if type(params['budget'][name]) is not int or not 1 <= params['budget'][name] <= maximum:
            raise DomainError('invalid_worker_budget', 'Worker budget is outside the protocol limits.')
    if type(params['artifacts']) is not list or len(params['artifacts']) > 20:
        raise DomainError('invalid_worker_artifacts', 'At most20artifact descriptors are allowed.')
    for artifact in params['artifacts']:
        fields(artifact, {'path', 'sha256', 'bytes'}, {'path', 'sha256', 'bytes'})
        path = bounded_text(artifact['path'], 1024, empty=False)
        if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path:
            raise DomainError('invalid_worker_artifacts', 'Artifact paths must stay relative to the workspace.')
        if type(artifact['sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', artifact['sha256']):
            raise DomainError('invalid_worker_artifacts', 'Artifact digest must be SHA256.')
        if type(artifact['bytes']) is not int or artifact['bytes'] < 0:
            raise DomainError('invalid_worker_artifacts', 'Artifact size must be nonnegative.')
    if type(params['input']) is not dict:
        raise DomainError('invalid_worker_input', 'Role input must be an object.')
    encode(params, 24000)


def response(value, frame):
    """Accept only a matching response; malformed or error replies never authorize retries."""
    encode(value, 16384)
    fields(value, {'protocol', 'requestId', 'result', 'error'}, {'protocol', 'requestId'})
    if value['protocol'] != PROTOCOL or value['requestId'] != frame['requestId']:
        raise DomainError('worker_protocol_mismatch', 'Worker version or request identity differs.', 409)
    if ('result' in value) == ('error' in value):
        raise DomainError('invalid_worker_response', 'Exactly one result or error is required.')
    if 'error' in value:
        fields(value['error'], {'code', 'message', 'retryable'}, {'code', 'message', 'retryable'})
        identifier(value['error']['code'])
        bounded_text(value['error']['message'], 512)
        if value['error']['retryable'] is not False:
            raise DomainError('invalid_worker_response', 'Worker errors cannot request automatic retry.')
        raise DomainError('worker_failed', 'External worker returned ' + value['error']['code'] + '.', 503)
    return value['result']


def capabilities(value, role):
    """Require explicit fresh context and workspace/artifact support before delivery."""
    fields(value, {'providerId', 'providerVersion', 'capabilities'}, {'providerId', 'providerVersion', 'capabilities'})
    identifier(value['providerId'])
    bounded_text(value['providerVersion'], 128, empty=False)
    caps = value['capabilities']
    fields(caps, CAPABILITIES, CAPABILITIES)
    if any(type(item) is not bool for item in caps.values()) or type(role) is not str or role not in ACTIONS:
        raise DomainError('invalid_worker_capabilities', 'Explicit boolean capabilities and role are required.')
    required = {'freshContext', 'boundedWorkspace'}
    required.add('readArtifact')
    if not all(caps[key] for key in required):
        raise DomainError('worker_capability_missing', 'Worker cannot meet this role contract.', 409)
    return value


def role_result(value, role):
    """Validate the only action allowed to the current role; all receipts remain declared evidence."""
    fields(value, {'action', 'output', 'receipts', 'usage', 'release'},
           {'action', 'output', 'receipts', 'usage', 'release'})
    if value['action'] != ACTIONS.get(role):
        raise DomainError('worker_action_forbidden', 'Role cannot perform this action.', 403)
    output = value['output']
    if role == 'producer':
        validate_result(output)
        strings(output['changes'], 20, 1024)
        strings(output['validation'], 20, 2048)
    elif role == 'critic':
        fields(output, {'verdict', 'findings', 'evidence'}, {'verdict', 'findings', 'evidence'})
        strings(output['findings'], 20, 2048)
        bounded_text(output['evidence'], 4096, empty=False)
        if output['verdict'] not in ('pass', 'rework') or output['verdict'] == 'rework' and not output['findings']:
            raise DomainError('invalid_worker_review', 'Rework needs concrete findings.')
    else:
        raise DomainError('invalid_worker_role', 'Only producer and critic roles belong to Tasks.')
    validate_usage(value['usage'])
    fields(value['release'], {'state', 'evidence'}, {'state', 'evidence'})
    if value['release']['state'] not in ('proved', 'unknown'):
        raise DomainError('invalid_worker_release', 'Release is proved or unknown.')
    bounded_text(value['release']['evidence'], 512)
    receipts = value['receipts']
    if type(receipts) is not list or len(receipts) > 20:
        raise DomainError('invalid_worker_receipts', 'At most20receipts are allowed.')
    for receipt in receipts:
        fields(receipt, {'kind', 'artifactDigest', 'source'}, {'kind', 'artifactDigest', 'source'})
        if (receipt['kind'] not in ('artifact_read', 'tool_result') or type(receipt['artifactDigest']) is not str or
                not re.fullmatch('[a-f0-9]{64}', receipt['artifactDigest'])):
            raise DomainError('invalid_worker_receipts', 'Receipt needs a kind and SHA256 digest.')
        bounded_text(receipt['source'], 128, empty=False)
    encode(value, 16384)
    return value
