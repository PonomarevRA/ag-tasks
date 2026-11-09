"""Bounded read-only HTTP adapter for operational task control."""

import asyncio
from fastapi import Query
from .manager_analytics import management_analytics


def register(app, store):
    """
    Register the manager snapshot without adding issue mutations or workflow authority.

    Args:
        app: The owning FastAPI application, including its existing HTTP boundary.
        store: The IssueStore supplying the locked database.
    """
    @app.get('/api/v1/analytics/management')
    async def management(spaceId: str = Query('', max_length=64), kind: str = Query('task', max_length=32),
                         status: str = Query('', max_length=32), priority: str = Query('', max_length=32),
                         assignee: str = Query('', max_length=128), q: str = Query('', max_length=200),
                         queue: str = Query('open', max_length=32), offset: int = Query(0, ge=0),
                         limit: int = Query(50, ge=1, le=100)):
        """
        Read current metadata; the queue narrows only the table, not scope KPI/charts.

        Args:
            spaceId: Exact project ID, or empty for all.
            kind: task, rnd, review, or empty for all card kinds.
            status: Exact status or empty.
            priority: Exact priority or empty.
            assignee: Exact trimmed owner label or empty.
            q: Literal key/title substring, case-insensitive.
            queue: open, attention, unassigned, overdue, review, or all.
            offset: Nonnegative table offset in deterministic risk order.
            limit: Table page size, one through one hundred.
        Returns:
            Whole-filter-scope KPI/facets/charts plus one bounded table page.
        """
        return await asyncio.to_thread(management_analytics, store.db, space_id=spaceId, kind=kind,
                                       status=status, priority=priority, assignee=assignee, query=q,
                                       queue=queue, offset=offset, limit=limit)
