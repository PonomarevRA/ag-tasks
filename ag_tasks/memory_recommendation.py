"""Optional, provider-neutral guidance; this module never discovers tools or saves memory."""

CONTRACT = 'ag-tasks-memory-recommendation/v1'
GUIDANCE = (
    'After completing the work and its current review, consider whether there is a useful reusable outcome. '
    'Inspect the memory-saving tools actually available in your own agent session and their documented capabilities. '
    'Choose a suitable tool yourself within the current permissions and project scope; no provider is preferred. '
    'If appropriate, save only a compact supported outcome with task/artifact references, actual validation and limitations. '
    'Keep reported results, independent review and uncertainty distinct. Do not copy credentials, private reasoning or raw transcripts. '
    'If no suitable tool is available, skip this optional step. Do not install a memory service, create fallback files '
    'or use a Tasks database for memory. Availability is not a successful write; claim saved only after the tool confirms it. '
    'An unavailable or failed memory write does not block task completion or replace the required task result and critic review.'
)


def memory_recommendation():
    """Return detached advisory data without inspecting agent tools, writing storage or recording a save status."""
    return {'contract': CONTRACT, 'mode': 'advisory', 'required': False,
            'selectionOwner': 'agent', 'toolSource': 'current_agent_session',
            'whenUnavailable': 'skip', 'fallback': None, 'blocksCompletion': False,
            'instructions': GUIDANCE}
