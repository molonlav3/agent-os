"""Lifecycle hooks and inbound webhooks (#3697)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agentos.gateway.config import (
    AuthConfig,
    GatewayConfig,
    HookRouteConfig,
    HooksConfig,
    LifecycleHooksConfig,
)
from agentos.gateway.inbound_hooks import HookGuards, register_inbound_hook_routes, render_template
from agentos.gateway.lifecycle_hooks import (
    PRE_RESTART,
    SHUTDOWN,
    STARTUP,
    LifecycleEvent,
    clear_lifecycle_hooks,
    register_lifecycle_hook,
    run_lifecycle_hooks,
    run_shutdown_hooks_once,
)

HOOK_TOKEN = "hook-secret"
GATEWAY_TOKEN = "gateway-secret"

#: Configured commands run through ``/bin/sh``; Windows reports them as failed
#: without starting them (``test_commands_are_not_started_without_a_posix_shell``).
posix_shell = pytest.mark.skipif(
    sys.platform == "win32", reason="lifecycle hook commands need /bin/sh"
)


@pytest.fixture(autouse=True)
def _clean_hooks() -> Any:
    clear_lifecycle_hooks()
    yield
    clear_lifecycle_hooks()


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    @pytest.mark.asyncio
    async def test_sync_and_async_hooks_run_with_the_event(self) -> None:
        seen: list[LifecycleEvent] = []

        async def flush(event: LifecycleEvent) -> None:
            seen.append(event)

        register_lifecycle_hook(SHUTDOWN, flush, name="flush")
        register_lifecycle_hook(SHUTDOWN, seen.append, name="sync")
        report = await run_lifecycle_hooks(SHUTDOWN, reason="sigterm")
        assert sorted(report.ran) == ["flush", "sync"]
        assert [e.reason for e in seen] == ["sigterm", "sigterm"]
        assert {e.name for e in seen} == {SHUTDOWN}

    @pytest.mark.asyncio
    async def test_budget_cancels_a_slow_hook_and_never_raises(self) -> None:
        cancelled = asyncio.Event()

        async def slow(_event: LifecycleEvent) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def broken(_event: LifecycleEvent) -> None:
            raise RuntimeError("boom")

        register_lifecycle_hook(PRE_RESTART, slow, name="slow")
        register_lifecycle_hook(PRE_RESTART, broken, name="broken")
        report = await run_lifecycle_hooks(PRE_RESTART, budget=0.05)
        assert report.timed_out == ["slow"]
        assert report.failed == ["broken"]
        assert cancelled.is_set()

    def test_default_budgets(self) -> None:
        from agentos.gateway.lifecycle_hooks import BUDGETS

        assert BUDGETS[SHUTDOWN] == 5.0
        assert BUDGETS[PRE_RESTART] == 10.0

    def test_unknown_event_is_rejected_at_registration(self) -> None:
        with pytest.raises(ValueError):
            register_lifecycle_hook("session:start", lambda _e: None)

    def test_unregister(self) -> None:
        calls: list[str] = []
        undo = register_lifecycle_hook(STARTUP, lambda _e: calls.append("x"))
        undo()
        asyncio.run(run_lifecycle_hooks(STARTUP))
        assert calls == []

    @posix_shell
    @pytest.mark.asyncio
    async def test_configured_command_runs_without_gateway_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "out.txt"
        monkeypatch.setenv("AGENTOS_GATEWAY_TOKEN", "must-not-leak")
        config = GatewayConfig(
            hooks=HooksConfig(
                lifecycle=LifecycleHooksConfig(
                    shutdown=[
                        f'printf "%s|%s|%s" "$AGENTOS_LIFECYCLE_EVENT" '
                        f'"$AGENTOS_LIFECYCLE_REASON" "${{AGENTOS_GATEWAY_TOKEN:-none}}" > {out}'
                    ]
                )
            )
        )
        report = await run_lifecycle_hooks(SHUTDOWN, config=config, reason="stop")
        assert report.failed == [] and len(report.ran) == 1
        assert out.read_text() == "gateway:shutdown|stop|none"

    @posix_shell
    @pytest.mark.asyncio
    async def test_timeout_kills_the_whole_process_group_within_budget(
        self, tmp_path: Path
    ) -> None:
        import time

        pidfile = tmp_path / "child.pid"
        config = GatewayConfig(
            hooks=HooksConfig(
                lifecycle=LifecycleHooksConfig(shutdown=[f"sleep 30 & echo $! > {pidfile}; wait"])
            )
        )
        started = time.monotonic()
        report = await run_lifecycle_hooks(SHUTDOWN, config=config, budget=1.0)
        elapsed = time.monotonic() - started
        assert report.timed_out == [f"command:sleep 30 & echo $! > {pidfile}; wait"]
        assert elapsed < 1.0
        child = int(pidfile.read_text().strip())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:  # pragma: no cover - the orphan survived
            os.kill(child, 9)
            pytest.fail("the command's child process outlived the budget")

    @posix_shell
    @pytest.mark.asyncio
    async def test_failing_or_hanging_command_is_contained(self, tmp_path: Path) -> None:
        config = GatewayConfig(
            hooks=HooksConfig(lifecycle=LifecycleHooksConfig(pre_restart=["exit 3", "sleep 30"]))
        )
        report = await run_lifecycle_hooks(PRE_RESTART, config=config, budget=0.5)
        assert report.failed == ["command:exit 3"]
        assert report.timed_out == ["command:sleep 30"]

    @pytest.mark.asyncio
    async def test_commands_are_not_started_without_a_posix_shell(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway import lifecycle_hooks

        spawned: list[tuple[Any, ...]] = []

        async def no_spawn(*argv: Any, **_kwargs: Any) -> Any:
            spawned.append(argv)
            raise AssertionError("a command was started")

        monkeypatch.setattr(lifecycle_hooks, "_shell_commands_supported", lambda: False)
        monkeypatch.setattr(lifecycle_hooks.asyncio, "create_subprocess_exec", no_spawn)
        seen: list[str] = []
        register_lifecycle_hook(SHUTDOWN, lambda event: seen.append(event.name), name="flush")
        config = GatewayConfig(
            hooks=HooksConfig(lifecycle=LifecycleHooksConfig(shutdown=["echo hi"]))
        )
        with structlog.testing.capture_logs() as logs:
            report = await run_lifecycle_hooks(SHUTDOWN, config=config)
        assert spawned == []
        assert report.failed == ["command:echo hi"]
        assert report.ran == ["flush"] and seen == [SHUTDOWN]
        (failure,) = [e for e in logs if e["event"] == "gateway.lifecycle_hook_failed"]
        assert "not run on Windows" in failure["error"]

    def test_kill_falls_back_to_the_process_without_process_groups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agentos.gateway import lifecycle_hooks

        class _Process:
            pid = 4242
            killed = False

            def kill(self) -> None:
                self.killed = True

        process = _Process()
        monkeypatch.delattr(lifecycle_hooks.os, "killpg", raising=False)
        lifecycle_hooks._kill_group(process)  # noqa: SLF001
        assert process.killed

    def test_lifecycle_commands_are_file_only(self) -> None:
        from agentos.gateway.rpc_config import _assert_file_only_paths_unchanged

        source = {"hooks": {"lifecycle": {"shutdown": ["a"]}, "token": ""}}
        _assert_file_only_paths_unchanged(
            {"hooks": {"lifecycle": {"shutdown": []}, "token": ""}}, source
        )  # clearing is allowed
        with pytest.raises(ValueError, match="file-only"):
            _assert_file_only_paths_unchanged(
                {"hooks": {"lifecycle": {"shutdown": ["a", "curl evil"]}, "token": ""}}, source
            )
        with pytest.raises(ValueError, match="file-only"):
            _assert_file_only_paths_unchanged(
                {"hooks": {"lifecycle": {"shutdown": ["a"]}, "token": "new"}}, source
            )

    def test_sigterm_lifespan_shutdown_runs_the_hooks_once(self) -> None:
        """``agentos gateway stop`` sends SIGTERM: uvicorn runs the lifespan
        shutdown without GatewayServer.close(). The hooks still run, once."""
        from agentos.gateway.app import create_gateway_app

        seen: list[str] = []
        register_lifecycle_hook(SHUTDOWN, lambda e: seen.append(e.reason or ""))
        with TestClient(create_gateway_app(GatewayConfig())):
            pass
        asyncio.run(run_shutdown_hooks_once(None, "close"))
        assert seen == ["lifespan"]

    @pytest.mark.asyncio
    async def test_gateway_close_runs_shutdown_hooks(self) -> None:
        from agentos.gateway.boot import GatewayServer

        seen: list[str] = []
        register_lifecycle_hook(SHUTDOWN, lambda e: seen.append(e.reason or ""))
        server = GatewayServer(app=Starlette(), config=GatewayConfig())
        await server.close("unit-test")
        assert seen == ["unit-test"]


# ---------------------------------------------------------------------------
# Inbound webhooks
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str, dict[str, Any]]] = []
        self.wakes: list[str] = []

    async def dispatch(self, session_key: str, message: str, provenance: dict[str, Any]) -> Any:
        self.messages.append((session_key, message, provenance))
        return {"ok": True, "runId": f"run-{len(self.messages)}"}

    def wake(self, reason: str) -> bool:
        self.wakes.append(reason)
        return True


def _hooks_client(
    *,
    hooks: HooksConfig | None = None,
    recorder: _Recorder | None = None,
    guards: HookGuards | None = None,
    wake: bool = True,
    auth_guard: Any = None,
) -> tuple[TestClient, _Recorder]:
    recorder = recorder or _Recorder()
    config = GatewayConfig(
        auth=AuthConfig(mode="token", token=GATEWAY_TOKEN),
        hooks=hooks or HooksConfig(token=HOOK_TOKEN),
    )
    app = Starlette()
    register_inbound_hook_routes(
        app,
        config=config,
        dispatch_message=recorder.dispatch,
        wake=recorder.wake if wake else None,
        guards=guards,
        auth_guard=auth_guard,
    )
    return TestClient(app), recorder


def _auth(token: str = HOOK_TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


class TestInboundHooks:
    def test_agent_hook_enqueues_into_the_hook_session(self) -> None:
        client, recorder = _hooks_client()
        resp = client.post("/api/v1/hooks/agent", json={"message": "CI failed"}, headers=_auth())
        assert resp.status_code == 202, resp.text
        assert resp.json()["sessionKey"] == "agent:main:hook:agent"
        assert recorder.messages == [
            (
                "agent:main:hook:agent",
                "CI failed",
                {"kind": "external_user", "source": "webhook", "hook": "agent"},
            )
        ]

    def test_hooks_token_header_alternative(self) -> None:
        client, _ = _hooks_client()
        resp = client.post(
            "/api/v1/hooks/agent",
            json={"message": "hi"},
            headers={"x-agentos-hook-token": HOOK_TOKEN},
        )
        assert resp.status_code == 202

    def test_the_gateway_token_is_refused(self) -> None:
        client, recorder = _hooks_client()
        resp = client.post(
            "/api/v1/hooks/agent", json={"message": "hi"}, headers=_auth(GATEWAY_TOKEN)
        )
        assert resp.status_code == 401
        assert "gateway token" in resp.json()["error"]
        assert recorder.messages == []

    def test_wrong_or_missing_token(self) -> None:
        client, _ = _hooks_client()
        assert client.post("/api/v1/hooks/agent", json={"message": "x"}).status_code == 401
        assert (
            client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth("no"))
        ).status_code == 401

    def test_routes_are_absent_without_a_hooks_token(self) -> None:
        client, _ = _hooks_client(hooks=HooksConfig())
        assert client.post("/api/v1/hooks/agent", json={}, headers=_auth()).status_code == 404

    def test_a_hooks_token_equal_to_the_gateway_token_disables_hooks(self) -> None:
        client, _ = _hooks_client(hooks=HooksConfig(token=GATEWAY_TOKEN))
        resp = client.post(
            "/api/v1/hooks/agent", json={"message": "x"}, headers=_auth(GATEWAY_TOKEN)
        )
        assert resp.status_code == 404

    def test_caller_session_key_must_stay_in_the_hook_namespace(self) -> None:
        client, recorder = _hooks_client(
            hooks=HooksConfig(token=HOOK_TOKEN, max_sessions_per_hook=2)
        )
        ok = client.post(
            "/api/v1/hooks/agent",
            json={"message": "x", "session_key": "agent:main:hook:ci"},
            headers=_auth(),
        )
        assert ok.status_code == 202
        refused = client.post(
            "/api/v1/hooks/agent",
            json={"message": "x", "session_key": "agent:main:telegram:direct:1"},
            headers=_auth(),
        )
        assert refused.status_code == 403
        assert [m[0] for m in recorder.messages] == ["agent:main:hook:ci"]

    def test_fan_out_is_capped_at_one_session_by_default(self) -> None:
        client, _ = _hooks_client()
        first = client.post(
            "/api/v1/hooks/agent",
            json={"message": "x", "session_key": "agent:main:hook:a"},
            headers=_auth(),
        )
        again = client.post(
            "/api/v1/hooks/agent",
            json={"message": "y", "session_key": "agent:main:hook:a"},
            headers=_auth(),
        )
        other = client.post(
            "/api/v1/hooks/agent",
            json={"message": "z", "session_key": "agent:main:hook:b"},
            headers=_auth(),
        )
        assert (first.status_code, again.status_code, other.status_code) == (202, 202, 409)
        assert other.json()["code"] == "INVALID_REQUEST"
        assert other.json()["details"] == {"code": "FANOUT_LIMIT", "limit": 1}

    def test_idempotency_key_replays_the_first_response(self) -> None:
        client, recorder = _hooks_client()
        body = {"message": "deploy done", "idempotency_key": "evt-1"}
        first = client.post("/api/v1/hooks/agent", json=body, headers=_auth())
        second = client.post("/api/v1/hooks/agent", json=body, headers=_auth())
        header = client.post(
            "/api/v1/hooks/agent",
            json={"message": "deploy done"},
            headers={**_auth(), "Idempotency-Key": "evt-1"},
        )
        assert first.status_code == second.status_code == header.status_code == 202
        assert second.json()["duplicate"] is True
        assert len(recorder.messages) == 1

    def test_idempotency_window_is_five_minutes(self) -> None:
        now = [0.0]
        guards = HookGuards(clock=lambda: now[0])
        client, recorder = _hooks_client(guards=guards)
        body = {"message": "x", "idempotency_key": "evt-2"}
        client.post("/api/v1/hooks/agent", json=body, headers=_auth())
        now[0] = 301.0
        client.post("/api/v1/hooks/agent", json=body, headers=_auth())
        assert len(recorder.messages) == 2

    def test_throttled_with_429(self) -> None:
        client, _ = _hooks_client(hooks=HooksConfig(token=HOOK_TOKEN, rate_limit_per_minute=2))
        codes = [
            client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth()).status_code
            for _ in range(3)
        ]
        assert codes == [202, 202, 429]
        throttled = client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth())
        assert int(throttled.headers["retry-after"]) >= 1

    def test_wake(self) -> None:
        client, recorder = _hooks_client()
        resp = client.post("/api/v1/hooks/wake", json={"reason": "nightly"}, headers=_auth())
        assert resp.status_code == 202
        assert recorder.wakes == ["hook:nightly"]
        unwired, _ = _hooks_client(wake=False)
        assert unwired.post("/api/v1/hooks/wake", json={}, headers=_auth()).status_code == 503

    def test_named_route_renders_its_template(self) -> None:
        hooks = HooksConfig(
            token=HOOK_TOKEN,
            routes={
                "github": HookRouteConfig(
                    agent="ops",
                    template="PR {{ payload.pull_request.title }} by {{ sender.login }}: "
                    "{{ payload.labels.0 }} {{ payload.missing.key }}",
                )
            },
        )
        client, recorder = _hooks_client(hooks=hooks)
        resp = client.post(
            "/api/v1/hooks/github",
            json={
                "pull_request": {"title": "Fix it"},
                "sender": {"login": "octo"},
                "labels": ["bug"],
            },
            headers=_auth(),
        )
        assert resp.status_code == 202
        session_key, message, provenance = recorder.messages[0]
        assert session_key == "agent:ops:hook:github"
        assert message == "PR Fix it by octo: bug "
        assert provenance["hook"] == "github"

    def test_named_route_session_key_comes_from_the_operator(self) -> None:
        hooks = HooksConfig(
            token=HOOK_TOKEN,
            routes={"alerts": HookRouteConfig(session_key="agent:main:telegram:direct:42")},
        )
        client, recorder = _hooks_client(hooks=hooks)
        client.post("/api/v1/hooks/alerts", json={"message": "disk full"}, headers=_auth())
        assert recorder.messages[0][:2] == ("agent:main:telegram:direct:42", "disk full")

    def test_unknown_route_is_404_only_after_auth(self) -> None:
        client, _ = _hooks_client()
        assert client.post("/api/v1/hooks/nope", json={}).status_code == 401
        assert client.post("/api/v1/hooks/nope", json={}, headers=_auth()).status_code == 404

    def test_route_names_are_validated(self) -> None:
        with pytest.raises(ValueError):
            HooksConfig(routes={"wake": HookRouteConfig()})
        with pytest.raises(ValueError):
            HooksConfig(routes={"Bad Name": HookRouteConfig()})

    def test_body_limits(self) -> None:
        client, _ = _hooks_client()
        too_big = client.post("/api/v1/hooks/agent", content=b"x" * (300 * 1024), headers=_auth())
        assert too_big.status_code == 413
        assert too_big.json()["code"] == "INVALID_REQUEST"
        assert too_big.json()["details"]["code"] == "PAYLOAD_TOO_LARGE"
        not_json = client.post("/api/v1/hooks/agent", content=b"{nope", headers=_auth())
        assert not_json.status_code == 400


def test_render_template_is_not_code() -> None:
    assert render_template("{{ __class__ }} {{ a.b }}", {"a": {"b": [1, 2]}}) == " [1,2]"


def test_hooks_routes_bypass_the_gateway_token_gate_only_for_their_own_check() -> None:
    """Through the real app: token auth on, the hooks bearer still works."""
    from agentos.gateway.app import create_gateway_app

    config = GatewayConfig(
        auth=AuthConfig(mode="token", token=GATEWAY_TOKEN),
        hooks=HooksConfig(token=HOOK_TOKEN),
    )
    client = TestClient(create_gateway_app(config), base_url="http://localhost")
    # No session manager in this app: chat.send instant-accepts.
    resp = client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth())
    assert resp.status_code == 202, resp.text
    assert client.post("/api/v1/hooks/agent", json={"message": "x"}).status_code == 401
    # The gateway token still guards the rest of the API.
    assert client.get("/api/sessions", headers=_auth()).status_code == 401


class TestHookAuthHardening:
    """#3697 follow-up: a hostile bearer must never produce a traceback."""

    @pytest.mark.parametrize(
        "raw", [b"Bearer \xc3\xa9", b"Bearer \xff\xfe", b"Bearer " + b"x" * 5000]
    )
    def test_non_ascii_or_huge_token_is_a_plain_401(self, raw: bytes) -> None:
        from structlog.testing import capture_logs

        client, recorder = _hooks_client()
        with capture_logs() as logs:
            resp = client.post(
                "/api/v1/hooks/agent",
                json={"message": "x"},
                headers={"authorization": raw},  # raw bytes, as curl sends them
            )
        assert resp.status_code == 401
        assert recorder.messages == []
        dumped = repr(logs)
        assert HOOK_TOKEN not in dumped and GATEWAY_TOKEN not in dumped

    def test_non_ascii_header_token(self) -> None:
        client, _ = _hooks_client()
        resp = client.post(
            "/api/v1/hooks/agent",
            json={"message": "x"},
            headers={"x-agentos-hook-token": b"\xc3\xa9"},
        )
        assert resp.status_code == 401

    def test_unexpected_failure_is_contained_without_secrets(self) -> None:
        from structlog.testing import capture_logs

        class _Exploding(_Recorder):
            async def dispatch(self, *_a: Any) -> Any:
                raise RuntimeError(f"boom with {HOOK_TOKEN}")

        client, _ = _hooks_client(recorder=_Exploding())
        with capture_logs() as logs:
            resp = client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth())
        assert resp.status_code == 500
        assert resp.json()["code"] == "INTERNAL_ERROR"
        failures = [e for e in logs if e["event"] == "gateway.hook_failed"]
        assert failures and "exc_info" not in failures[0]

    def test_idempotency_key_reused_for_another_body_conflicts(self) -> None:
        client, recorder = _hooks_client()
        first = client.post(
            "/api/v1/hooks/agent", json={"message": "a", "idempotency_key": "k"}, headers=_auth()
        )
        second = client.post(
            "/api/v1/hooks/agent", json={"message": "b", "idempotency_key": "k"}, headers=_auth()
        )
        assert (first.status_code, second.status_code) == (202, 409)
        assert second.json()["code"] == "INVALID_REQUEST"
        assert second.json()["details"] == {"code": "REQUEST_CONFLICT"}
        assert len(recorder.messages) == 1

    def test_fan_out_cap_is_a_rolling_window(self) -> None:
        from agentos.gateway.inbound_hooks import FANOUT_WINDOW_SECONDS

        now = [0.0]
        guards = HookGuards(clock=lambda: now[0])
        client, _ = _hooks_client(guards=guards)

        def post(key: str) -> int:
            return client.post(
                "/api/v1/hooks/agent",
                json={"message": "x", "session_key": key},
                headers=_auth(),
            ).status_code

        assert post("agent:main:hook:a") == 202
        assert post("agent:main:hook:b") == 409
        now[0] = FANOUT_WINDOW_SECONDS + 1
        assert post("agent:main:hook:b") == 202

    def test_wrong_hooks_tokens_lock_the_address_out_of_the_hooks_only(self) -> None:
        """Guessing the hooks token is throttled like the gateway token, in a
        budget of its own: a locked-out sender is refused even with the right
        token, and the gateway token's budget is untouched."""
        from agentos.gateway.auth_rate_limit import TOKEN_SCOPE, AuthGuard

        guard = AuthGuard.from_config(
            GatewayConfig(auth=AuthConfig(mode="token", token=GATEWAY_TOKEN))
        )
        client, recorder = _hooks_client(auth_guard=guard)
        for _ in range(10):
            wrong = client.post(
                "/api/v1/hooks/agent", json={"message": "x"}, headers=_auth("guess")
            )
            assert wrong.status_code == 401
        locked = client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth())
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0
        assert locked.json()["code"] == "RATE_LIMITED"
        assert locked.json()["details"]["code"] == "AUTH_RATE_LIMITED"
        assert recorder.messages == []
        assert guard.limiter.check("testclient", TOKEN_SCOPE).allowed

    def test_the_gateway_token_on_a_hook_is_charged_too(self) -> None:
        """Its distinct refusal must not be an unthrottled gateway-token oracle."""
        from agentos.gateway.auth_rate_limit import AuthGuard

        guard = AuthGuard.from_config(
            GatewayConfig(auth=AuthConfig(mode="token", token=GATEWAY_TOKEN))
        )
        client, _ = _hooks_client(auth_guard=guard)
        for _ in range(10):
            refused = client.post(
                "/api/v1/hooks/agent", json={"message": "x"}, headers=_auth(GATEWAY_TOKEN)
            )
            assert refused.status_code == 401
            assert "hooks token" in refused.json()["error"]
        again = client.post(
            "/api/v1/hooks/agent", json={"message": "x"}, headers=_auth(GATEWAY_TOKEN)
        )
        assert again.status_code == 429

    def test_a_missing_token_is_never_charged(self) -> None:
        from agentos.gateway.auth_rate_limit import AuthGuard

        guard = AuthGuard.from_config(
            GatewayConfig(auth=AuthConfig(mode="token", token=GATEWAY_TOKEN))
        )
        client, _ = _hooks_client(auth_guard=guard)
        for _ in range(12):
            assert client.post("/api/v1/hooks/agent", json={"message": "x"}).status_code == 401
        assert (
            client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth()).status_code
            == 202
        )

    def test_the_app_wires_its_failed_auth_limiter_into_the_hooks(self) -> None:
        from agentos.gateway.app import create_gateway_app

        config = GatewayConfig(
            auth=AuthConfig(mode="token", token=GATEWAY_TOKEN),
            hooks=HooksConfig(token=HOOK_TOKEN),
        )
        client = TestClient(create_gateway_app(config), base_url="http://localhost")
        for _ in range(10):
            client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth("guess"))
        resp = client.post("/api/v1/hooks/agent", json={"message": "x"}, headers=_auth())
        assert resp.status_code == 429
        # The gateway token's own budget is separate.
        assert client.get("/api/sessions", headers=_auth(GATEWAY_TOKEN)).status_code != 429


class TestHookOrderingOnRestart:
    @pytest.mark.asyncio
    async def test_pre_restart_runs_after_the_drain_then_shutdown(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from agentos.gateway.boot import GatewayServer

        order: list[str] = []
        runtime = MagicMock()
        runtime.shutdown = AsyncMock(side_effect=lambda **_k: order.append("drain"))
        services = MagicMock()
        services.task_runtime = runtime
        services.close = AsyncMock()
        register_lifecycle_hook(PRE_RESTART, lambda _e: order.append("pre-restart"))
        register_lifecycle_hook(SHUTDOWN, lambda _e: order.append("shutdown"))
        server = GatewayServer(app=Starlette(), config=GatewayConfig())
        server._services = services
        await server.close("restart:safe")
        assert order == ["drain", "pre-restart", "shutdown"]
