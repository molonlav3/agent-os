"""Origin + auth guard coverage for the JSON surface under the Control UI prefix (#351).

``LoopbackOriginMiddleware`` exempts the Control UI prefix because the shell
and its fingerprinted assets are navigations/subresources, not RPC sinks. The
JSON routes mounted under that prefix are a different animal: any page the
operator visits can ``fetch("http://127.0.0.1:18791/control/api/bootstrap")``
cross-origin and read the response. Those routes must stay behind the guard.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agentos.gateway.app import create_gateway_app
from agentos.gateway.config import AuthConfig, ControlUiConfig, GatewayConfig
from agentos.gateway.middleware import AuthMiddleware, LoopbackOriginMiddleware

_EVIL = "https://evil.example"


def _client(base_path: str = "/control", *, bind_is_loopback: bool = True) -> TestClient:
    config = GatewayConfig(control_ui=ControlUiConfig(base_path=base_path))

    async def ok(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[
            Route(f"{base_path}/", ok),
            Route(f"{base_path}/api/bootstrap", ok),
            Route(f"{base_path}/chat", ok),
            Route(f"{base_path}/static/dist/assets/app-a1b2c3.js", ok),
            Route("/api/config", ok),
        ]
    )
    app.add_middleware(
        LoopbackOriginMiddleware,
        config=config,
        bind_is_loopback=bind_is_loopback,
    )
    return TestClient(app)


def test_cross_origin_fetch_of_ui_bootstrap_is_rejected() -> None:
    response = _client().get("/control/api/bootstrap", headers={"origin": _EVIL})

    assert response.status_code == 403
    assert "Origin not allowed" in response.text


def test_cross_origin_fetch_of_ui_bootstrap_is_rejected_on_custom_base_path() -> None:
    response = _client("/console").get("/console/api/bootstrap", headers={"origin": _EVIL})

    assert response.status_code == 403


def test_same_origin_and_originless_bootstrap_requests_pass() -> None:
    client = _client()

    assert client.get("/control/api/bootstrap").status_code == 200
    same_origin = client.get("/control/api/bootstrap", headers={"origin": "http://127.0.0.1:18791"})
    assert same_origin.status_code == 200


def test_ui_shell_and_assets_stay_exempt_for_cross_origin_navigations() -> None:
    client = _client()
    headers = {"origin": _EVIL}

    # A cross-site navigation to the shell sends an Origin on some browsers; the
    # page it lands on is same-origin-isolated, so it must keep rendering.
    assert client.get("/control/", headers=headers).status_code == 200
    assert client.get("/control/chat", headers=headers).status_code == 200
    assert (
        client.get("/control/static/dist/assets/app-a1b2c3.js", headers=headers).status_code == 200
    )


def test_root_api_surface_stays_guarded() -> None:
    assert _client().get("/api/config", headers={"origin": _EVIL}).status_code == 403


def test_guard_applies_on_a_public_bind_too() -> None:
    """A non-loopback bind used to skip the guard (#3692); it no longer does.

    ``/api/bootstrap`` is carved out of ``AuthMiddleware`` so the console can
    read it before it holds a token, so on a LAN bind the origin policy is the
    only thing standing between it and a cross-site reader. A page served from
    the gateway's own private address is same-origin and still passes.
    """
    client = _client(bind_is_loopback=False)

    assert client.get("/control/api/bootstrap", headers={"origin": _EVIL}).status_code == 403
    lan = client.get(
        "/control/api/bootstrap",
        headers={"origin": "http://192.168.1.20:18791", "host": "192.168.1.20:18791"},
    )
    assert lan.status_code == 200


class TestAuthExemptionScope:
    """``AuthMiddleware`` exempts the served UI, not the JSON surface under it.

    ``/api/bootstrap`` is the single carve-out: the console fetches it before it
    holds a token, and (with ``config_path`` removed) it carries nothing an
    unauthenticated caller may not learn. Any other JSON route mounted under
    the UI prefix must fail closed onto the same token gate as ``/api/*``.
    """

    @staticmethod
    def _auth_client(base_path: str = "/control") -> TestClient:
        config = GatewayConfig(
            auth=AuthConfig(mode="token", token="secret-123"),
            control_ui=ControlUiConfig(base_path=base_path),
        )

        async def ok(_request: Request) -> PlainTextResponse:
            return PlainTextResponse("ok")

        app = Starlette(
            routes=[
                Route(f"{base_path}/", ok),
                Route(f"{base_path}/api/bootstrap", ok),
                Route(f"{base_path}/api/secrets", ok),
                Route("/api/config", ok),
            ]
        )
        app.add_middleware(AuthMiddleware, config=config)
        return TestClient(app)

    def test_bootstrap_stays_reachable_without_a_token(self) -> None:
        assert self._auth_client().get("/control/api/bootstrap").status_code == 200

    def test_shell_stays_reachable_without_a_token(self) -> None:
        assert self._auth_client().get("/control/").status_code == 200

    def test_other_ui_json_routes_require_a_token(self) -> None:
        client = self._auth_client()

        assert client.get("/control/api/secrets").status_code == 401
        assert (
            client.get(
                "/control/api/secrets", headers={"authorization": "Bearer secret-123"}
            ).status_code
            == 200
        )

    def test_root_api_still_requires_a_token(self) -> None:
        assert self._auth_client().get("/api/config").status_code == 401


class TestAssembledGatewayApp:
    """The guard only works because of where it sits in the real middleware stack.

    ``create_gateway_app`` installs ``LoopbackOriginMiddleware`` *before*
    ``CORSMiddleware`` (app.py). Reorder those two and CORS answers the
    preflight first, reflecting the wildcard origin and reopening #351 — with
    every unit test above still green. These exercise the assembled app so the
    ordering itself is covered.
    """

    @staticmethod
    def _app_client(auth_mode: str) -> TestClient:
        config = GatewayConfig(
            host="127.0.0.1",
            port=18791,
            auth=AuthConfig(mode=auth_mode, token="secret-123" if auth_mode == "token" else None),
        )
        return TestClient(create_gateway_app(config=config), base_url="http://127.0.0.1:18791")

    @pytest.mark.parametrize("auth_mode", ("none", "token"))
    def test_console_still_boots_without_a_token(self, auth_mode: str) -> None:
        """The SPA fetches bootstrap same-origin (no Origin header) before it has a token."""
        response = self._app_client(auth_mode).get("/control/api/bootstrap")

        assert response.status_code == 200
        assert "config_path" not in response.json()

    @pytest.mark.parametrize("auth_mode", ("none", "token"))
    def test_cross_origin_page_cannot_read_bootstrap(self, auth_mode: str) -> None:
        client = self._app_client(auth_mode)

        assert client.get("/control/api/bootstrap", headers={"origin": _EVIL}).status_code == 403
        preflight = client.options(
            "/control/api/bootstrap",
            headers={"origin": _EVIL, "access-control-request-method": "GET"},
        )
        assert preflight.status_code == 403
        assert "access-control-allow-origin" not in preflight.headers
