"""Read-only aggregation of committed work sessions for the cross-space dashboard."""

from datetime import UTC, date, datetime, time, timedelta
from collections import Counter

from .issues import DomainError

DAY_MS = 86_400_000
DAY = timedelta(days=1)


def _window(start, end):
    """Validate an inclusive UTC calendar range and produce an exclusive end."""
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end or not 0 <= (last - first).days < 366:
            raise ValueError
        return datetime.combine(first, time.min, UTC), datetime.combine(last + DAY, time.min, UTC)
    except (ValueError, OverflowError) as error:
        raise DomainError("invalid_period", "Укажите период от 1 до 366 дней.", 422) from error


def _sessions(db, start, end, space_id, kind, query):
    """Read one consistent metadata projection without restoring cold issue bodies."""
    sql = """
        SELECT w.started_at, w.elapsed_ms, i.id, CASE WHEN i.deleted_at IS NULL THEN i.title ELSE '' END AS title,
               i.kind, i.space_id,
               s.name AS project, s.prefix || '-' || i.space_number AS key, i.deleted_at
          FROM work_sessions w JOIN issues i ON i.id = w.issue_id
          JOIN spaces s ON s.id = i.space_id
         WHERE w.elapsed_ms > 0 AND julianday(w.started_at) <= julianday(?)
           AND julianday(w.started_at) + w.elapsed_ms / 86400000.0 >= julianday(?)
           AND (? = '' OR i.space_id = ?) AND (? = '' OR i.kind = ?)
    """
    # SQLite timestamps round to milliseconds; widen the prefilter and clip precisely in Python.
    prefilter_start = start if start == datetime.min.replace(tzinfo=UTC) else start - timedelta(milliseconds=1)
    with db.lock:
        rows = db.connection.execute(sql, (end.isoformat(), prefilter_start.isoformat(), space_id, space_id, kind, kind)).fetchall()
        projects = db.connection.execute("SELECT id, name FROM spaces ORDER BY name, id").fetchall()
        kinds = db.connection.execute("SELECT DISTINCT kind FROM issues ORDER BY kind").fetchall()
    needle = query.strip().casefold()
    return (
        (row for row in rows if needle in row["key"].casefold() or needle in row["title"].casefold()),
        [dict(row) for row in projects],
        [row["kind"] for row in kinds],
    )


def _day_slices(started_at, elapsed_ms, start, end):
    """Assign committed monotonic time from session start across UTC day boundaries."""
    began = datetime.fromisoformat(started_at).astimezone(UTC)
    cursor = max(start, began)
    stop = min(end, began + timedelta(milliseconds=elapsed_ms))
    while cursor < stop:
        day = cursor.date()
        next_day = min(stop, datetime.combine(day + DAY, time.min, UTC))
        # Millisecond offsets from the same origin telescope exactly across midnight.
        lower = (cursor - began) // timedelta(milliseconds=1)
        upper = (next_day - began) // timedelta(milliseconds=1)
        yield day.isoformat(), upper - lower
        cursor = next_day


def _task_percentile(ranked, percent):
    """Interpolate task milliseconds in descending rank, rounding to the nearest ms."""
    if not ranked:
        return None
    index, weight = divmod((len(ranked) - 1) * (100 - percent), 100)
    higher = ranked[index]["elapsedMs"]
    if not weight:
        return higher
    lower = ranked[index + 1]["elapsedMs"]
    return (higher * (100 - weight) + lower * weight + 50) // 100


def _duration_summary(task_times):
    """Summarize milliseconds per task; empty percentiles/mean remain missing."""
    ranked = [{"elapsedMs": amount} for amount in sorted(task_times.values(), reverse=True)]
    count = len(ranked)
    total = sum(task_times.values())
    return {
        "elapsedMs": total, "taskCount": count,
        "p50Ms": _task_percentile(ranked, 50),
        "p90Ms": _task_percentile(ranked, 90),
        "meanMs": (total + count // 2) // count if count else None,
    }


def _record_duration(groups, key, issue_id, amount):
    """Accumulate a task once per group before any percentile is calculated."""
    task_times = groups.setdefault(key, {})
    task_times[issue_id] = task_times.get(issue_id, 0) + amount


def work_analytics(db, start_date, end_date, space_id="", kind="", query="", offset=0):
    """Return exact committed totals, UTC series, ranked groups and one page of issue details."""
    start, end = _window(start_date, end_date)
    rows, projects, kinds = _sessions(db, start, end, space_id, kind, query)
    days = {(start.date() + timedelta(days=index)).isoformat(): 0 for index in range((end - start).days)}
    project_ms, kind_ms = Counter(), Counter()
    tasks = {}
    day_tasks, week_tasks, project_tasks, kind_tasks = {}, {}, {}, {}
    for day in days:
        week = date.fromisoformat(day)
        week -= timedelta(days=week.weekday())
        week_tasks[week.isoformat()] = {}
    count = 0
    for row in rows:
        elapsed = 0
        for day, amount in _day_slices(row["started_at"], row["elapsed_ms"], start, end):
            days[day] += amount
            elapsed += amount
            if amount:
                week = date.fromisoformat(day)
                week -= timedelta(days=week.weekday())
                _record_duration(day_tasks, day, row["id"], amount)
                _record_duration(week_tasks, week.isoformat(), row["id"], amount)
        if not elapsed:
            continue
        count += 1
        _record_duration(project_tasks, row["space_id"], row["id"], elapsed)
        _record_duration(kind_tasks, row["kind"], row["id"], elapsed)
        project_ms[row["space_id"]] += elapsed
        kind_ms[row["kind"]] += elapsed
        issue = tasks.setdefault(row["id"], {
            "id": row["id"], "key": row["key"], "title": row["title"], "kind": row["kind"],
            "project": row["project"], "deleted": bool(row["deleted_at"]), "elapsedMs": 0, "sessionCount": 0,
        })
        issue["elapsedMs"] += elapsed
        issue["sessionCount"] += 1

    names = {project["id"]: project["name"] for project in projects}
    ranked = sorted(tasks.values(), key=lambda issue: (-issue["elapsedMs"], issue["key"]))
    page_size = 50
    return {
        "totalMs": sum(days.values()), "sessionCount": count, "taskCount": len(tasks),
        "meanTaskMs": (sum(days.values()) + len(tasks) // 2) // len(tasks) if tasks else None,
        "taskDurationPercentiles": {"p50Ms": _task_percentile(ranked, 50), "p90Ms": _task_percentile(ranked, 90)},
        "projectCount": len(project_ms), "projects": projects, "kinds": kinds,
        "byDay": [{"label": day, **_duration_summary(day_tasks.get(day, {}))} for day in days],
        "byWeek": [{"label": week, **_duration_summary(times)} for week, times in sorted(week_tasks.items())],
        "byProject": [{"id": key, "label": names[key], **_duration_summary(project_tasks[key])} for key, amount in project_ms.most_common()],
        "byKind": [{"id": key, **_duration_summary(kind_tasks[key])} for key, amount in kind_ms.most_common()],
        "tasks": ranked[offset:offset + page_size],
        "nextOffset": offset + page_size if offset + page_size < len(ranked) else None,
    }
