"""Deterministic external protocol fixture, not an LLM or native provider qualification."""

import hashlib
import json
from pathlib import Path
import sys


def reply(frame):
    """Actually read the supplied artifact so conformance covers the process/file boundary."""
    if frame['method'] == 'negotiate':
        return {'providerId': 'fixture-agent', 'providerVersion': '1', 'capabilities': {
            'freshContext': True, 'boundedWorkspace': True, 'readArtifact': True,
            'cancel': False, 'releaseEvidence': False, 'usage': False}}
    params = frame['params']
    artifact = params['artifacts'][0] if params['artifacts'] else None
    digest = hashlib.sha256((Path(params['workspace']['path']) / artifact['path']).read_bytes()).hexdigest() if artifact else None
    if params['role'] == 'producer':
        action, output = 'submit_result', {'summary': 'Artifact audited', 'changes': ['Read-only audit'],
            'validation': ['SHA256:' + digest], 'limitations': 'Deterministic fixture only', 'artifacts': []}
    elif params['role'] == 'critic':
        action, output = 'review_report', {'verdict': 'pass', 'findings': [], 'evidence': 'Independent fixture artifact SHA256:' + digest}
    else:
        raise ValueError('Unsupported task execution role')
    return {'action': action, 'output': output,
        'receipts': [{'kind': 'artifact_read', 'artifactDigest': digest, 'source': 'fixture-file-read'}] if artifact else [],
        'usage': {'inputTokens': None, 'outputTokens': None, 'cost': None},
        'release': {'state': 'unknown', 'evidence': 'Foreground fixture only'}}


if __name__ == '__main__':
    for line in sys.stdin:
        request = json.loads(line)
        print(json.dumps({'protocol': request['protocol'], 'requestId': request['requestId'], 'result': reply(request)}), flush=True)
