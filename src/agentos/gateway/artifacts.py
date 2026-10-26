"""HTTP download route for generated artifacts.

``GET /api/v1/artifacts/{artifact_id}?sessionKey=`` is the pre-ticket route:
the gateway token plus a session key, no expiry and no connection binding.
It keeps answering for one release behind ``[artifacts]
legacy_session_key_urls`` (each kind logs one deprecation warning); clients
mint a ticketed URL with the ``artifacts.download`` RPC instead
(``gateway.download_grants``).
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any

import anyio
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
    key = (str(store.media_root), session_id, str(artifact_id))
    cached = _resolved_cache_get(key)
    if cached is not None:
        return cached
    try:
        # Reads the metadata and hashes the material: off the event loop.
        ref, path = await anyio.to_thread.run_sync(
            lambda: store.resolve_for_download(str(artifact_id), session_id=session_id)
        )
    except ArtifactIntegrityError:
        raise
    except (ArtifactNotFoundError, ValueError) as exc:
        raise ArtifactNotFoundError("artifact not found") from exc
    _resolved_cache_put(key, ref, path)
    return ref, path


#: Verified artifacts, kept while the material's size, mtime and inode are
#: unchanged: a ticket's HEAD, ranges and thumbnail must not each re-hash it.
_RESOLVED: OrderedDict[tuple[str, str, str], tuple[ArtifactRef, Path, tuple[int, int, int]]] = (
    OrderedDict()
)
_RESOLVED_MAX = 1024


def _file_stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _resolved_cache_get(key: tuple[str, str, str]) -> tuple[ArtifactRef, Path] | None:
    entry = _RESOLVED.get(key)
    if entry is None:
        return None
    ref, path, stamp = entry
    if _file_stamp(path) != stamp:
        _RESOLVED.pop(key, None)
        return None
    _RESOLVED.move_to_end(key)
    return ref, path


def _resolved_cache_put(key: tuple[str, str, str], ref: ArtifactRef, path: Path) -> None:
    stamp = _file_stamp(path)
    if stamp is None:
        return
    _RESOLVED[key] = (ref, path, stamp)
    _RESOLVED.move_to_end(key)
    while len(_RESOLVED) > _RESOLVED_MAX:
        _RESOLVED.popitem(last=False)


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
