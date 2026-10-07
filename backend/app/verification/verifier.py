"""The verifier: VerificationContext -> VerificationResult. Provider-independent.

    deterministic checks (already in the context, computed from state)
        any failed  -> FAIL (the LLM is not asked: it cannot overrule a failed check)
        all passed  -> spec.semantic? ask the SemanticVerifier : PASS
                       semantic failed -> FAIL; passed -> PASS

The verifier only *returns* a result; `app.verification.manager.VerificationManager`
records it as events, and the projector re-checks it.
"""

from pydantic import BaseModel, ConfigDict

from app.core.exceptions import VerifierUnavailableError
from app.events.types import SemanticVerification, VerificationCheck
from app.verification.checks import describe_failures
from app.verification.context import VerificationContext
from app.verification.semantic import SemanticVerifier


class VerificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    verification_id: str
    passed: bool
    checks: tuple[VerificationCheck, ...]
    semantic: SemanticVerification | None = None
    # Why it failed (None when it passed); a summary when it passed.
    reason: str | None = None
    summary: str


class Verifier:
    def __init__(self, semantic: SemanticVerifier | None = None) -> None:
        self._semantic = semantic

    @property
    def has_semantic(self) -> bool:
        return self._semantic is not None

    async def verify(self, context: VerificationContext) -> VerificationResult:
        checks = context.checks
        failing = [c for c in checks if not c.passed]
        semantic = None
        if not failing and context.spec.semantic:
            if self._semantic is None:
                raise VerifierUnavailableError(
                    f"checkpoint {context.verification_id!r} requires semantic verification, "
                    "but no LLM verifier is configured"
                )
            semantic = await self._semantic.assess(context)
        return result_for(context.verification_id, checks, semantic)


def result_for(
    verification_id: str, checks: tuple[VerificationCheck, ...], semantic: SemanticVerification | None
) -> VerificationResult:
    """The verdict for deterministic checks plus an optional semantic verdict."""
    failing = [c for c in checks if not c.passed]
    passed = not failing and (semantic is None or semantic.passed)
    if passed:
        summary = f"{len(checks)} deterministic checks passed" + (
            f"; semantic verification passed ({semantic.provider}/{semantic.model})" if semantic else ""
        )
        return VerificationResult(
            verification_id=verification_id, passed=True, checks=checks, semantic=semantic, summary=summary
        )
    parts = []
    if failing:
        parts.append(f"{len(failing)} of {len(checks)} checks failed: {describe_failures(checks)}")
    if semantic is not None and not semantic.passed:
        rejected = [j.criterion for j in (semantic.objective, *semantic.constraints) if not j.passed]
        parts.append(f"semantic verification failed for: {'; '.join(rejected)}")
    reason = " | ".join(parts)[:2000]
    return VerificationResult(
        verification_id=verification_id, passed=False, checks=checks, semantic=semantic,
        reason=reason, summary=reason[:500],
    )
