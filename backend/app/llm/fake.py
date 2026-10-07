"""Deterministic in-process provider for tests and offline development.

It never calls a network service. Each request is answered with a reply scripted by the
caller, chosen by `request.purpose` (e.g. "planner", "agent:researcher"). A reply can be
a callable, so one script can answer differently per task. Replies can return data,
raise a provider error, or block on an `asyncio.Event` (to test concurrency).
"""

import asyncio
import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.exceptions import LLMError
from app.llm.schemas import LLMRequest, LLMResponse, LLMUsage


@dataclass(frozen=True)
class FakeReply:
    data: Mapping[str, Any] | None = None
    error: LLMError | None = None
    wait_for: asyncio.Event | None = None
    delay_seconds: float = 0.0


ReplySource = FakeReply | Callable[[LLMRequest], FakeReply]


class FakeLLMProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, replies: Mapping[str, ReplySource] | None = None) -> None:
        self._replies = dict(replies or {})
        self.requests: list[LLMRequest] = []
        self.active: set[str] = set()
        self.max_active = 0
        self._started: dict[str, asyncio.Event] = {}

    async def generate(self, request: LLMRequest) -> LLMResponse:
        source = self._replies.get(request.purpose)
        if source is None:
            raise LLMError(f"fake provider has no scripted reply for {request.purpose!r}")
        reply = source(request) if callable(source) else source

        label = request.metadata.get("task_id", request.purpose)
        self.requests.append(request)
        self.active.add(label)
        self.max_active = max(self.max_active, len(self.active))
        self._started.setdefault(label, asyncio.Event()).set()
        try:
            if reply.wait_for is not None:
                await reply.wait_for.wait()
            if reply.delay_seconds:
                await asyncio.sleep(reply.delay_seconds)
        finally:
            self.active.discard(label)

        if reply.error is not None:
            raise reply.error
        if reply.data is None:
            raise LLMError("fake reply has neither data nor error")
        return LLMResponse(
            data=copy.deepcopy(dict(reply.data)),
            provider=self.name,
            model=self.model,
            usage=LLMUsage(input_tokens=0, output_tokens=0),
        )

    async def wait_until_called(self, label: str) -> None:
        """Wait until a request labelled `label` (task id, or purpose) is in progress."""
        await self._started.setdefault(label, asyncio.Event()).wait()
