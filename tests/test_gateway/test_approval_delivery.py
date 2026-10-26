"""Approval delivery: Web Push, channel custody and parent-session audience (#3694).

No network: the push transport is a recorder, the browser side of the
encryption is reproduced with ``cryptography``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from agentos.application import approval_delivery
from agentos.application.approval_queue import ApprovalQueue, PendingApproval
from agentos.gateway.access import ConnectionSurface
from agentos.gateway.approval_custody import channel_custody, parse_channel_session_key
from agentos.gateway.auth import AccessContext
from agentos.gateway.config import GatewayConfig, PushConfig
from agentos.gateway.rpc import RpcContext, get_dispatcher
from agentos.gateway.session_index import SessionIndex
from agentos.gateway.web_push import (
    VAPID_FILENAME,
    PushRequest,
    VapidKeys,
    WebPushService,
    b64url_decode,
    b64url_encode,
    decrypt_payload,
    encrypt_payload,
    set_web_push_service,
    validate_subscription,
    vapid_authorization,
)

ENDPOINT = "https://push.example.net/send/abc123"


@pytest.fixture(autouse=True)
def _reset_delivery() -> Any:
    approval_delivery.reset()
    set_web_push_service(None)
    yield
    approval_delivery.reset()
    set_web_push_service(None)


class _Browser:
    """The receiving side of a push subscription."""

    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        public = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.p256dh = b64url_encode(public)
        self.auth = b64url_encode(os.urandom(16))

    def subscription_json(self, endpoint: str = ENDPOINT) -> dict[str, Any]:
        return {"endpoint": endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}

    def read(self, request: PushRequest) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(
            decrypt_payload(request.body, self.private_key, self.auth)
        )
        return result


class _Transport:
    def __init__(self, status: int = 201) -> None:
        self.status = status
        self.requests: list[PushRequest] = []

    async def __call__(self, request: PushRequest) -> int | None:
        self.requests.append(request)
        return self.status


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _service(
    tmp_path: Path, transport: _Transport, *, config: GatewayConfig | None = None, queue: Any = None
) -> WebPushService:
    service = WebPushService.from_state_dir(
        tmp_path, config or GatewayConfig(), transport=transport
    )
    if queue is not None:
        service._lifespan_end = queue.lifespan_end
    approval_delivery.register_listener(service)
    return service


def _queue(tmp_path: Path, timeout: float = 300.0) -> ApprovalQueue:
    return ApprovalQueue(default_timeout=timeout, db_path=str(tmp_path / "approvals.sqlite"))


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_rfc8291_round_trip_and_header(self) -> None:
        browser = _Browser()
        body = encrypt_payload(b'{"hello":"world"}', browser.p256dh, browser.auth)
        assert int.from_bytes(body[16:20], "big") == 4096
        assert body[20] == 65
        assert body[21] == 0x04  # the sender's ephemeral uncompressed point
        assert decrypt_payload(body, browser.private_key, browser.auth) == b'{"hello":"world"}'

    def test_each_message_uses_a_fresh_key_and_salt(self) -> None:
        browser = _Browser()
        first = encrypt_payload(b"x", browser.p256dh, browser.auth)
        second = encrypt_payload(b"x", browser.p256dh, browser.auth)
        assert first[:16] != second[:16]
        assert first[21:86] != second[21:86]

    def test_oversized_payload_is_refused(self) -> None:
        browser = _Browser()
        with pytest.raises(ValueError):
            encrypt_payload(b"x" * 5000, browser.p256dh, browser.auth)

    def test_vapid_jwt_is_a_valid_es256_signature(self) -> None:
        keys = VapidKeys.generate()
        header = vapid_authorization(keys, ENDPOINT, "mailto:ops@example.com", now=1_000_000)
        token = header.split("t=", 1)[1].split(",", 1)[0]
        k = header.split("k=", 1)[1]
        assert k == keys.public_key_b64
        head, claims, signature = token.split(".")
        assert json.loads(b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}
        payload = json.loads(b64url_decode(claims))
        assert payload == {
            "aud": "https://push.example.net",
            "exp": 1_000_000 + 12 * 3600,
            "sub": "mailto:ops@example.com",
        }
        raw = b64url_decode(signature)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        keys.private_key.public_key().verify(
            der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())
        )

    def test_vapid_keys_are_generated_once_with_0600(self, tmp_path: Path) -> None:
        first = VapidKeys.load_or_create(tmp_path)
        path = tmp_path / VAPID_FILENAME
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        os.chmod(path, 0o644)
        second = VapidKeys.load_or_create(tmp_path)
        assert second.public_key_b64 == first.public_key_b64
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert len(b64url_decode(first.public_key_b64)) == 65


# ---------------------------------------------------------------------------
# Delivery from the queue write path
# ---------------------------------------------------------------------------


class TestDelivery:
    @pytest.mark.asyncio
    async def test_request_then_resolution_share_tag_and_topic(self, tmp_path: Path) -> None:
        browser = _Browser()
        transport = _Transport()
        queue = _queue(tmp_path)
        service = _service(tmp_path, transport, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))

        approval_id = queue.request(
            "exec",
            {"sessionKey": "agent:main:webchat:default", "toolName": "exec", "command": "ls -la"},
        )
        await service.drain()
        assert len(transport.requests) == 1
        request = transport.requests[0]
        assert request.endpoint == ENDPOINT
        assert 295 <= int(request.headers["TTL"]) <= 300
        assert request.headers["Urgency"] == "high"
        assert request.headers["Topic"] == hashlib.sha256(approval_id.encode()).hexdigest()[:32]
        assert request.headers["Content-Encoding"] == "aes128gcm"
        assert request.headers["Authorization"].startswith("vapid t=")
        payload = browser.read(request)
        assert payload == {
            "type": "approval.requested",
            "id": approval_id,
            "namespace": "exec",
            "title": "Approval needed",
            "summary": "exec: ls -la",
            "url": f"/control/approvals?approval={approval_id}",
            "tag": f"agentos-approval-{approval_id}",
            "state": "pending",
            "sessionKey": "agent:main:webchat:default",
        }

        queue.resolve(approval_id, True)
        await service.drain()
        assert len(transport.requests) == 2
        resolved = transport.requests[1]
        assert resolved.headers["Topic"] == request.headers["Topic"]
        assert resolved.headers["Urgency"] == "normal"
        notice = browser.read(resolved)
        assert notice["type"] == "approval.resolved"
        assert notice["state"] == "resolved"
        assert notice["tag"] == payload["tag"]

    @pytest.mark.asyncio
    async def test_ttl_follows_the_approval_lifespan(self, tmp_path: Path) -> None:
        browser = _Browser()
        transport = _Transport()
        queue = _queue(tmp_path)
        service = _service(tmp_path, transport, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))
        queue.request("acp", {"sessionKey": "agent:main:acp:r1"}, ttl=900)
        await service.drain()
        assert 895 <= int(transport.requests[0].headers["TTL"]) <= 900

    @pytest.mark.asyncio
    async def test_expiry_sweep_sends_the_no_longer_pending_notice(self, tmp_path: Path) -> None:
        browser = _Browser()
        transport = _Transport()
        queue = _queue(tmp_path, timeout=0.01)
        service = _service(tmp_path, transport, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))
        approval_id = queue.request("exec", {"sessionKey": "s"})
        await service.drain()
        import time

        time.sleep(0.02)
        queue.list_pending()  # runs the sweep
        await service.drain()
        assert [browser.read(r)["type"] for r in transport.requests][-1] == "approval.resolved"
        assert browser.read(transport.requests[-1])["id"] == approval_id

    @pytest.mark.asyncio
    async def test_gone_subscription_is_dropped(self, tmp_path: Path) -> None:
        browser = _Browser()
        transport = _Transport(status=410)
        queue = _queue(tmp_path)
        service = _service(tmp_path, transport, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))
        queue.request("exec", {"sessionKey": "s"})
        await service.drain()
        assert service.store.all() == []

    @pytest.mark.asyncio
    async def test_disabled_push_sends_nothing(self, tmp_path: Path) -> None:
        browser = _Browser()
        transport = _Transport()
        queue = _queue(tmp_path)
        config = GatewayConfig(push=PushConfig(web_enabled=False))
        service = _service(tmp_path, transport, config=config, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))
        queue.request("exec", {"sessionKey": "s"})
        await service.drain()
        assert transport.requests == []

    @pytest.mark.asyncio
    async def test_cancelled_and_expired_prompts_are_replaced_with_their_decision(
        self, tmp_path: Path
    ) -> None:
        browser = _Browser()
        transport = _Transport()
        queue = _queue(tmp_path, timeout=0.05)
        service = _service(tmp_path, transport, queue=queue)
        service.store.upsert(validate_subscription(**browser.subscription_json()))
        stopped = queue.request("exec", {"sessionKey": "agent:main:webchat:a"}, ttl=60)
        queue.cancel_pending_for_session("agent:main:webchat:a")
        await service.drain()
        notice = browser.read(transport.requests[-1])
        assert (notice["id"], notice["state"], notice["decision"]) == (
            stopped,
            "resolved",
            "cancelled",
        )
        assert notice["title"] == "Approval cancelled: its run was stopped"

        aged = queue.request("exec", {"sessionKey": "agent:main:webchat:b"})
        await service.drain()
        import time

        time.sleep(0.06)
        queue.sweep()
        await service.drain()
        notice = browser.read(transport.requests[-1])
        assert (notice["id"], notice["decision"], notice["title"]) == (
            aged,
            "expired",
            "Approval expired",
        )

    def test_each_write_path_announces_once_to_consoles_and_delivery(self, tmp_path: Path) -> None:
        """The queue's write paths are the single source: every request and
        every resolution (decided, cancelled, expired, timed out) reaches the
        ``approval.*`` event listeners and the delivery listeners exactly once."""
        import time

        from agentos.application.approval_events import add_approval_listener

        events: list[tuple[str, str, str | None]] = []
        delivered: list[tuple[str, str, str | None]] = []

        class _Recorder:
            def on_requested(self, entry: PendingApproval) -> None:
                delivered.append(("requested", entry.approval_id, None))

            def on_resolved(self, entry: PendingApproval) -> None:
                delivered.append(("resolved", entry.approval_id, entry.decision))

        approval_delivery.register_listener(_Recorder())
        remove = add_approval_listener(
            lambda name, payload: events.append(
                (name.split(".")[1], payload["id"], payload.get("decision"))
            )
        )
        try:
            queue = _queue(tmp_path, timeout=0.05)
            decided = queue.request("exec", {"sessionKey": "agent:main:webchat:a"}, ttl=60)
            queue.resolve(decided, True)
            stopped = queue.request("exec", {"sessionKey": "agent:main:webchat:b"}, ttl=60)
            queue.cancel_pending_for_session("agent:main:webchat:b")
            timed = queue.request("exec", {"sessionKey": "agent:main:webchat:d"}, ttl=0.01)
            time.sleep(0.02)
            assert asyncio.run(queue.wait(timed, timeout=0.01)) is False
            swept = queue.request("exec", {"sessionKey": "agent:main:webchat:c"})
            time.sleep(0.06)
            queue.sweep()
            queue.sweep()  # a second sweep finds nothing new to announce
        finally:
            remove()
        expected = [
            ("requested", decided, None),
            ("resolved", decided, "approved"),
            ("requested", stopped, None),
            ("resolved", stopped, "cancelled"),
            ("requested", timed, None),
            ("resolved", timed, "expired"),
            ("requested", swept, None),
            ("resolved", swept, "expired"),
        ]
        assert events == expected
        assert delivered == expected

    def test_a_failing_listener_never_breaks_the_queue(self, tmp_path: Path) -> None:
        class _Broken:
            def on_requested(self, entry: PendingApproval) -> None:
                raise RuntimeError("boom")

            def on_resolved(self, entry: PendingApproval) -> None:
                raise RuntimeError("boom")

        approval_delivery.register_listener(_Broken())
        queue = _queue(tmp_path)
        approval_id = queue.request("exec", {"sessionKey": "s"})
        queue.resolve(approval_id, False)
        assert queue.get(approval_id).resolved is True


# ---------------------------------------------------------------------------
# Subscriptions RPC
# ---------------------------------------------------------------------------


def _ctx(config: GatewayConfig, *, agent: Any = None) -> RpcContext:
    return RpcContext(
        conn_id="conn-1",
        access=AccessContext(
            surface=ConnectionSurface.CONTROL,
            admitted=True,
            credential_verified=True,
            agent=agent,
        ),
        config=config,
    )


class TestSubscriptionRpc:
    @pytest.mark.asyncio
    async def test_key_subscribe_unsubscribe(self, tmp_path: Path) -> None:
        config = GatewayConfig(state_dir=str(tmp_path))
        dispatcher = get_dispatcher()
        key = await dispatcher.dispatch("1", "push.web.vapid_public_key", None, _ctx(config))
        assert key.ok, key.error
        assert key.payload["enabled"] is True
        assert len(b64url_decode(key.payload["publicKey"])) == 65

        browser = _Browser()
        sub = await dispatcher.dispatch(
            "2", "push.web.subscribe", browser.subscription_json(), _ctx(config)
        )
        assert sub.ok, sub.error
        unsub = await dispatcher.dispatch(
            "3", "push.web.unsubscribe", {"endpoint": ENDPOINT}, _ctx(config)
        )
        assert unsub.payload == {"ok": True, "removed": True}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://push.example.net/x",
            "https://127.0.0.1/x",
            "https://localhost/x",
            "https://10.0.0.5/x",
            "https://[::1]/x",
            "https://router.local/x",
        ],
    )
    async def test_endpoint_must_be_public_https(self, tmp_path: Path, endpoint: str) -> None:
        config = GatewayConfig(state_dir=str(tmp_path))
        res = await get_dispatcher().dispatch(
            "1", "push.web.subscribe", _Browser().subscription_json(endpoint), _ctx(config)
        )
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_keys_are_validated(self, tmp_path: Path) -> None:
        config = GatewayConfig(state_dir=str(tmp_path))
        bad = {
            "endpoint": ENDPOINT,
            "keys": {"p256dh": base64.urlsafe_b64encode(b"x" * 10).decode(), "auth": "AAAA"},
        }
        res = await get_dispatcher().dispatch("1", "push.web.subscribe", bad, _ctx(config))
        assert res.error.code == "INVALID_REQUEST"

    @pytest.mark.asyncio
    async def test_a_browser_console_is_unbound_and_may_subscribe(self, tmp_path: Path) -> None:
        """A console never presents an operator secret; refusing ``unbound``
        would lock every browser out of the toggle."""
        from agentos.gateway.agent_surface import AgentBinding

        config = GatewayConfig(state_dir=str(tmp_path))
        unbound = AgentBinding(via="unbound")
        res = await get_dispatcher().dispatch(
            "1", "push.web.subscribe", _Browser().subscription_json(), _ctx(config, agent=unbound)
        )
        assert res.ok, res.error

    @pytest.mark.asyncio
    async def test_an_agent_connection_cannot_subscribe(self, tmp_path: Path) -> None:
        from agentos.gateway.agent_surface import AgentBinding

        config = GatewayConfig(state_dir=str(tmp_path))
        agent = AgentBinding(via="token", agent_id="main", session_key="agent:main:main")
        res = await get_dispatcher().dispatch(
            "1", "push.web.subscribe", _Browser().subscription_json(), _ctx(config, agent=agent)
        )
        assert res.error.code == "INVALID_REQUEST"
        assert "agent" in res.error.message


# ---------------------------------------------------------------------------
# Parent-session audience
# ---------------------------------------------------------------------------


def _sessions_db(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "sessions.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (session_key TEXT PRIMARY KEY, parent_session_key TEXT, "
        "spawned_by TEXT, last_channel TEXT, last_to TEXT, last_account_id TEXT, "
        "delivery_context TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["key"],
                row.get("parent"),
                row.get("spawned_by"),
                row.get("channel"),
                row.get("to"),
                row.get("account"),
                json.dumps(row["context"]) if "context" in row else None,
            ),
        )
    conn.commit()
    conn.close()
    return path


class TestAudience:
    def test_child_request_reaches_every_ancestor(self, tmp_path: Path) -> None:
        index = SessionIndex(
            _sessions_db(
                tmp_path,
                [
                    {"key": "agent:main:acp:r2", "parent": "agent:main:subagent:s1"},
                    {"key": "agent:main:subagent:s1", "spawned_by": "agent:main:webchat:abc"},
                    {"key": "agent:main:webchat:abc"},
                ],
            )
        )
        approval_delivery.set_lineage_resolver(index.parents)
        queue = _queue(tmp_path)
        approval_id = queue.request("acp", {"sessionKey": "agent:main:acp:r2"})
        audience = (
            "agent:main:acp:r2",
            "agent:main:subagent:s1",
            "agent:main:webchat:abc",
        )
        assert queue.get(approval_id).audience_session_keys == audience
        listed = queue.list_pending()
        assert listed[0]["audience_session_keys"] == list(audience)
        assert queue.status(approval_id)["audience_session_keys"] == list(audience)
        # Persisted: a fresh queue on the same file reads it back.
        reopened = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
        assert reopened.get(approval_id).audience_session_keys == audience

    def test_approval_requested_event_carries_the_audience(self, tmp_path: Path) -> None:
        from agentos.application.approval_events import add_approval_listener

        index = SessionIndex(
            _sessions_db(
                tmp_path,
                [
                    {"key": "agent:main:subagent:s1", "spawned_by": "agent:main:webchat:abc"},
                    {"key": "agent:main:webchat:abc"},
                ],
            )
        )
        approval_delivery.set_lineage_resolver(index.parents)
        events: list[tuple[str, dict[str, Any]]] = []
        remove = add_approval_listener(lambda name, payload: events.append((name, payload)))
        try:
            _queue(tmp_path).request("exec", {"sessionKey": "agent:main:subagent:s1"})
        finally:
            remove()
        name, payload = events[0]
        audience = ["agent:main:subagent:s1", "agent:main:webchat:abc"]
        assert name == "approval.requested"
        assert payload["audience_session_keys"] == audience
        # The display item the console renders matches on the same field.
        assert payload["approval"]["audienceSessionKeys"] == audience

    def test_cycles_and_depth_are_bounded(self) -> None:
        approval_delivery.set_lineage_resolver(
            lambda key: {"a": ["b"], "b": ["a"]}.get(key, [f"{key}x"])
        )
        assert approval_delivery.audience_for("a") == ("a", "b")
        deep = approval_delivery.audience_for("c")
        assert len(deep) == approval_delivery.MAX_LINEAGE_DEPTH + 1

    def test_legacy_subagent_key_names_its_parent(self) -> None:
        assert approval_delivery.audience_for("subagent:agent:main:webchat:x") == (
            "subagent:agent:main:webchat:x",
            "agent:main:webchat:x",
        )

    def test_http_listing_carries_the_audience(self, tmp_path: Path, monkeypatch: Any) -> None:
        from starlette.testclient import TestClient

        from agentos.gateway import app as app_module

        approval_delivery.set_lineage_resolver(lambda key: ["agent:main:webchat:p"])
        queue = _queue(tmp_path)
        queue.request("exec", {"sessionKey": "agent:main:subagent:c"})
        monkeypatch.setattr(app_module, "get_approval_queue", lambda: queue)
        from agentos.gateway.config import AuthConfig

        config = GatewayConfig(auth=AuthConfig(mode="token", token="t"))
        client = TestClient(
            app_module.create_gateway_app(config),
            base_url="http://localhost",
            headers={"authorization": "Bearer t"},
        )
        item = client.get("/api/approvals").json()["pending"][0]
        assert item["audienceSessionKeys"][:2] == ["agent:main:subagent:c", "agent:main:webchat:p"]


# ---------------------------------------------------------------------------
# Channel custody
# ---------------------------------------------------------------------------


def _entry(session_key: str, audience: tuple[str, ...] = ()) -> Any:
    return SimpleNamespace(params={"sessionKey": session_key}, audience_session_keys=audience)


class TestCustody:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("agent:main:tg:direct:42", ("tg", "direct", "42")),
            ("agent:main:tg:group:-100:topic:7", ("tg", "group", "-100")),
            ("agent:main:tg:work:direct:42", ("tg", "direct", "42")),
            ("subagent:agent:main:slack:channel:C1", ("slack", "channel", "C1")),
            ("agent:main:main", None),
            ("agent:main:webchat:default", None),
            ("agent:main:direct:42", None),
        ],
    )
    def test_parse(self, key: str, expected: tuple[str, str, str] | None) -> None:
        parsed = parse_channel_session_key(key)
        assert (None if parsed is None else (parsed.channel, parsed.kind, parsed.peer)) == expected

    def test_dm_session_matches_the_sender(self) -> None:
        entry = _entry("agent:main:tg:direct:42")
        assert channel_custody(entry, channel_name="tg", sender_id="42", chat_id="42").allowed
        other = channel_custody(entry, channel_name="tg", sender_id="43", chat_id="43")
        assert (other.allowed, other.reason) == (False, "other-chat")

    def test_group_session_matches_the_chat(self) -> None:
        entry = _entry("agent:main:tg:group:-100")
        assert channel_custody(entry, channel_name="tg", sender_id="7", chat_id="-100").allowed
        assert not channel_custody(entry, channel_name="tg", sender_id="7", chat_id="-200").allowed

    def test_other_channel_account_is_refused(self) -> None:
        entry = _entry("agent:main:tg:direct:42")
        decision = channel_custody(entry, channel_name="tg-work", sender_id="42", chat_id="42")
        assert (decision.allowed, decision.reason) == (False, "other-channel")

    def test_web_session_cannot_be_answered_from_a_channel(self, tmp_path: Path) -> None:
        """The hole this closes: any paired sender could approve a console prompt."""
        index = SessionIndex(_sessions_db(tmp_path, [{"key": "agent:main:webchat:default"}]))
        decision = channel_custody(
            _entry("agent:main:webchat:default"),
            channel_name="tg",
            sender_id="42",
            chat_id="42",
            index=index,
        )
        assert (decision.allowed, decision.reason) == (False, "not-delivered-here")

    def test_shared_main_session_follows_its_delivery_route(self, tmp_path: Path) -> None:
        index = SessionIndex(
            _sessions_db(tmp_path, [{"key": "agent:main:main", "channel": "tg", "to": "42"}])
        )
        entry = _entry("agent:main:main")
        allowed = channel_custody(
            entry, channel_name="tg", sender_id="42", chat_id="42", index=index
        )
        assert (allowed.allowed, allowed.reason) == (True, "delivery-route")
        assert not channel_custody(
            entry, channel_name="tg", sender_id="9", chat_id="9", index=index
        ).allowed
        assert not channel_custody(
            entry, channel_name="discord", sender_id="42", chat_id="42", index=index
        ).allowed

    def test_child_session_answers_through_its_parent_chat(self, tmp_path: Path) -> None:
        index = SessionIndex(_sessions_db(tmp_path, [{"key": "agent:main:acp:r1"}]))
        entry = _entry("agent:main:acp:r1", ("agent:main:acp:r1", "agent:main:tg:direct:42"))
        decision = channel_custody(
            entry, channel_name="tg", sender_id="42", chat_id="42", index=index
        )
        assert (decision.allowed, decision.session_key) == (True, "agent:main:tg:direct:42")

    def test_no_session_is_refused(self) -> None:
        entry = SimpleNamespace(params={}, audience_session_keys=())
        assert not channel_custody(entry, channel_name="tg", sender_id="1", chat_id="1").allowed
