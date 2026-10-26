"""``POST /api/v1/tools/invoke`` (#3695)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, GatewayConfig, ToolsConfig, ToolsInvokeConfig
from agentos.gateway.tools_invoke import HARD_DENY_TOOLS, IdempotencyCache, is_hard_denied
from agentos.tools.registry import ToolRegistry
from agentos.tools.types import ToolSpec, current_tool_context

TOKEN = "test-token"


def _registry(calls: list[dict[str, Any]]) -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup(query: str = "", action: str | None = None) -> str:
        ctx = current_tool_context.get()
        calls.append(
            {
                "query": query,
                "action": action,
                "interaction_mode": str(ctx.interaction_mode) if ctx else None,
                "elevated": ctx.elevated if ctx else "missing",
                "session_key": ctx.session_key if ctx else None,
            }
        )
        return json.dumps({"answer": f"found {query}"})

    async def needs_human(**_: Any) -> str:
        return json.dumps(
            {"status": "approval_pending", "approval_id": "abc123", "message": "Approve it."}
        )

    async def boom(**_: Any) -> str:
        raise RuntimeError("secret detail sk-should-not-leak")

    async def never(**_: Any) -> str:
        raise AssertionError("must not run")

    registry.register(
        ToolSpec(
            name="lookup",
            description="read-only lookup",
            parameters={"query": {"type": "string"}, "action": {"type": "string"}},
            read_only=True,
        ),
        lookup,
    )
    registry.register(
        ToolSpec(name="needs_human", description="", parameters={}, requires_approval=True),
        needs_human,
    )
    registry.register(ToolSpec(name="boom", description="", parameters={}), boom)
    for name in ("exec_command", "write_file", "sessions_spawn", "cron", "gateway"):
        registry.register(ToolSpec(name=name, description="", parameters={}), never)
    registry.register(ToolSpec(name="trading_swap", description="", parameters={}), never)
    return registry


def _client(
    registry: ToolRegistry,
    *,
    tools: ToolsConfig | None = None,
    auth: bool = True,
) -> TestClient:
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token=TOKEN),
        tools=tools or ToolsConfig(),
    )
    headers = {"authorization": f"Bearer {TOKEN}"} if auth else {}
    return TestClient(
        create_gateway_app(config, tool_registry=registry),
        base_url="http://localhost",
        headers=headers,
    )


def _invoke(client: TestClient, body: dict[str, Any], **headers: str) -> Any:
    return client.post("/api/v1/tools/invoke", json=body, headers=headers)


class TestInvoke:
    def test_allowed_tool_executes(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(_client(_registry(calls)), {"tool": "lookup", "args": {"query": "agentos"}})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["result"] == {"answer": "found agentos"}
        # Unattended and sandboxed, exactly like a background run.
        assert calls == [
            {
                "query": "agentos",
                "action": None,
                "interaction_mode": "unattended",
                "elevated": None,
                "session_key": None,
            }
        ]

    def test_action_and_session_key_reach_the_tool(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(
            _client(_registry(calls)),
            {"tool": "lookup", "action": "list", "session_key": "agent:main:webchat:x"},
        )
        assert resp.status_code == 200
        assert calls[0]["action"] == "list"
        assert calls[0]["session_key"] == "agent:main:webchat:x"

    @pytest.mark.parametrize(
        "tool", ["exec_command", "write_file", "sessions_spawn", "cron", "gateway", "trading_swap"]
    )
    def test_hard_denied_tool_is_404_like_an_unknown_one(self, tool: str) -> None:
        client = _client(_registry([]))
        denied = _invoke(client, {"tool": tool})
        unknown = _invoke(client, {"tool": "no_such_tool"})
        assert denied.status_code == unknown.status_code == 404
        assert denied.json() == unknown.json()

    def test_hard_deny_holds_even_when_config_allows_it(self) -> None:
        tools = ToolsConfig(allow=["exec_command", "lookup"], also_allow=["exec_command"])
        assert (
            _invoke(_client(_registry([]), tools=tools), {"tool": "exec_command"}).status_code
            == 404
        )

    def test_invoke_allow_lifts_one_tool(self) -> None:
        tools = ToolsConfig(invoke=ToolsInvokeConfig(allow=["write_file"]))
        client = _client(_registry([]), tools=tools)
        dry = _invoke(client, {"tool": "write_file", "dry_run": True})
        assert dry.status_code == 200
        assert dry.json()["allowed"] is True
        assert dry.json()["policy"]["invokeAllowOverride"] is True
        assert _invoke(client, {"tool": "exec_command"}).status_code == 404

    def test_policy_denied_tool_is_403(self) -> None:
        tools = ToolsConfig(deny=["lookup"])
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "POLICY_DENIED"

    def test_profile_allowlist_applies(self) -> None:
        tools = ToolsConfig(profile="minimal")
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup"})
        assert resp.status_code == 403

    def test_dry_run_returns_the_policy_and_runs_nothing(self) -> None:
        calls: list[dict[str, Any]] = []
        resp = _invoke(_client(_registry(calls)), {"tool": "lookup", "dry_run": True})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["dryRun"] is True
        assert payload["allowed"] is True
        assert payload["policy"]["readOnly"] is True
        assert payload["policy"]["interactionMode"] == "unattended"
        assert payload["policy"]["sandboxed"] is True
        assert "result" not in payload
        assert calls == []

    def test_dry_run_reports_a_policy_refusal(self) -> None:
        tools = ToolsConfig(deny=["lookup"])
        resp = _invoke(_client(_registry([]), tools=tools), {"tool": "lookup", "dryRun": True})
        assert resp.status_code == 200
        assert resp.json()["allowed"] is False
        assert resp.json()["errorClass"] == "PolicyDenied"

    def test_approval_requirement_is_403_with_the_approval_id(self) -> None:
        resp = _invoke(_client(_registry([])), {"tool": "needs_human"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "APPROVAL_REQUIRED"
        assert resp.json()["approval_id"] == "abc123"

    def test_tool_failure_is_reported_without_internals(self) -> None:
        resp = _invoke(_client(_registry([])), {"tool": "boom"})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is False
        assert payload["isError"] is True
        assert "sk-should-not-leak" not in resp.text

    def test_bearer_auth_is_required(self) -> None:
        resp = _invoke(_client(_registry([]), auth=False), {"tool": "lookup"})
        assert resp.status_code == 401

    def test_an_agent_bound_connection_is_refused(self) -> None:
        from agentos.gateway import agent_surface

        surface = agent_surface.get_agent_surface()
        token = surface.mint_token("agent:main:main", "main")
        resp = _invoke(
            _client(_registry([])), {"tool": "lookup"}, **{"x-agentos-agent-token": token}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "body",
        [{}, {"tool": ""}, {"tool": "lookup", "args": []}, {"tool": "lookup", "session_key": ""}],
    )
    def test_invalid_bodies(self, body: dict[str, Any]) -> None:
        assert _invoke(_client(_registry([])), body).status_code == 400


class TestIdempotency:
    def test_replay_returns_the_first_response(self) -> None:
        calls: list[dict[str, Any]] = []
        client = _client(_registry(calls))
        body = {"tool": "lookup", "args": {"query": "q"}, "idempotency_key": "k-1"}
        first = _invoke(client, body)
        second = _invoke(client, body)
        assert first.status_code == second.status_code == 200
        assert second.json()["idempotentReplay"] is True
        assert second.json()["result"] == first.json()["result"]
        assert len(calls) == 1

    def test_reusing_a_key_for_another_request_conflicts(self) -> None:
        client = _client(_registry([]))
        _invoke(client, {"tool": "lookup", "args": {"query": "a"}, "idempotency_key": "k-2"})
        conflict = _invoke(
            client, {"tool": "lookup", "args": {"query": "b"}, "idempotency_key": "k-2"}
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "REQUEST_CONFLICT"

    def test_entries_expire(self) -> None:
        now = [0.0]
        cache = IdempotencyCache(ttl_seconds=10, clock=lambda: now[0])

        async def scenario() -> list[str]:
            verdicts = []
            verdict, future = cache.claim("k", "f1")
            future.set_result((200, {}))
            verdicts.append(verdict)
            verdicts.append(cache.claim("k", "f2")[0])
            now[0] = 11.0
            verdicts.append(cache.claim("k", "f2")[0])
            return verdicts

        assert asyncio.run(scenario()) == ["new", "conflict", "new"]


def test_hard_deny_list_covers_the_documented_classes() -> None:
    for tool in (
        "exec",
        "exec_command",
        "bash",
        "execute_code",
        "write_file",
        "edit_file",
        "apply_patch",
        "sessions_spawn",
        "sessions_send",
        "cron",
        "gateway",
    ):
        assert tool in HARD_DENY_TOOLS
    assert is_hard_denied("trading_buy")
    assert not is_hard_denied("web_search")
    assert not is_hard_denied("exec_command", ["exec_command"])
