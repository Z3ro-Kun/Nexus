from uuid import uuid4

import httpx

from app.persistence.database import Database


async def _create(api: httpx.AsyncClient, goal: str = "Plan a trip") -> dict:  # type: ignore[type-arg]
    response = await api.post("/api/v1/runs", json={"goal": goal})
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_run(api: httpx.AsyncClient) -> None:
    run = await _create(api)

    assert run["goal"] == "Plan a trip"
    assert run["status"] == "created"
    assert run["last_sequence"] == 1


async def test_get_run(api: httpx.AsyncClient) -> None:
    run = await _create(api)

    response = await api.get(f"/api/v1/runs/{run['id']}")

    assert response.status_code == 200
    assert response.json() == run


async def test_get_events(api: httpx.AsyncClient) -> None:
    run = await _create(api)

    response = await api.get(f"/api/v1/runs/{run['id']}/events")

    assert response.status_code == 200
    [event] = response.json()
    assert event["sequence"] == 1
    assert event["event_type"] == "RunCreated"
    assert event["payload"] == {"goal": "Plan a trip", "constraints": []}
    assert event["run_id"] == run["id"]


async def test_get_state(api: httpx.AsyncClient) -> None:
    run = await _create(api)

    response = await api.get(f"/api/v1/runs/{run['id']}/state")

    assert response.status_code == 200
    state = response.json()
    assert (state["run_id"], state["goal"], state["status"]) == (run["id"], "Plan a trip", "created")
    assert state["last_sequence"] == 1


async def test_event_lifecycle_end_to_end(api: httpx.AsyncClient) -> None:
    run = await _create(api)
    base = f"/api/v1/runs/{run['id']}"

    appended = await api.post(
        f"{base}/events",
        json={
            "expected_sequence": 1,
            "events": [
                {"event_type": "TaskCreated", "payload": {"task_id": "t1", "title": "Find flights"}},
                {
                    "event_type": "FactAdded",
                    "payload": {"fact_id": "f1", "content": "Flight at 9:00",
                                "provenance": {"kind": "user_provided", "source": "run goal and constraints"}},
                    "agent_id": "researcher",
                    "task_id": "t1",
                },
            ],
        },
    )
    assert appended.status_code == 201, appended.text
    assert [e["sequence"] for e in appended.json()] == [2, 3]

    # Phase 8: RunCompleted is privileged; the whole batch is refused, nothing is written.
    forged = await api.post(
        f"{base}/events",
        json={"events": [
            {"event_type": "TaskStarted", "payload": {"task_id": "t1"}},
            {"event_type": "TaskCompleted", "payload": {"task_id": "t1"}},
            {"event_type": "RunCompleted", "payload": {"summary": "done"}},
        ]},
    )
    assert forged.status_code == 403 and forged.json()["error"]["code"] == "privileged_event"

    done = await api.post(
        f"{base}/events",
        json={"events": [
            {"event_type": "TaskStarted", "payload": {"task_id": "t1"}},
            {"event_type": "TaskCompleted", "payload": {"task_id": "t1"}},
        ]},
    )
    assert done.status_code == 201, done.text
    # The run completes through verification and the completion gate.
    assert (await api.post(f"{base}/verification", json={"task_id": "verify"})).status_code == 201
    scheduled = (await api.post(f"{base}/schedule", json={"executor": "scripted"})).json()
    assert (scheduled["run_status"], scheduled["completion_blockers"]) == ("completed", [])

    events = (await api.get(f"{base}/events")).json()
    assert [(e["sequence"], e["event_type"]) for e in events] == [
        (1, "RunCreated"),
        (2, "TaskCreated"),
        (3, "FactAdded"),
        (4, "TaskStarted"),
        (5, "TaskCompleted"),
        (6, "TaskCreated"),
        (7, "TaskStarted"),
        (8, "VerificationStarted"),
        (9, "VerificationPassed"),
        (10, "TaskCompleted"),
        (11, "RunCompleted"),
    ]
    tail = (await api.get(f"{base}/events", params={"after_sequence": 3})).json()
    assert [e["sequence"] for e in tail] == list(range(4, 12))

    state = (await api.get(f"{base}/state")).json()
    assert state["status"] == "completed"
    assert state["tasks"]["t1"]["status"] == "completed"
    assert state["facts"]["f1"]["agent_id"] == "researcher"
    assert state["last_sequence"] == 11

    run_after = (await api.get(base)).json()
    assert (run_after["status"], run_after["last_sequence"]) == ("completed", 11)

    context = (await api.get(f"{base}/context", params=[("fields", "facts")])).json()
    assert set(context) == {"run_id", "last_sequence", "facts"}


async def test_error_responses(api: httpx.AsyncClient) -> None:
    run = await _create(api)
    base = f"/api/v1/runs/{run['id']}"

    missing = await api.get(f"/api/v1/runs/{uuid4()}/state")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "run_not_found"

    bad_payload = await api.post(
        f"{base}/events", json={"events": [{"event_type": "FactAdded", "payload": {}}]}
    )
    assert bad_payload.status_code == 422
    assert bad_payload.json()["error"]["code"] == "invalid_event"

    unknown_type = await api.post(
        f"{base}/events", json={"events": [{"event_type": "Teleported", "payload": {}}]}
    )
    assert unknown_type.status_code == 422

    rule_violation = await api.post(
        f"{base}/events",
        json={"events": [{"event_type": "TaskCompleted", "payload": {"task_id": "nope"}}]},
    )
    assert rule_violation.status_code == 422
    assert rule_violation.json()["error"]["code"] == "invalid_event"

    stale = await api.post(
        f"{base}/events",
        json={
            "expected_sequence": 0,
            "events": [{"event_type": "FactAdded", "payload": {"fact_id": "f", "content": "x"}}],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "sequence_conflict"

    bad_field = await api.get(f"{base}/context", params=[("fields", "secrets")])
    assert bad_field.status_code == 422

    # None of the rejected requests wrote anything.
    assert len((await api.get(f"{base}/events")).json()) == 1


async def test_failed_requests_return_their_connection(api: httpx.AsyncClient, database: Database) -> None:
    """Regression: an endpoint that raises must still close its session."""
    run = await _create(api)
    for path in (f"/api/v1/runs/{run['id']}/tasks/nope", f"/api/v1/runs/{uuid4()}/state"):
        assert (await api.get(path)).status_code == 404
        assert database.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
