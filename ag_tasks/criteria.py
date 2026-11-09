"""Bounded, explicit readiness/completion evidence and structured caller-reported results."""

import json

from .issues import DomainError, bounded_text, external_refs

ROLES = ('analyst', 'architect', 'developer', 'qa', 'critic', 'documentation', 'operator')


def criteria(value):
    """Validate at most fifty stable checklist items; a checked box must carry nonempty evidence."""
    if type(value) is not list or len(value) > 50:
        raise DomainError('invalid_criteria', 'At most fifty criteria are allowed.')
    result, identifiers = [], set()
    for item in value:
        if type(item) is not dict or set(item) != {'id', 'text', 'passed', 'evidence'} or type(item['passed']) is not bool:
            raise DomainError('invalid_criteria', 'Criteria require id, text, passed and evidence.')
        identifier = bounded_text(item['id'], 128, empty=False)
        if identifier in identifiers:
            raise DomainError('invalid_criteria', 'Criterion IDs must be unique.')
        identifiers.add(identifier)
        result.append({'id': identifier, 'text': bounded_text(item['text'], 1024, empty=False), 'passed': item['passed'],
                       'evidence': bounded_text(item['evidence'], 2048, empty=not item['passed'])})
    bounded_json(result, 'invalid_criteria')
    return result


def failures(items):
    """Return explicit missing/unchecked evidence, including an empty definition rather than vacuous success."""
    if not items:
        return [{'id': '', 'text': 'Definition is empty', 'reason': 'definition_required'}]
    return [{'id': item['id'], 'text': item['text'], 'reason': 'criterion_not_passed'}
            for item in items if not item['passed'] or not item['evidence'].strip()]


def strings(value, maximum, length):
    """Validate a bounded list of human-readable findings without coercing arbitrary objects."""
    if type(value) is not list or len(value) > maximum:
        raise DomainError('invalid_result', 'Invalid result collection.')
    return [bounded_text(item, length, empty=False) for item in value]


def result(value):
    """Keep immutable results structured and bounded; validation text is a declaration, not executed proof."""
    fields = {'summary', 'changes', 'validation', 'limitations', 'artifacts'}
    if type(value) is not dict or set(value) != fields:
        raise DomainError('invalid_result', 'Result requires summary, changes, validation, limitations and artifacts.')
    projected = {'summary': bounded_text(value['summary'], 4096, empty=False),
            'changes': strings(value['changes'], 50, 1024), 'validation': strings(value['validation'], 50, 2048),
            'limitations': bounded_text(value['limitations'], 4096), 'artifacts': external_refs(value['artifacts'], field='artifacts')}
    bounded_json(projected, 'invalid_result')
    return projected


def bounded_json(value, code):
    """Reserve room for details/history envelopes even for worst-case Unicode escaping."""
    if len(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('ascii')) > 48 * 1024:
        raise DomainError(code, 'Structured definition/result exceeds 48 KiB.')
