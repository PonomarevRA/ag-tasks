"""Bounded JSON, durable fingerprints and explicit archival redaction policies."""

import hashlib
import json
import re

from .issues import DomainError, bounded_text

SECRET_KEYS = frozenset({'authorization', 'apikey', 'accesstoken', 'refreshtoken',
                         'password', 'secret', 'token', 'sessionkey', 'env'})


def identifier(value):
    """Accept only bounded opaque local keys; remote identifiers remain ordinary JSON values."""
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        raise DomainError('invalid_identity', 'Expected a bounded opaque identity.')
    return value


def encode(value, maximum=49152, *, maximum_nodes=8192):
    """Canonicalize business JSON without changing text/array order; reject non-JSON and oversized input."""
    pending = [(value, 0)]
    count = 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 24 or count > maximum_nodes:
            raise DomainError('invalid_payload', 'JSON nesting or node budget exceeded.')
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise DomainError('invalid_payload', 'JSON object keys must be text.')
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise DomainError('invalid_payload', 'Only JSON values are accepted.')
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(result.encode('utf-8')) > maximum:
            raise DomainError('payload_too_large', 'Payload exceeds the documented byte budget.', 413)
        return result
    except (ValueError, UnicodeError, OverflowError) as error:
        raise DomainError('invalid_payload', 'Expected finite JSON and valid Unicode.') from error


def fingerprint(value):
    """Version the canonical representation so upgrades cannot silently change key semantics."""
    return 'json-v1:' + hashlib.sha256(encode(value).encode()).hexdigest()


def redact(value, path='', paths=None):
    """Remove known credential fields in the archived copy; arbitrary secrets in prose are not detected."""
    paths = [] if paths is None else paths
    if type(value) is dict:
        result = {}
        for key, child in value.items():
            child_path = path + '/' + key.replace('~', '~0').replace('/', '~1')
            if key.lower().replace('_', '').replace('-', '') in SECRET_KEYS:
                result[key] = '[redacted]'
                paths.append(child_path)
            else:
                result[key] = redact(child, child_path, paths)[0]
        return result, paths
    if type(value) is list:
        return [redact(child, path + '/' + str(index), paths)[0] for index, child in enumerate(value)], paths
    return value, paths


def fields(value, allowed, required=()):
    """Reject misspelled command envelope fields while preserving arbitrary event payload fields."""
    if type(value) is not dict or set(value) - set(allowed) or set(required) - set(value):
        raise DomainError('invalid_fields', 'Unexpected or missing command fields.')
    return value


def page(limit, after):
    """Bound page work and reject booleans masquerading as numeric cursors."""
    if type(limit) is not int or not 1 <= limit <= 50 or type(after) is not int or after < 0:
        raise DomainError('invalid_page', 'Use limit 1..50 and a nonnegative integer cursor.')
    return limit, after
