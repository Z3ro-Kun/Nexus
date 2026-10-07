"""Phase 6 unit tests: fact identity, deterministic conflict detection, conflict events
and their ordering rules, resolution verdicts, provenance, safety, reconstruction.

Pure: event histories are built in memory and projected. No database, LLM or network.
"""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.conflicts.resolution import evaluate, resolution_task, resolution_task_id
from app.core.exceptions import InvalidEventError
from app.events.base import Event
from app.events.types import (
    ConflictDetected,
    ConflictResolved,
    ConflictType,
    ConflictUnresolved,
    EventPayload,
    FactAdded,
    FactClaim,
    FactKey,
    Provenance,
    RunCreated,
    TaskCompleted,
    TaskCreated,
    TaskFailed,
    TaskStarted,
)
from app.state.conflict_detector import (
    FactApplicability,
    applicability,
    conflict_fingerprint,
    current_value,
    detect_conflicts,
    fact_key,
)
from app.state.context_builder import build_task_context
from app.state.models import ConflictStatus, RunState
from app.state.projector import apply, project

T = ConflictType
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RUN = uuid4()


class Log:
    """Builds a run history; each step may carry an envelope task id / agent id."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.add(RunCreated(goal="Research several candidate products and compare them."))

    def add(self, payload: EventPayload, *, task: str | None = None, agent: str | None = None) -> "Log":
        n = len(self.events) + 1
        self.events.append(
            Event(id=uuid4(), run_id=RUN, sequence=n, event_type=payload.event_type,
                  timestamp=BASE + timedelta(seconds=n), agent_id=agent, task_id=task, payload=payload)
        )
        return self

    def task(self, task_id: str, *deps: str, complete: bool = True, **kw: Any) -> "Log":
        self.add(TaskCreated(task_id=task_id, title=task_id, agent_type="researcher", task_type="research",
                             dependencies=list(deps), **kw))
        self.add(TaskStarted(task_id=task_id), task=task_id)
        return self

    def done(self, task_id: str) -> "Log":
        return self.add(TaskCompleted(task_id=task_id, summary="ok"), task=task_id, agent="researcher")

    def fact(self, fact_id: str, task: str | None, value: Any, *, subject: str = "Product X", attribute: str = "price",
             unit: str | None = "INR", source: str | None = None, kind: str = "tool_output") -> "Log":
        provenance = Provenance(kind=kind, tool_name="web_search" if kind == "tool_output" else None,  # type: ignore[arg-type]
                                tool_call_id=f"{task}.t1" if kind == "tool_output" else None,
                                source=source or f"https://fake-search.invalid/{fact_id}", fake=True)
        claim = FactClaim(subject=subject, attribute=attribute, value=value, unit=unit)
        return self.add(FactAdded(fact_id=fact_id, content=f"{subject} {attribute} {value}", source=provenance.source,
                                  provenance=provenance, claim=claim), task=task, agent="researcher")

    def researched(self, task_id: str, fact_id: str, value: Any, **kw: Any) -> "Log":
        return self.task(task_id).fact(fact_id, task_id, value, **kw).done(task_id)

    def state(self) -> RunState:
        return project(self.events)

    def detect(self) -> "Log":
        """Record whatever the detector finds, plus resolution tasks (as the manager does)."""
        state = self.state()
        for c in detect_conflicts(state):
            self.add(ConflictDetected(conflict_id=c.conflict_id, fact_ids=list(c.fact_ids), conflict_type=c.conflict_type,
                                      fact_key=c.fact_key, fingerprint=c.fingerprint, reason=c.reason), agent="conflict_detector")
            self.add(resolution_task(self.state(), c), agent="conflict_detector")
        return self


def conflicting() -> Log:
    return Log().researched("ra", "fa", 94999).researched("rb", "fb", 99999)


def only_conflict(state: RunState) -> Any:
    [conflict] = state.conflicts.values()
    return conflict


# --- 1-6: detection ----------------------------------------------------------------------------


def test_contradictory_numeric_facts_create_a_conflict() -> None:
    [c] = detect_conflicts(conflicting().state())
    assert (c.conflict_type, c.fact_ids, c.fact_key) == (T.NUMERIC_DISAGREEMENT, ("fa", "fb"), FactKey(subject="product x", attribute="price"))
    assert "fa = 94999 INR; fb = 99999 INR" in c.reason


def test_contradictory_textual_facts_create_a_conflict() -> None:
    log = Log().researched("ra", "fa", "Available", attribute="stock", unit=None).researched("rb", "fb", "Sold out", attribute="stock", unit=None)
    [c] = detect_conflicts(log.state())
    assert c.conflict_type is T.TEXTUAL_DISAGREEMENT


@pytest.mark.parametrize(
    ("a", "b"),
    [({"unit": "INR"}, {"unit": "USD"}), ({"unit": None}, {"unit": None, "value": "about a lakh"})],
)
def test_incomparable_values_are_attribute_disagreements(a: dict[str, Any], b: dict[str, Any]) -> None:
    log = Log().researched("ra", "fa", a.pop("value", 94999), **a).researched("rb", "fb", b.pop("value", 1199), **b)
    [c] = detect_conflicts(log.state())
    assert c.conflict_type is T.ATTRIBUTE_DISAGREEMENT


def test_different_subjects_do_not_conflict() -> None:
    log = Log().researched("ra", "fa", 94999).researched("rb", "fb", 99999, subject="Product Y")
    assert detect_conflicts(log.state()) == []


def test_different_attributes_do_not_conflict() -> None:
    log = Log().researched("ra", "fa", 94999).researched("rb", "fb", 4.5, attribute="rating", unit=None)
    assert detect_conflicts(log.state()) == []


@pytest.mark.parametrize(("b_value", "b_subject"), [(99999, "Product X"), (99999.0, "  product   X "), ("99999", "Product X")])
def test_identical_values_are_corroboration_not_conflict(b_value: Any, b_subject: str) -> None:
    log = Log().researched("ra", "fa", 99999).researched("rb", "fb", b_value, subject=b_subject)
    state = log.state()
    if isinstance(b_value, str):  # number vs text is not the same value: attribute disagreement
        assert [c.conflict_type for c in detect_conflicts(state)] == [T.ATTRIBUTE_DISAGREEMENT]
        return
    assert detect_conflicts(state) == []
    value = current_value(state, "product x", "PRICE")
    assert (value.status, value.fact_ids) == ("corroborated", ("fa", "fb"))  # two provenance records kept
    assert state.facts["fa"].provenance != state.facts["fb"].provenance


def test_repeated_same_conflict_is_deduplicated() -> None:
    log = conflicting().detect()
    state = log.state()
    assert len(state.conflicts) == 1 and detect_conflicts(state) == []  # nothing new to detect
    c = only_conflict(state)
    duplicate = ConflictDetected(conflict_id="other_id", fact_ids=["fb", "fa"], conflict_type=c.conflict_type,
                                 fact_key=c.fact_key, fingerprint=c.fingerprint, reason="again")
    with pytest.raises(InvalidEventError, match="fingerprint does not match|already detected"):
        log.add(duplicate).state()
    assert conflict_fingerprint(T.NUMERIC_DISAGREEMENT, ["fb", "fa"]) == conflict_fingerprint(T.NUMERIC_DISAGREEMENT, ["fa", "fb"])


def test_detection_is_per_key_not_all_pairs() -> None:
    log = conflicting().researched("rc", "fc", 1000, subject="Product Y").researched("rd", "fd", 1200, subject="Product Y")
    found = detect_conflicts(log.state())
    assert sorted(c.fact_ids for c in found) == [("fa", "fb"), ("fc", "fd")]


# --- 7-10: events --------------------------------------------------------------------------------


def test_conflict_detected_has_a_structured_payload() -> None:
    state = conflicting().detect().state()
    c = only_conflict(state)
    assert (c.status, c.conflict_type, c.fact_ids) == (ConflictStatus.OPEN, T.NUMERIC_DISAGREEMENT, ("fa", "fb"))
    assert c.fact_key == FactKey(subject="product x", attribute="price")
    assert c.fingerprint == conflict_fingerprint(T.NUMERIC_DISAGREEMENT, ["fa", "fb"])
    assert c.detected_at is not None and c.resolution_task_id == resolution_task_id(c.conflict_id)
    with pytest.raises(ValidationError, match="must be given together"):
        ConflictDetected(conflict_id="c", fact_ids=["fa", "fb"], conflict_type=T.NUMERIC_DISAGREEMENT, reason="r")


def test_original_facts_remain_unchanged() -> None:
    before = conflicting().state()
    after = conflicting().detect().state()
    assert after.facts == before.facts


def resolved_log() -> Log:
    log = conflicting().detect()
    c = only_conflict(log.state())
    resolver = c.resolution_task_id
    log.add(TaskStarted(task_id=resolver), task=resolver).fact("fc", resolver, 96999).done(resolver)
    log.add(evaluate(log.state(), only_conflict(log.state()), resolver), task=resolver, agent="conflict_resolver")
    return log


def test_conflict_resolved_has_a_structured_payload() -> None:
    log = resolved_log()
    event = log.events[-1]
    assert isinstance(event.payload, ConflictResolved)
    p = event.payload
    assert (p.resolved_fact_id, p.evidence_fact_ids, p.corroborated_fact_ids) == ("fc", ["fc"], [])
    assert p.resolver_task_id.startswith("resolve_conflict_") and "96999 INR" in (p.reason or "")
    c = only_conflict(log.state())
    assert (c.status, c.resolved_fact_id, c.evidence_ids, c.resolver_task_id) == (ConflictStatus.RESOLVED, "fc", ("fc",), p.resolver_task_id)
    assert c.resolved_at == event.timestamp


def test_invalid_resolution_ordering_is_rejected() -> None:
    log = conflicting()
    premature = ConflictResolved(conflict_id="conflict_x", resolved_fact_id="fa", evidence_fact_ids=["fa"], resolver_task_id="ra", reason="r")
    with pytest.raises(InvalidEventError, match="does not exist"):
        log.add(premature).state()  # resolved before detected

    log = conflicting().detect()
    c = only_conflict(log.state())
    resolver = c.resolution_task_id
    early = ConflictResolved(conflict_id=c.conflict_id, resolved_fact_id="fa", evidence_fact_ids=["fa"], resolver_task_id=resolver, reason="r")
    with pytest.raises(InvalidEventError, match="is ready; requires completed"):
        project([*log.events, _as_event(log, early)])  # resolver has not even started

    done = resolved_log()
    with pytest.raises(InvalidEventError, match="already resolved"):
        project([*done.events, _as_event(done, done.events[-1].payload)])


def _as_event(log: Log, payload: EventPayload) -> Event:
    n = len(log.events) + 1
    return Event(id=uuid4(), run_id=RUN, sequence=n, event_type=payload.event_type, timestamp=BASE + timedelta(seconds=n), payload=payload)


@pytest.mark.parametrize(
    "tamper",
    [
        {"evidence_fact_ids": ["fa"], "resolved_fact_id": "fa"},  # an original fact is not new evidence
        {"resolved_fact_id": "fb"},  # accepted fact not among the evidence
        {"resolver_task_id": "ra"},  # not the resolution task
        {"corroborated_fact_ids": ["fa"]},  # fa does not have the accepted value
    ],
)
def test_resolution_must_be_backed_by_its_evidence(tamper: dict[str, Any]) -> None:
    log = resolved_log()
    genuine = log.events.pop().payload
    forged = genuine.model_copy(update=tamper)
    with pytest.raises(InvalidEventError):
        project([*log.events, _as_event(log, forged)])


# --- 13-15 (verdict level), 19-21, 29-30 ---------------------------------------------------------


def resolver_reports(*facts: tuple[str, Any, dict[str, Any]]) -> tuple[Log, Any]:
    log = conflicting().detect()
    resolver = only_conflict(log.state()).resolution_task_id
    log.add(TaskStarted(task_id=resolver), task=resolver)
    for fact_id, value, kw in facts:
        log.fact(fact_id, resolver, value, **kw)
    log.done(resolver)
    return log, evaluate(log.state(), only_conflict(log.state()), resolver)


def test_evidence_that_matches_a_side_resolves_and_corroborates_it() -> None:
    _, verdict = resolver_reports(("fc", 99999, {}))
    assert isinstance(verdict, ConflictResolved) and verdict.corroborated_fact_ids == ["fb"]


@pytest.mark.parametrize(
    ("facts", "why"),
    [
        ((), "no independent tool-derived evidence"),
        ((("fc", 96999, {"kind": "model_knowledge"}),), "not tool-derived"),  # 30: LLM-only claim
        ((("fc", 94999, {"source": "https://fake-search.invalid/fa"}),), "same source as a conflicting fact"),
        ((("fc", 96999, {}), ("fd", 97999, {"source": "https://fake-search.invalid/other"})), "the new evidence disagrees"),
        ((("fc", 4.5, {"attribute": "rating", "unit": None}),), "no claim on the conflict's fact key"),
    ],
)
def test_unreliable_evidence_leaves_the_conflict_unresolved(facts: Any, why: str) -> None:
    log, verdict = resolver_reports(*facts)
    assert isinstance(verdict, ConflictUnresolved) and why in verdict.reason
    state = log.add(verdict).state()
    c = only_conflict(state)
    assert c.status is ConflictStatus.UNRESOLVED and c.resolved_fact_id is None  # no manufactured winner
    assert current_value(state, "Product X", "price").status == "conflicting"


def test_provenance_is_preserved_on_conflicting_facts_evidence_and_resolution() -> None:
    state = resolved_log().state()
    c = only_conflict(state)
    for fid in ("fa", "fb"):
        p = state.facts[fid].provenance
        assert p is not None and (p.kind, p.tool_name, p.fake) == ("tool_output", "web_search", True)
        assert state.facts[fid].task_id in ("ra", "rb") and state.facts[fid].agent_id == "researcher"
    evidence = state.facts["fc"]
    assert evidence.provenance is not None and evidence.provenance.source == "https://fake-search.invalid/fc"
    assert evidence.task_id == c.resolver_task_id and evidence.recorded_at is not None
    assert state.tasks[c.resolver_task_id].conflict_id == c.conflict_id
    assert applicability(state, evidence) is FactApplicability.EVIDENCE


def test_no_last_write_wins() -> None:
    log = conflicting()
    assert current_value(log.state(), "Product X", "price").status == "conflicting"  # not fb (latest), not fa
    log.detect()
    assert current_value(log.state(), "Product X", "price").status == "conflicting"
    resolved = resolved_log().state()
    value = current_value(resolved, "Product X", "price")
    assert (value.status, value.fact_ids, value.value.value) == ("accepted", ("fc",), 96999)  # type: ignore[union-attr]


def test_legacy_free_text_resolution_is_refused_for_structured_conflicts() -> None:
    log = conflicting().detect()
    with pytest.raises(InvalidEventError, match="requires evidence"):
        log.add(ConflictResolved(conflict_id=only_conflict(log.state()).conflict_id, resolution="fa seems right")).state()


# --- 12: context --------------------------------------------------------------------------------


def test_resolution_task_context_contains_the_conflict() -> None:
    state = conflicting().detect().state()
    c = only_conflict(state)
    task = state.tasks[c.resolution_task_id]
    assert (task.agent_type, task.task_type, task.dependencies) == ("researcher", "research", ("ra", "rb"))
    assert "Determine the current verified price of product x" in (task.description or "")
    brief = build_task_context(state, task.task_id).conflict
    assert brief is not None and brief.conflict_type is T.NUMERIC_DISAGREEMENT
    assert [(f.fact_id, f.task_id, f.claim.value) for f in brief.facts] == [("fa", "ra", 94999), ("fb", "rb", 99999)]  # type: ignore[union-attr]
    assert all(f.provenance and f.provenance.kind == "tool_output" for f in brief.facts)
    assert build_task_context(state, "ra").conflict is None


def test_resolution_task_message_is_unchanged_by_the_task_scope_framing() -> None:
    from app.agents.reasoning import task_message

    state = conflicting().detect().state()
    context = build_task_context(state, only_conflict(state).resolution_task_id)  # type: ignore[arg-type]

    message = task_message(context)

    # Exactly the pre-existing resolver message: no goal/task labelling; the conflict section is shown.
    assert message == (
        "Complete the task described in this task context. Respond with the structured output.\n\n"
        f"<task_context>\n{context.model_dump_json(indent=2)}\n</task_context>"
    )
    assert "Goal (background context)" not in message and '"conflict"' in message


# --- 18: facts of failed tasks; later facts vs accepted value ------------------------------------


def test_facts_of_failed_tasks_are_historical_and_never_compared() -> None:
    log = Log().researched("ra", "fa", 94999).task("rb").fact("fb", "rb", 99999)
    log.add(TaskFailed(task_id="rb", error="failed after recording"), task="rb")
    state = log.state()
    assert applicability(state, state.facts["fb"]) is FactApplicability.HISTORICAL
    assert detect_conflicts(state) == [] and "fb" in state.facts  # kept, not deleted


def test_later_fact_is_compared_with_the_accepted_value() -> None:
    log = resolved_log().researched("re", "fe", 96999.0)
    assert detect_conflicts(log.state()) == []  # agrees with the accepted value
    log = resolved_log().researched("re", "fe", 91999)
    [c] = detect_conflicts(log.state())
    assert c.fact_ids == ("fc", "fe")  # accepted fact vs the new claim


def test_open_conflict_defers_new_facts_on_the_same_key() -> None:
    log = conflicting().detect().researched("re", "fe", 91999)
    assert detect_conflicts(log.state()) == []


# --- 25-28: reconstruction and safety ------------------------------------------------------------


def test_raw_events_reconstruct_identical_conflict_state() -> None:
    events = resolved_log().researched("rc2", "fx", 10, subject="Product Y").researched("rd2", "fy", 12, subject="Product Y").detect().events
    rebuilt = project(events)
    incremental = None
    for event in events:
        incremental = apply(incremental, event)
    assert rebuilt == incremental == project([e.model_copy() for e in events])
    assert sorted(c.status.value for c in rebuilt.conflicts.values()) == ["open", "resolved"]
    assert {c.fact_key.subject for c in rebuilt.conflicts.values()} == {"product x", "product y"}  # type: ignore[union-attr]


def test_no_deletion_of_facts_or_conflicts() -> None:
    log = resolved_log()
    states = []
    for n in range(1, len(log.events) + 1):
        states.append(project(log.events[:n]))
    for earlier, later in zip(states, states[1:]):
        assert set(earlier.facts) <= set(later.facts)
        assert set(earlier.conflicts) <= set(later.conflicts)
        for fid, fact in earlier.facts.items():
            assert later.facts[fid] == fact  # facts are immutable
    final = states[-1]
    assert {"fa", "fb", "fc"} <= set(final.facts) and only_conflict(final).fact_ids == ("fa", "fb")


def test_fact_claim_is_strictly_typed() -> None:
    with pytest.raises(ValidationError):
        FactClaim(subject="x", attribute="price", value=True)  # type: ignore[arg-type]
    assert fact_key(FactClaim(subject="  Product   X ", attribute="Price", value=1)) == FactKey(subject="product x", attribute="price")
