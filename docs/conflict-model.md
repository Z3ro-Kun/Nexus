# NEXUS Conflict Model (Phase 6)

Agents and tools can report facts that disagree. NEXUS does not settle this by last
write, first write, agent priority, confidence scores or an LLM's opinion. A conflict is
recorded explicitly and resolved only by **new, tool-derived evidence**. Every claim,
the conflict and its resolution stay in the event log.

```
Agent facts                    FactAdded (+ structured claim, provenance)
   ↓
Conflict Detector      [code]  app/state/conflict_detector.py, per fact key
   ↓
Conflict?
 ├── NO  → continue            (agreeing values are corroboration)
 └── YES
       ↓
 ConflictDetected      [code]  structured: type, key, fact ids, fingerprint, reason
       ↓
 Resolution Task       [code]  TaskCreated(conflict_id=...): an ordinary researcher task
       ↓
 Additional Evidence   [LLM+tools]  the agent consults a different source, reports facts
       ↓
 Verdict               [code]  app/conflicts/resolution.py: evidence rules only
       ↓
 ConflictResolved / ConflictUnresolved
```

## Fact identity

There is one fact system: `FactAdded` / `RunState.facts`. It gained a single optional
field, `claim`:

| field | meaning |
|---|---|
| `subject` | the entity, e.g. `Product X` |
| `attribute` | e.g. `price` |
| `value` | a number (compared numerically) or a string (compared as normalized text) |
| `unit` | optional, e.g. `INR` |

The **fact key** is `(normalized subject, normalized attribute)`. Normalization is
lowercase with whitespace collapsed, and nothing more. The free-text `content` is never
the identity. There is no semantic entity resolution: `Product X` and `product  x` are
the same key, while `Product X` and `ProductX` are not.

A fact otherwise keeps its Phase 4 shape: id, content, source, provenance (kind, tool,
tool call, source, retrieval time, fake flag), agent, task and sequence. It also gains
`recorded_at`, the event timestamp. Agents supply `claim` in their structured report.
The runtime records it unchanged, and it is subject to the usual provenance checks.
Facts without a claim never take part in conflict handling.

## Conflict types

Values are compared in canonical form (number or normalized text, plus normalized unit).
Equal canonical values are **corroboration**, never a conflict. Otherwise:

| type | when |
|---|---|
| `NUMERIC_DISAGREEMENT` | all values are numbers in the same unit, and they differ |
| `TEXTUAL_DISAGREEMENT` | all values are texts in the same unit, and they differ |
| `ATTRIBUTE_DISAGREEMENT` | the values are not comparable: units differ, or number vs text |

Numbers compare exactly: `99999` equals `99999.0`, but `99999` does not equal
`"99999"`. There is no tolerance, currency conversion or text parsing.

## Which facts are compared (applicability)

| applicability | rule | used for |
|---|---|---|
| CURRENT | has a claim; its task is COMPLETED, or it has no task | starting conflicts |
| HISTORICAL | its task is not COMPLETED (e.g. FAILED, possibly replaced in Phase 5) | kept and visible; never compared |
| EVIDENCE | produced by a resolution task or a replacement of one | judged by the verdict; never starts a conflict itself |
| UNCLAIMED | no claim | never compared |

The runtime records facts only together with `TaskCompleted`, so HISTORICAL facts
arise only from events appended by other writers (e.g. through the API). They are never
deleted or rewritten; they just don't count as current claims.

## Detection

`detect_conflicts(state)` is pure and deterministic, and it never compares all pairs.
For each fact key:

1. If a conflict on the key is **OPEN**, the key is skipped: resolution is pending, and
   new facts on that key wait for it.
2. The candidates are CURRENT facts on the key that are not already part of a conflict.
   If there are none, nothing happens.
3. The group is the candidates plus, if the key has a RESOLVED conflict, its **accepted
   fact**. Later claims are therefore compared with the accepted value, not with the
   superseded ones.
4. If the group's canonical values are not all equal, one conflict covers the whole
   group.

**Fingerprint:** SHA-256 over the conflict type and the sorted fact ids. The conflict id
is `conflict_<first 12 hex>`. The projector rejects a second conflict with the same
fingerprint, so the same facts can never produce the same conflict twice.

`ConflictManager` (`app/conflicts/manager.py`) runs detection when the scheduler starts
and after every recorded `TaskCompleted`. It appends one `ConflictDetected` plus one
resolution `TaskCreated` per new conflict, atomically. Several conflicts found at once
share the same append.

## Resolution task

This is an ordinary task (`agent_type=researcher`, `task_type=research`), run by the
ordinary agent runtime with its ordinary tools, tool policy and limits. No new LLM
architecture is involved.

- id `resolve_<conflict_id>`; `TaskCreated.conflict_id` links it to the conflict (the
  projector requires the conflict to be OPEN with no resolution task yet);
- it depends on the completed tasks that produced the conflicting facts, so it receives
  their results;
