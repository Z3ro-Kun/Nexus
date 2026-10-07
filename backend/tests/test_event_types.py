import json

import pytest

from app.core.exceptions import InvalidEventError, UnsupportedEventError
from app.events.factory import new_event, parse_payload
from app.events.types import PAYLOAD_TYPES, EventType, FactAdded, ToolCalled


def test_every_event_type_has_a_payload_schema() -> None:
    assert set(PAYLOAD_TYPES) == set(EventType)
    for event_type, cls in PAYLOAD_TYPES.items():
        assert cls.event_type is event_type


def test_parse_payload_returns_typed_model() -> None:
    payload = parse_payload("FactAdded", {"fact_id": "f1", "content": "Paris is in France"})

    assert isinstance(payload, FactAdded)
    assert payload.fact_id == "f1"


def test_unknown_event_type_is_unsupported() -> None:
    with pytest.raises(UnsupportedEventError):
        parse_payload("SomethingNew", {})


@pytest.mark.parametrize(
    "data",
    [
        {"content": "missing fact_id"},
        {"fact_id": "", "content": "empty id"},
        {"fact_id": "f1", "content": "x", "unexpected": True},
    ],
)
def test_invalid_payload_is_rejected(data: dict[str, object]) -> None:
    with pytest.raises(InvalidEventError):
        parse_payload(EventType.FACT_ADDED, data)


def test_invalid_envelope_is_rejected() -> None:
    with pytest.raises(InvalidEventError):
        new_event(EventType.REPLAN_TRIGGERED, {"reason": "x"}, agent_id="")


def test_payloads_are_json_round_trippable() -> None:
    draft = new_event(
        EventType.TOOL_CALLED,
        {"tool_call_id": "c1", "tool_name": "search", "arguments": {"q": "nexus", "n": 3}},
        agent_id="researcher",
    )
    data = json.loads(json.dumps(draft.payload.model_dump(mode="json")))

    assert parse_payload(EventType.TOOL_CALLED, data) == draft.payload
    assert isinstance(draft.payload, ToolCalled)


def test_payloads_are_immutable() -> None:
    payload = FactAdded(fact_id="f1", content="x")
    with pytest.raises(ValueError):
        payload.content = "changed"  # type: ignore[misc]
