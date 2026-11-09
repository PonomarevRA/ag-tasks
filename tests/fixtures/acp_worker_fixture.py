"""Small real ACP process with deterministic transport faults and a prompt marker."""

import json
from pathlib import Path
import sys
import time


def write(message, split=False):
    """Emit complete or deliberately split NDJSON frames to exercise the worker's real reader."""
    data = json.dumps(message).encode() + b'\n'
    if split:
        sys.stdout.buffer.write(data[:7])
        sys.stdout.buffer.flush()
        time.sleep(0.02)
        sys.stdout.buffer.write(data[7:])
    else:
        sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main():
    """React only to fixed fixture methods; never execute text submitted by a queue item."""
    mode, marker = sys.argv[1], Path(sys.argv[2])
    prompt_id = None
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get('method')
        if method == 'initialize':
            write({'jsonrpc': '2.0', 'id': message['id'],
                   'result': {'protocolVersion': 2 if mode == 'v2' else 1, 'agentCapabilities': {}}}, split=True)
        elif method == 'session/new':
            write({'jsonrpc': '2.0', 'id': message['id'], 'result': {'sessionId': 'fixture-session'}})
        elif method == 'session/prompt':
            marker.write_text(json.dumps(message))
            prompt_id = message['id']
            if mode == 'eof':
                return
            if mode == 'malformed':
                sys.stdout.write('{malformed}\n')
                sys.stdout.flush()
                continue
            if mode == 'oversized':
                write({'jsonrpc': '2.0', 'method': 'fixture/large', 'params': {'text': 'x' * 20000}})
                continue
            if mode == 'permission':
                write({'jsonrpc': '2.0', 'id': 'permission', 'method': 'session/request_permission',
                       'params': {'sessionId': 'fixture-session', 'options': []}})
                continue
            if mode == 'cancel':
                continue
            if mode == 'wait':
                continue
            write({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'fixture-session', 'update': {'sessionUpdate': 'agent_message_chunk',
                'content': {'type': 'text', 'text': 'fixture answer'}}}, '_meta': {'token': 'fixture-secret'}})
            write({'jsonrpc': '2.0', 'id': message['id'], 'result': {'stopReason': 'end_turn'}})
        elif method == 'session/cancel':
            write({'jsonrpc': '2.0', 'id': prompt_id, 'result': {'stopReason': 'cancelled'}})


if __name__ == '__main__':
    main()