- its description states the objective ("Determine the current verified price of
  product x"), the conflicting values and the sources already used;
- its task context contains a `conflict` section: conflict id and type, fact key, each
  conflicting fact with its claim, provenance, task and agent, and the objective. Other
  tasks' contexts don't include this section.

**Budget:** `NEXUS_MAX_CONFLICT_RESOLUTIONS_PER_RUN` (default 5; 0 = detect only).
Conflicts beyond the budget are still recorded, but stay OPEN without a task.

## Evidence

The agent reports facts; it never declares a winner. A fact of the resolver task is
**usable evidence** only if it:

1. has a claim on the conflict's fact key;
2. is tool-derived (`provenance.kind == "tool_output"`). The runtime has already
   checked that it cites a successful tool call of this task, and that any source URL
   appears in that call's output. The model's own knowledge is never evidence;
3. comes from a source other than the conflicting facts' sources. Re-reading a
   disputed source is not new evidence.

The evidence keeps its full provenance: source, retrieval time, tool, tool call, fake
flag, agent and task.

## Resolution (the verdict)

After the resolver task completes, `evaluate` (`app/conflicts/resolution.py`) decides:

| evidence | verdict |
|---|---|
| none usable | `ConflictUnresolved` (reason lists what was excluded and why) |
| usable, but values disagree | `ConflictUnresolved` |
| usable and in agreement | `ConflictResolved` |

`ConflictResolved` records:
- `resolved_fact_id`: the first evidence fact, which becomes the **currently accepted
  value**;
- `evidence_fact_ids`;
- `corroborated_fact_ids`: the conflicting facts that have the accepted value, if any;
- `resolver_task_id`;
- `reason`.

The time is the envelope timestamp. "Accepted" doesn't delete or demote anything: the
alternatives stay facts in the log and in state.

The projector re-checks every verdict with the same rules, so no writer (including the
raw API) can resolve a conflict:
- before it is detected, or twice;
- with a resolver task that isn't COMPLETED, or isn't the conflict's resolution task or
  its replacement;
- with evidence that isn't usable, or that disagrees;
- with an accepted fact that isn't part of the evidence;
- with a wrong list of corroborated facts.

A free-text (`resolution` only) `ConflictResolved` is refused for structured conflicts.

`current_value(state, subject, attribute)` returns what a consumer may rely on:
- `accepted`: a RESOLVED conflict's fact;
- `corroborated`: current facts that all agree;
- `conflicting`: an OPEN or UNRESOLVED conflict, or a disagreement not yet recorded;
- `unknown`.

It never returns the latest or the first fact.

## Unresolved conflicts

- **UNRESOLVED:** the resolution task finished without a reliable result. No winner is
  chosen, and all evidence is kept (`evidence_ids`).
- **OPEN:** detected, with no verdict yet. This covers resolution in progress, the
  resolution task failed and was not recovered, or the budget was spent.

A later verifier treats both as "not resolved". In this phase an UNRESOLVED conflict is
final: no automatic second attempt is made, which keeps resolution bounded.

## Relationship to recovery (Phase 5)

A failed resolution task is not special. The scheduler records `TaskFailed` (classified)
and hands it to the `RecoveryManager` like any other failure. The same policy and budget
apply, and the replanner can propose a replacement (`replaces`). The replacement
**inherits the conflict through the replacement chain** (`resolution_origin`): the
replanner can't set `conflict_id`. The replacement's context includes the conflict, and
when it completes its evidence is judged exactly as above (`resolver_task_id` = the
replacement; `resolution_task_id` stays the original). If recovery isn't possible or the
budget runs out, the conflict simply stays OPEN.

## Events (summary)

| event | writer | meaning |
|---|---|---|
| `FactAdded` (+ `claim`) | scheduler (agent result) | a claim with provenance |
| `ConflictDetected` | ConflictManager (`conflict_detector`) | structured conflict |
| `TaskCreated` (+ `conflict_id`) | ConflictManager | the resolution task |
| `ConflictResolved` | ConflictManager (`conflict_resolver`) | accepted value backed by evidence |
| `ConflictUnresolved` | ConflictManager (`conflict_resolver`) | no reliable result; no winner |

Valid order: FactAdded(A), FactAdded(B), ConflictDetected, TaskCreated(resolution),
TaskStarted, FactAdded(evidence), TaskCompleted, ConflictResolved.

State (`RunState.conflicts[id]`): status, fact ids, type, fact key, fingerprint,
detected_at/sequence, resolution task, resolver task, accepted fact, evidence ids,
corroborated ids, resolution reason and resolved_at. All of it is derived from events.

## Limitations

- Real LLM conflict resolution has not been verified; every test and the demo use
  `FakeLLMProvider` and fake search.
- Identity is only as good as the claims agents report: no entity resolution, unit
  conversion or numeric tolerance.
- Tasks that depend on the conflicting facts' tasks are not held back while a conflict
  is open. In the demo, the comparison runs alongside the resolution task. Phase 7
  verification decides whether a result built on disputed facts is acceptable: a
  checkpoint waits while a relevant resolution is pending and fails on OPEN or
  UNRESOLVED conflicts (see [verification-model.md](verification-model.md)).
- One resolution attempt per conflict (plus Phase 5 replacements of a failed task).
