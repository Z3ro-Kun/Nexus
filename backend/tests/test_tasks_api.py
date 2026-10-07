from uuid import uuid4

import httpx


async def _run(api: httpx.AsyncClient) -> str:
    response = await api.post("/api/v1/runs", json={"goal": "Plan a trip"})
    assert response.status_code == 201
    return response.json()["id"]


def _task(task_id: str, *deps: str, **extra: object) -> dict[str, object]:
    return {"task_id": task_id, "title": f"Task {task_id}", "dependencies": list(deps), **extra}


async def test_create_list_and_get_tasks(api: httpx.AsyncClient) -> None:
    run_id = await _run(api)

    created = await api.post(
        f"/api/v1/runs/{run_id}/tasks",
        json={"tasks": [_task("t3", "t1", "t2"), _task("t1", task_type="search", agent_type="researcher"), _task("t2")]},
    )
    assert created.status_code == 201, created.text
    by_id = {t["task_id"]: t for t in created.json()}
    assert {t: v["status"] for t, v in by_id.items()} == {"t1": "ready", "t2": "ready", "t3": "pending"}
    assert (by_id["t1"]["task_type"], by_id["t1"]["agent_type"]) == ("search", "researcher")
    assert by_id["t3"]["dependencies"] == ["t1", "t2"]
    assert by_id["t3"]["run_id"] == run_id

    listed = (await api.get(f"/api/v1/runs/{run_id}/tasks")).json()
    assert [t["task_id"] for t in listed] == ["t1", "t2", "t3"]  # creation (dependency) order

    one = await api.get(f"/api/v1/runs/{run_id}/tasks/t3")
    assert one.status_code == 200
    assert one.json()["status"] == "pending"

    missing = await api.get(f"/api/v1/runs/{run_id}/tasks/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "task_not_found"


async def test_invalid_graphs_are_rejected_without_writing(api: httpx.AsyncClient) -> None:
    run_id = await _run(api)
    cases = {
        "dependency_cycle": [_task("t1", "t3"), _task("t2", "t1"), _task("t3", "t2")],
        "missing_dependency": [_task("t1", "nope")],
        "self_dependency": [_task("t1", "t1")],
        "duplicate_task": [_task("t1"), _task("t1")],
        "unknown_parent": [_task("t1", parent_id="nope")],
    }
    for code, tasks in cases.items():
        response = await api.post(f"/api/v1/runs/{run_id}/tasks", json={"tasks": tasks})
        assert response.status_code == 422, (code, response.text)
        assert response.json()["error"]["code"] == code

    duplicate_dep = await api.post(f"/api/v1/runs/{run_id}/tasks", json={"tasks": [_task("t1"), _task("t2", "t1", "t1")]})
    assert duplicate_dep.status_code == 422

    events = (await api.get(f"/api/v1/runs/{run_id}/events")).json()
    assert [e["event_type"] for e in events] == ["RunCreated"]


async def test_schedule_end_to_end(api: httpx.AsyncClient) -> None:
    run_id = await _run(api)
    base = f"/api/v1/runs/{run_id}"
    tasks = [_task("t1"), _task("t2", "t1"), _task("t3", "t1"), _task("t4", "t2", "t3"), _task("t5", "t2")]
    assert (await api.post(f"{base}/tasks", json={"tasks": tasks})).status_code == 201

    response = await api.post(
        f"{base}/schedule", json={"executor": "scripted", "outcomes": {"t2": "failure"}}
    )

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["task_statuses"] == {
        "t1": "completed", "t2": "failed", "t3": "completed", "t4": "blocked", "t5": "blocked",
    }
    assert sorted(report["started"]) == ["t1", "t2", "t3"]
    assert report["failed"] == ["t2"]

    state = (await api.get(f"{base}/state")).json()
    assert {t: v["status"] for t, v in state["tasks"].items()} == report["task_statuses"]
    assert state["tasks"]["t2"]["error"] == "scripted failure of task 't2'"

    events = [e["event_type"] for e in (await api.get(f"{base}/events")).json()]
    assert events.count("TaskStarted") == 3
    assert events.count("TaskCompleted") == 2
    assert events.count("TaskFailed") == 1

    rescheduled = (await api.post(f"{base}/schedule", json={"executor": "scripted"})).json()
    assert rescheduled["started"] == []


async def test_schedule_errors(api: httpx.AsyncClient) -> None:
    scripted = {"executor": "scripted"}
    missing = await api.post(f"/api/v1/runs/{uuid4()}/schedule", json=scripted)
    assert missing.status_code == 404

    run_id = await _run(api)
    # Phase 8: a run completes through verified work and the completion gate.
    await api.post(f"/api/v1/runs/{run_id}/tasks", json={"tasks": [_task("t1")]})
    await api.post(f"/api/v1/runs/{run_id}/verification", json={})
    done = (await api.post(f"/api/v1/runs/{run_id}/schedule", json=scripted)).json()
    assert done["run_status"] == "completed"
    inactive = await api.post(f"/api/v1/runs/{run_id}/schedule", json=scripted)
    assert inactive.status_code == 409
    assert inactive.json()["error"]["code"] == "run_not_active"


async def test_agent_scheduling_requires_a_configured_provider(api: httpx.AsyncClient) -> None:
    run_id = await _run(api)

    response = await api.post(f"/api/v1/runs/{run_id}/schedule")  # default executor: agent

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"
    bad = await api.post(f"/api/v1/runs/{run_id}/schedule", json={"outcomes": {"t1": "failure"}})
    assert bad.status_code == 422  # outcomes are only for the scripted executor
