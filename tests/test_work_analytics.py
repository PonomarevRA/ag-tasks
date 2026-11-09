"""Cross-space work totals and UTC bucket boundaries through the public read route."""

from fastapi.testclient import TestClient

from ag_tasks.app import create_app


def _session(store, issue, started, elapsed, state="paused", *, session_key="fixture"):
    """Seed exact committed durations without waiting for wall-clock leases."""
    with store.db.lock, store.db.connection:
        store.db.connection.execute(
            """INSERT INTO work_sessions(id,issue_id,agent_id,session_key,started_at,
               last_heartbeat_at,lease_expires_at,elapsed_ms,state)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (f'{issue["id"]}-{session_key}', issue["id"], "agent", session_key, started, started, started, elapsed, state),
        )


def test_work_analytics_cross_space_totals_and_filters(tmp_path):
    """Combine overlapping sessions and clip work at the selected UTC midnights."""
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        store = app.state.store
        project = store.spaces.write({"name":"Другой проект", "prefix":"OTHER"})
        first = store.create({"title":"Анализ границ", "kind":"rnd"})
        second = store.create({"title":"Реализация", "spaceId":project["id"]})
        store.create({"title":"Задача без журнала"})
        _session(store, first, "2026-10-31T23:59:59.750000+00:00", 1_000)
        _session(store, second, "2026-11-01T00:00:00+00:00", 2_000, "running")
        path = "/api/v1/analytics/work"
        period = {"start":"2026-11-01", "end":"2026-11-01"}
        response = client.get(path, params=period)
        assert response.status_code == 200
        report = response.json()
        assert (report["totalMs"], report["taskCount"], report["projectCount"], report["sessionCount"]) == (2_750, 2, 2, 2)
        assert report["taskDurationPercentiles"] == {"p50Ms": 1375, "p90Ms": 1875}
        assert report["byDay"] == [{
            "label":"2026-11-01", "elapsedMs":2_750, "taskCount":2,
            "p50Ms":1375, "p90Ms":1875, "meanMs":1375,
        }]
        assert report["byWeek"] == [{**report["byDay"][0], "label":"2026-10-26"}]
        assert report["meanTaskMs"] == 1375
        assert all(row["taskCount"] == 1 and row["p50Ms"] == row["elapsedMs"] for row in report["byProject"])
        for group in ("byProject", "byKind", "tasks"):
            assert sum(row["elapsedMs"] for row in report[group]) == 2_750
        assert [row["id"] for row in report["tasks"]] == [second["id"], first["id"]]
        assert client.get(path, params={**period, "spaceId":project["id"]}).json()["totalMs"] == 2_000
        filtered = client.get(path, params={**period, "kind":"rnd", "q":"АНАЛИЗ"}).json()
        assert filtered["totalMs"] == 750
        assert filtered["taskDurationPercentiles"] == {"p50Ms": 750, "p90Ms": 750}
        assert client.get(path, params={**period, "q":second["key"]}).json()["totalMs"] == 2_000
        empty = client.get(path, params={**period, "q":"absent"}).json()
        assert empty["totalMs"] == 0
        assert empty["taskDurationPercentiles"] == {"p50Ms": None, "p90Ms": None}
        assert client.get(path, params={"start":"2026-10-31", "end":"2026-10-31"}).json()["totalMs"] == 250
        assert client.get(path, params={**period, "end":"2026-10-30"}).status_code == 422


def test_work_percentiles_use_task_totals_before_pagination(tmp_path):
    """Use each task once, including tasks absent from the current page."""
    app = create_app(tmp_path)
    started = "2026-11-01T12:00:00+00:00"
    with TestClient(app, base_url="http://localhost") as client:
        store = app.state.store
        for seconds in range(1, 53):
            task = store.create({"title": f"Task {seconds}"})
            _session(store, task, started, seconds * 500)
            _session(store, task, started, seconds * 500, session_key="second")

        path = "/api/v1/analytics/work"
        period = {"start": "2026-11-01", "end": "2026-11-01"}
        first = client.get(path, params=period).json()
        second = client.get(path, params={**period, "offset": first["nextOffset"]}).json()
        assert (first["taskCount"], first["sessionCount"]) == (52, 104)
        assert (len(first["tasks"]), len(second["tasks"])) == (50, 2)
        assert first["byDay"][0]["taskCount"] == 52
        assert first["byDay"][0]["p50Ms"] == 26_500
        assert first["byDay"][0]["p90Ms"] == 46_900
        assert first["byKind"] == second["byKind"]
        assert first["taskDurationPercentiles"] == second["taskDurationPercentiles"] == {
            "p50Ms": 26_500, "p90Ms": 46_900,
        }


def test_bucket_percentiles_sum_sessions_per_task_and_do_not_average_days(tmp_path):
    """Weekly percentiles aggregate task time again instead of averaging daily quantiles."""
    app = create_app(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        store = app.state.store
        first = store.create({"title": "Repeated task"})
        second = store.create({"title": "Second task"})
        _session(store, first, "2026-11-02T12:00:00+00:00", 10_000)
        _session(store, first, "2026-11-03T12:00:00+00:00", 30_000, session_key="second")
        _session(store, second, "2026-11-03T12:00:00+00:00", 20_000)
        report = client.get("/api/v1/analytics/work", params={"start":"2026-11-02","end":"2026-11-04"}).json()
        assert report["byDay"][2]["p50Ms"] is None
        assert report["byDay"][2]["elapsedMs"] == 0
        assert report["byDay"][2]["taskCount"] == 0
        assert report["byWeek"][0] == {
            "label":"2026-11-02", "elapsedMs":60_000, "taskCount":2,
            "p50Ms":30_000, "p90Ms":38_000, "meanMs":30_000,
        }
