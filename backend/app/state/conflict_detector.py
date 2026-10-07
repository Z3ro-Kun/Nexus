"""Deterministic conflict detection over projected state. No LLM, no I/O.

Fact identity
-------------
A fact takes part in conflict handling only if it has a structured `claim`
(subject / attribute = value [unit]). Its key is (normalized subject, normalized
attribute): lowercase with whitespace collapsed. Nothing semantic: "Product X" and
"product  x" are the same key; "Product X" and "ProductX" are not.

Values
------
A number compares numerically, a string as normalized text; units are normalized the
same way. Equal canonical values are corroboration, never a conflict. Otherwise:
- all numbers in one unit          -> NUMERIC_DISAGREEMENT
- all texts in one unit            -> TEXTUAL_DISAGREEMENT
- anything else (units differ, number vs text) -> ATTRIBUTE_DISAGREEMENT

Applicability (which facts are compared)
----------------------------------------
- CURRENT: the fact has a claim and was produced by a COMPLETED task (or has no task).
  Only CURRENT facts start conflicts.
- HISTORICAL: produced by a task that is not COMPLETED (e.g. FAILED, and possibly
  replaced). Kept in history, shown to resolvers, never compared.
- EVIDENCE: produced by a conflict-resolution task (or its replacement). Judged by the
  resolution step, never the start of a new conflict; the accepted fact of a RESOLVED
  conflict is the reference value for later facts on the same key.
- UNCLAIMED: no claim; never compared.

Detection (per fact key, never all-pairs)
-----------------------------------------
For each key: if a conflict on the key is OPEN, wait (its resolution is pending). The
candidates are CURRENT facts on the key not already part of a conflict. If there are
none, nothing happens. The group is the candidates plus, if the key has a RESOLVED
conflict, its accepted fact. If the group's canonical values are not all equal, one
conflict covering the whole group is proposed. The fingerprint (conflict type + sorted
fact ids) makes a repeated detection of the same facts impossible.
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.events.types import ConflictType, FactClaim, FactKey
from app.state.models import ConflictState, ConflictStatus, Fact, RunState, TaskStatus

CanonicalValue = tuple[Literal["number", "text"], float | str, str | None]


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def fact_key(claim: FactClaim) -> FactKey:
    return FactKey(subject=normalize(claim.subject), attribute=normalize(claim.attribute))


def canonical_value(claim: FactClaim) -> CanonicalValue:
    unit = normalize(claim.unit) if claim.unit else None
    if isinstance(claim.value, str):
        return ("text", normalize(claim.value), unit)
    return ("number", float(claim.value), unit)


def classify_disagreement(claims: Iterable[FactClaim]) -> ConflictType | None:
    """None if every claim has the same canonical value (corroboration)."""
    values = [canonical_value(c) for c in claims]
    if len(set(values)) <= 1:
        return None
    kinds = {kind for kind, _, _ in values}
    units = {unit for _, _, unit in values}
    if len(units) > 1 or len(kinds) > 1:
        return ConflictType.ATTRIBUTE_DISAGREEMENT
    return ConflictType.NUMERIC_DISAGREEMENT if kinds == {"number"} else ConflictType.TEXTUAL_DISAGREEMENT


def conflict_fingerprint(conflict_type: ConflictType, fact_ids: Iterable[str]) -> str:
    material = "|".join([conflict_type.value, *sorted(fact_ids)])
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def conflict_id_for(fingerprint: str) -> str:
    return f"conflict_{fingerprint[:12]}"


def display_value(claim: FactClaim) -> str:
    return f"{claim.value} {claim.unit}" if claim.unit else str(claim.value)


# --- task / fact relationships -------------------------------------------------------------


def resolution_origin(state: RunState, task_id: str | None) -> str | None:
    """The conflict a task resolves: its own `conflict_id`, or that of the task it
    (transitively) replaces. A replacement of a failed resolution task inherits it."""
    seen: set[str] = set()
    while task_id is not None and task_id not in seen:
        task = state.tasks.get(task_id)
        if task is None:
            return None
        if task.conflict_id is not None:
            return task.conflict_id
        seen.add(task_id)
        task_id = task.replaces
    return None


class FactApplicability(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    EVIDENCE = "evidence"
    UNCLAIMED = "unclaimed"


def applicability(state: RunState, fact: Fact) -> FactApplicability:
    if fact.claim is None:
        return FactApplicability.UNCLAIMED
    if fact.task_id is not None and fact.task_id in state.tasks:
        if resolution_origin(state, fact.task_id) is not None:
            return FactApplicability.EVIDENCE
        if state.tasks[fact.task_id].status is not TaskStatus.COMPLETED:
            return FactApplicability.HISTORICAL
    return FactApplicability.CURRENT


# --- detection ------------------------------------------------------------------------------


class ConflictCandidate(BaseModel):
    """A conflict the detector proposes. Recorded as a structured ConflictDetected."""

    model_config = ConfigDict(frozen=True)

    conflict_id: str
    conflict_type: ConflictType
    fact_key: FactKey
    fact_ids: tuple[str, ...]  # in fact (sequence) order
    fingerprint: str
    reason: str


def _structured(state: RunState) -> dict[tuple[str, str], list[ConflictState]]:
    by_key: dict[tuple[str, str], list[ConflictState]] = defaultdict(list)
    for conflict in state.conflicts.values():
        if conflict.fact_key is not None:
            by_key[(conflict.fact_key.subject, conflict.fact_key.attribute)].append(conflict)
    return by_key


def _latest_resolved(conflicts: Sequence[ConflictState]) -> ConflictState | None:
    resolved = [c for c in conflicts if c.status is ConflictStatus.RESOLVED]
    return max(resolved, key=lambda c: c.detected_sequence or 0) if resolved else None


def detect_conflicts(state: RunState) -> list[ConflictCandidate]:
    current: dict[tuple[str, str], list[Fact]] = defaultdict(list)
    for fact in sorted(state.facts.values(), key=lambda f: f.sequence):
        if applicability(state, fact) is FactApplicability.CURRENT:
            assert fact.claim is not None
            key = fact_key(fact.claim)
            current[(key.subject, key.attribute)].append(fact)

    existing = _structured(state)
    fingerprints = {c.fingerprint for c in state.conflicts.values() if c.fingerprint}
    found: list[ConflictCandidate] = []
    for key in sorted(current):
        on_key = existing.get(key, [])
        if any(c.status is ConflictStatus.OPEN for c in on_key):
            continue
        covered = {
            fid
            for c in on_key
            for fid in (*c.fact_ids, *c.evidence_ids, *([c.resolved_fact_id] if c.resolved_fact_id else []))
        }
        candidates = [f for f in current[key] if f.fact_id not in covered]
        if not candidates:
            continue
        reference = _latest_resolved(on_key)
        group = ([state.facts[reference.resolved_fact_id]] if reference and reference.resolved_fact_id else []) + candidates
        conflict_type = classify_disagreement(f.claim for f in group if f.claim)
        if conflict_type is None:
            continue  # corroboration
        fact_ids = tuple(f.fact_id for f in group)
        fingerprint = conflict_fingerprint(conflict_type, fact_ids)
        if fingerprint in fingerprints:
            continue
        subject, attribute = key
        found.append(
            ConflictCandidate(
                conflict_id=conflict_id_for(fingerprint),
                conflict_type=conflict_type,
                fact_key=FactKey(subject=subject, attribute=attribute),
                fact_ids=fact_ids,
                fingerprint=fingerprint,
                reason=(
                    f"{len(group)} facts disagree on {subject} / {attribute}: "
                    + "; ".join(f"{f.fact_id} = {display_value(f.claim)}" for f in group if f.claim)
                )[:1000],
            )
        )
    return found


# --- evidence ------------------------------------------------------------------------------


class EvidenceAssessment(BaseModel):
    """Which facts of a resolver task count as evidence for a conflict, and why not."""

    model_config = ConfigDict(frozen=True)

    usable: tuple[str, ...]  # tool-derived, same key, independent source
    excluded: tuple[tuple[str, str], ...]  # (fact id, reason)


def assess_evidence(state: RunState, conflict: ConflictState, resolver_task_id: str) -> EvidenceAssessment:
    """Evidence rules. A fact of the resolver task is usable evidence only if it
    - states a claim on the conflict's fact key,
    - is tool-derived (provenance kind tool_output, checked by the runtime against the
      task's real tool calls): the model's own opinion is never evidence, and
    - comes from a source other than the sources of the conflicting facts (re-reading
      a source that is part of the disagreement is not new evidence)."""
    assert conflict.fact_key is not None
    disputed_sources = {
        state.facts[fid].provenance.source  # type: ignore[union-attr]
        for fid in conflict.fact_ids
        if fid in state.facts and state.facts[fid].provenance is not None and state.facts[fid].provenance.source  # type: ignore[union-attr]
    }
    usable: list[str] = []
    excluded: list[tuple[str, str]] = []
    for fact in sorted(state.facts.values(), key=lambda f: f.sequence):
        if fact.task_id != resolver_task_id:
            continue
        if fact.claim is None or fact_key(fact.claim) != conflict.fact_key:
            excluded.append((fact.fact_id, "no claim on the conflict's fact key"))
        elif fact.provenance is None or fact.provenance.kind != "tool_output":
            excluded.append((fact.fact_id, "not tool-derived"))
        elif fact.provenance.source in disputed_sources:
            excluded.append((fact.fact_id, f"same source as a conflicting fact ({fact.provenance.source})"))
        else:
            usable.append(fact.fact_id)
    return EvidenceAssessment(usable=tuple(usable), excluded=tuple(excluded))


# --- current value of a key (no last-write-wins) ------------------------------------------


class KeyValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["accepted", "corroborated", "conflicting", "unknown"]
    fact_ids: tuple[str, ...] = ()
    value: FactClaim | None = None


def current_value(state: RunState, subject: str, attribute: str) -> KeyValue:
    """The value a consumer may rely on for a key. Never the latest or the first fact:
    - an OPEN or UNRESOLVED conflict (or undetected disagreement) -> "conflicting";
    - a RESOLVED conflict -> its accepted fact ("accepted");
    - otherwise, CURRENT facts that all agree -> "corroborated" (one or more facts)."""
    key = (normalize(subject), normalize(attribute))
    on_key = _structured(state).get(key, [])
    facts = [
        f
        for f in sorted(state.facts.values(), key=lambda f: f.sequence)
        if f.claim is not None and (fact_key(f.claim).subject, fact_key(f.claim).attribute) == key
    ]
    if any(c.status is not ConflictStatus.RESOLVED for c in on_key):
        return KeyValue(status="conflicting", fact_ids=tuple(f.fact_id for f in facts))
    if any((c.fact_key.subject, c.fact_key.attribute) == key for c in detect_conflicts(state)):
        return KeyValue(status="conflicting", fact_ids=tuple(f.fact_id for f in facts))
    reference = _latest_resolved(on_key)
    if reference is not None and reference.resolved_fact_id:
        accepted = state.facts[reference.resolved_fact_id]
        return KeyValue(status="accepted", fact_ids=(accepted.fact_id,), value=accepted.claim)
    current = [f for f in facts if applicability(state, f) is FactApplicability.CURRENT]
    if not current:
        return KeyValue(status="unknown")
    return KeyValue(status="corroborated", fact_ids=tuple(f.fact_id for f in current), value=current[0].claim)
