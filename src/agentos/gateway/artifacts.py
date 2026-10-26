"""HTTP download route for generated artifacts.

``GET /api/v1/artifacts/{artifact_id}?sessionKey=`` is the pre-ticket route:
the gateway token plus a session key, no expiry and no connection binding.
It keeps answering for one release behind ``[artifacts]
legacy_session_key_urls`` (each kind logs one deprecation warning); clients
mint a ticketed URL with the ``artifacts.download`` RPC instead
(``gateway.download_grants``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from agentos.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
)
from agentos.gateway.config import GatewayConfig
from agentos.paths import media_root_from_config

log = structlog.get_logger(__name__)

_LEGACY_WARNED: set[str] = set()


def note_legacy_download_route(kind: str) -> None:
    """Log the deprecated ``?sessionKey=`` route once per kind and process."""
    if kind in _LEGACY_WARNED:
        log.debug("gateway.download.legacy_session_key_url", kind=kind)
        return
    _LEGACY_WARNED.add(kind)
    log.warning(
        "gateway.download.legacy_session_key_url",
        kind=kind,
        hint="?sessionKey= download URLs are deprecated and go away next release; mint a "
        "ticketed URL with the artifacts.download RPC. Set [artifacts] "
        "legacy_session_key_urls = false to refuse them now.",
    )


def legacy_session_key_urls_enabled(config: GatewayConfig) -> bool:
    artifacts = getattr(config, "artifacts", None)
    return bool(getattr(artifacts, "legacy_session_key_urls", True))


async def _session_id_for_download(session_manager: Any, session_key: str) -> str | None:
    if not session_key:
        return None
    if session_manager is None:
        return session_key
    get_session = getattr(session_manager, "get_session", None)
    if not callable(get_session):
        return session_key
    try:
        session = await get_session(session_key)
    except Exception:
        return None
    session_id = getattr(session, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _media_root_from_config(config: GatewayConfig) -> Path:
    return media_root_from_config(config)


async def resolve_artifact_download(
    config: GatewayConfig,
    session_manager: Any,
    session_key: str,
    artifact_id: str,
) -> tuple[ArtifactRef, Path]:
    """Find ``artifact_id`` in ``session_key``'s store and verify its material.

    Raises ``ArtifactNotFoundError`` (also for an unknown session or a
    malformed id) or ``ArtifactIntegrityError``.
    """
    session_id = await _session_id_for_download(session_manager, session_key)
    if not session_id:
        raise ArtifactNotFoundError("artifact not found")
    store = ArtifactStore(_media_root_from_config(config))
    try:
        return store.resolve_for_download(str(artifact_id), session_id=session_id)
    except ArtifactIntegrityError:
        raise
    except (ArtifactNotFoundError, ValueError) as exc:
        raise ArtifactNotFoundError("artifact not found") from exc


def register_artifact_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/artifacts/{artifact_id} on the given Starlette app."""

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        if not legacy_session_key_urls_enabled(config):
            return JSONResponse(
                {
                    "error": "Session-key download URLs are disabled; use artifacts.download",
                    "code": "NOT_FOUND",
                },
                status_code=404,
            )
        note_legacy_download_route("artifact")
        artifact_id = request.path_params.get("artifact_id", "")
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-agentos-session-key")
            or ""
        )
        try:
            ref, path = await resolve_artifact_download(
                config, session_manager, session_key, str(artifact_id)
            )
        except ArtifactIntegrityError as exc:
            return JSONResponse({"error": str(exc), "code": "INTEGRITY_ERROR"}, status_code=409)
        except ArtifactNotFoundError:
            return JSONResponse(
                {"error": "Artifact not found", "code": "NOT_FOUND"},
                status_code=404,
            )

        return FileResponse(path, media_type=ref.mime, filename=ref.name)

    app.router.routes.append(
        Route("/api/v1/artifacts/{artifact_id}", download_handler, methods=["GET", "HEAD"])
    )
