"""HTTP download route for transcript attachment material.

``GET /api/v1/attachments/{sha256}?sessionKey=`` is the pre-ticket route and is
deprecated like its artifact sibling (see ``gateway.artifacts``); the
``artifacts.download`` RPC mints a ticketed URL for an attachment too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from agentos.attachment_refs import transcript_material_path, truncate_filename
from agentos.gateway.config import GatewayConfig
from agentos.paths import media_root_from_config


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


async def _material_scopes_for_download(session_manager: Any, session_key: str) -> list[str]:
    """Directories under ``transcripts/`` that may hold this session's material.

    ``chat.send`` stages an attachment under the session's ``session_id``, but a
    channel turn (``channel_dispatch``) stages it under the session key's last
    segment -- ``main`` for a DM on the default ``dm_scope``, the chat id for a
    group. Looking only under ``session_id`` turned every
    attachment a user sent over Telegram, Discord or Slack into a 404 in the
    Control UI. Both scopes are tried, canonical first, and only for a key that
    names an existing session.
    """
    session_id = await _session_id_for_download(session_manager, session_key)
    if not session_id:
        return []
    scopes = [session_id]
    key_scope = session_key.split(":")[-1] or session_key
    if key_scope not in scopes:
        scopes.append(key_scope)
    return scopes


def _media_root_from_config(config: GatewayConfig) -> Path:
    return media_root_from_config(config)


class AttachmentNotFoundError(LookupError):
    """No material with that hash is stored for that session."""


class AttachmentIntegrityError(ValueError):
    """The stored material no longer hashes to its name."""


async def resolve_attachment_download(
    config: GatewayConfig,
    session_manager: Any,
    session_key: str,
    sha256: str,
) -> Path:
    """Find ``sha256``'s material for ``session_key`` and verify it."""
    sha = str(sha256 or "").lower()
    media_root = _media_root_from_config(config)
    path: Path | None = None
    for scope in await _material_scopes_for_download(session_manager, session_key):
        try:
            candidate = transcript_material_path(media_root, scope, sha)
        except ValueError:
            continue
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise AttachmentNotFoundError(sha)
    from agentos.gateway.download_verify import file_digest

    try:
        actual_sha = await file_digest(path)
    except OSError as exc:
        raise AttachmentNotFoundError(sha) from exc
    if actual_sha != sha:
        raise AttachmentIntegrityError("Attachment integrity check failed")
    return path


#: Bound on the ``Content-Disposition`` filename. Trimmed from the stem so a
#: long name still downloads as something the OS can open.
_MAX_DOWNLOAD_NAME = 160


def safe_download_name(value: object) -> str:
    return _safe_download_name(value)


def safe_media_type(value: object) -> str:
    return _safe_media_type(value)


def _safe_download_name(value: object) -> str:
    raw = str(value or "").strip()
    cleaned = " ".join(raw.replace("/", " ").replace("\\", " ").split())
    return truncate_filename(cleaned, _MAX_DOWNLOAD_NAME) or "attachment"


def _safe_media_type(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or "/" not in raw or any(ch in raw for ch in "\r\n;"):
        return "application/octet-stream"
    return raw[:120]


def register_attachment_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/attachments/{sha256} on the given Starlette app."""

    async def download_handler(request: Request) -> FileResponse | JSONResponse:
        from agentos.gateway.artifacts import (
            legacy_session_key_urls_enabled,
            note_legacy_download_route,
        )

        if not legacy_session_key_urls_enabled(config):
            return JSONResponse(
                {
                    "error": "Session-key download URLs are disabled; use artifacts.download",
                    "code": "NOT_FOUND",
                },
                status_code=404,
            )
        note_legacy_download_route("attachment")
        sha = str(request.path_params.get("sha256", "")).lower()
        session_key = (
            request.query_params.get("sessionKey")
            or request.query_params.get("session_key")
            or request.headers.get("x-agentos-session-key")
            or ""
        )
        try:
            path = await resolve_attachment_download(config, session_manager, session_key, sha)
        except AttachmentNotFoundError:
            return JSONResponse(
                {"error": "Attachment not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        except AttachmentIntegrityError:
            return JSONResponse(
                {"error": "Attachment integrity check failed", "code": "INTEGRITY_ERROR"},
                status_code=409,
            )

        return FileResponse(
            path,
            media_type=_safe_media_type(request.query_params.get("mime")),
            filename=_safe_download_name(request.query_params.get("name")),
        )

    app.router.routes.append(
        Route("/api/v1/attachments/{sha256}", download_handler, methods=["GET", "HEAD"])
    )
