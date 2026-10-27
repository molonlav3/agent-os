"""A turn sees resolved names and coerced arguments; rejections count as failures."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from agentos.engine import Agent, AgentConfig, DoneEvent, ToolCall
from agentos.engine.progress_watchdog import (
    ProgressObservation,
    ProgressWatchdog,
    tool_call_signature,
)
from agentos.provider import ChatConfig, Message
from agentos.provider import DoneEvent as ProviderDone
from agentos.provider import TextDeltaEvent as ProviderText
from agentos.provider import ToolUseEndEvent as ProviderToolUseEnd
from agentos.provider import ToolUseStartEvent as ProviderToolUseStart
from agentos.tools.dispatch import build_tool_handler
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolContext, ToolSpec


class _ScriptedToolProvider:
    """Emits the same tool call ``rounds`` times, then answers in text."""

    provider_name = "fake"

    def __init__(self, *, tool_name: str, arguments: dict[str, Any], rounds: int) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.rounds = rounds
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number > self.rounds:
            yield ProviderText(text="done")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)
            return
        tool_use_id = f"call-{call_number}"
        yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=self.tool_name)
        yield ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name=self.tool_name,
            arguments=dict(self.arguments),
        )
        yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    async def _count(**kwargs: Any) -> str:
        calls.append(kwargs)
        return f"counted {kwargs['n']}"

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="count_things",
            description="Count things.",
            parameters={"n": {"type": "integer"}},
            required=["n"],
        ),
        _count,
    )
    return registry


def _tool_results(events: list[Any]) -> list[Any]:
    return [event for event in events if getattr(event, "kind", None) == "tool_result"]


async def test_turn_runs_a_misnamed_call_with_stringified_arguments() -> None:
    calls: list[dict[str, Any]] = []
    provider = _ScriptedToolProvider(
        tool_name="functions.Count_Things:0", arguments={"n": "4"}, rounds=1
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_handler=build_tool_handler(_registry(calls), ToolContext()),
    )

    events = [event async for event in agent.run_turn("count them")]

    assert calls == [{"n": 4}]
    assert any(isinstance(event, DoneEvent) for event in events)
    results = _tool_results(events)
    assert len(results) == 1
    assert getattr(results[0], "is_error", False) is False


async def test_repeated_rejections_reach_the_failure_loop_guard() -> None:
    calls: list[dict[str, Any]] = []
    provider = _ScriptedToolProvider(tool_name="count_things", arguments={"n": "many"}, rounds=3)
    agent = Agent(
        provider=provider,
        config=AgentConfig(tool_failure_loop_block_threshold=3, max_iterations=5),
        tool_handler=build_tool_handler(_registry(calls), ToolContext()),
    )

    events = [event async for event in agent.run_turn("count them")]

    reasons = [
        (getattr(event, "execution_status", None) or {}).get("reason")
        for event in _tool_results(events)
    ]
    assert calls == []
    assert reasons == ["invalid_arguments", "invalid_arguments", "tool_failure_loop_exhausted"]


async def test_rejected_call_is_recorded_like_any_failed_call() -> None:
    agent = Agent(
        provider=_ScriptedToolProvider(tool_name="count_things", arguments={}, rounds=0),
        config=AgentConfig(tool_failure_loop_block_threshold=0),
        tool_handler=build_tool_handler(_registry([]), ToolContext()),
    )
    watchdog = ProgressWatchdog(repeated_tool_error_threshold=2)
    call = ToolCall(tool_use_id="w1", tool_name="count_things", arguments={"n": "many"})

    decisions = []
    for iteration in (1, 2):
        result = await agent._execute_tool(call)
        assert result.is_error is True
        assert json.loads(result.content)["error_class"] == "ToolArgumentsRejected"
        decisions.append(
            watchdog.observe(
                ProgressObservation(
                    iteration=iteration,
                    tool_error_signature=f"{result.tool_name}:{result.content[:160]}",
                    tool_calls=(
                        tool_call_signature(
                            result.tool_name,
                            call.arguments,
                            result.content,
                            is_error=True,
                        ),
                    ),
                )
            )
        )

    assert agent._tool_failure_loop_counts[("count_things", _args_hash(call.arguments))] == 2
    assert [decision.reason for decision in decisions] == ["no_signal", "repeated_tool_error"]


def _args_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
