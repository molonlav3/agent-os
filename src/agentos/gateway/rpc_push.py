"""RPC handlers for Web Push subscriptions (``push.web.*``)."""

from __future__ import annotations

from typing import Any

from agentos.gateway.access import CONTROL_ONLY
from agentos.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher

_d = get_dispatcher()


def _service(ctx: RpcContext) -> Any:
    from agentos.gateway.web_push import get_web_push_service

    service = get_web_push_service(getattr(ctx, "config", None))
    if service is None:
        raise RpcUnavailableError("web push needs a gateway state directory")
    return service


def _refuse_agent(ctx: RpcContext) -> None:
    """A connection recognized as an agent's may not point approval notices elsewhere.

    Only an agent token or an open exec window counts: a browser console never
    presents an operator secret, so it is ``unbound``, and it is exactly who
    subscribes.
    """
    from agentos.gateway.agent_surface import agent_binding

    binding = agent_binding(ctx)
    if binding is not None and binding.via != "unbound":
        raise ValueError("push subscriptions are the user's to manage; an agent cannot")


@_d.method("push.web.vapid_public_key", CONTROL_ONLY)
async def _handle_vapid_public_key(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    service = _service(ctx)
    return {"publicKey": service.keys.public_key_b64, "enabled": service.enabled}


@_d.method("push.web.subscribe", CONTROL_ONLY)
async def _handle_subscribe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from agentos.gateway.web_push import validate_subscription

    if not isinstance(params, dict):
        raise ValueError("params must be an object with endpoint and keys")
    _refuse_agent(ctx)
    service = _service(ctx)
    if not service.enabled:
        raise ValueError("web push is disabled ([push] web_enabled = false)")
    subscription = validate_subscription(params.get("endpoint"), params.get("keys"))
    service.store.upsert(subscription)
    return {"ok": True}


@_d.method("push.web.unsubscribe", CONTROL_ONLY)
async def _handle_unsubscribe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("endpoint"), str):
        raise ValueError("params.endpoint is required")
    _refuse_agent(ctx)
    removed = _service(ctx).store.remove(str(params["endpoint"]).strip())
    return {"ok": True, "removed": removed}
