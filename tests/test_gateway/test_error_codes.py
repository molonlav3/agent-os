"""The closed error-code enums and the category every emitted code maps to."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

import agentos.gateway as gateway_pkg
from agentos.gateway import protocol
from agentos.gateway.app import RPC_CATEGORY_STATUS, rpc_status_code
from agentos.gateway.protocol import (
    CATEGORY_CODES,
    LEGACY_ERROR_CATEGORIES,
    ErrorCode,
    ErrorDetailCode,
    error_category,
    make_error_res,
)

_GATEWAY_SRC = Path(gateway_pkg.__file__).resolve().parent


def test_category_codes_are_the_closed_top_level_set() -> None:
    assert {code.value for code in CATEGORY_CODES} == {
        "INVALID_REQUEST",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "NOT_FOUND",
        "METHOD_NOT_FOUND",
        "UNAVAILABLE",
        "RATE_LIMITED",
        "INTERNAL_ERROR",
    }


def test_detail_codes_include_the_shared_names() -> None:
    names = {code.value for code in ErrorDetailCode}
    assert {
        "PROTOCOL_MISMATCH",
        "AUTH_RATE_LIMITED",
        "REQUEST_START_TIMEOUT",
        "QUEUE_FULL",
        "GATEWAY_RESTARTING",
        "SLOW_CONSUMER",
        "REQUEST_CONFLICT",
        "ABORT_INCOMPLETE",
        "STARTUP_SIDECARS",
    } <= names


def test_legacy_constants_still_compare_equal_to_their_wire_strings() -> None:
    assert protocol.ERROR_INVALID_REQUEST == "INVALID_REQUEST"
    assert protocol.ERROR_UNAUTHORIZED == "UNAUTHORIZED"
    assert protocol.ERROR_METHOD_NOT_FOUND == "METHOD_NOT_FOUND"
    assert protocol.ERROR_NOT_LINKED == "NOT_LINKED"
    assert protocol.ERROR_APPROVAL_NOT_FOUND == "APPROVAL_NOT_FOUND"
    # An enum member on the wire serializes as the plain string.
    frame = make_error_res("r", ErrorCode.FORBIDDEN, "no")
    assert '"code":"FORBIDDEN"' in frame.model_dump_json()


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("INVALID_REQUEST", ErrorCode.INVALID_REQUEST),
        ("RATE_LIMITED", ErrorCode.RATE_LIMITED),
        ("NOT_LINKED", ErrorCode.UNAVAILABLE),
        ("APPROVAL_NOT_FOUND", ErrorCode.NOT_FOUND),
        ("QUEUE_FULL", ErrorCode.UNAVAILABLE),
        ("GATEWAY_RESTARTING", ErrorCode.UNAVAILABLE),
        ("flush_unavailable", ErrorCode.UNAVAILABLE),
        ("project.not_found", ErrorCode.NOT_FOUND),
        ("project.conflict", ErrorCode.INVALID_REQUEST),
        ("agent.exists", ErrorCode.INVALID_REQUEST),
        ("agent.builtin_immutable", ErrorCode.FORBIDDEN),
        ("trading.operator_required", ErrorCode.FORBIDDEN),
        ("trading.lp.not_owner", ErrorCode.FORBIDDEN),
        ("trading.invalid", ErrorCode.INVALID_REQUEST),
        ("sessions.rename_unsupported", ErrorCode.UNAVAILABLE),
        ("router.unknown_model", ErrorCode.NOT_FOUND),
        ("router.disabled", ErrorCode.UNAVAILABLE),
        ("ACP_SESSION_CLOSED", ErrorCode.INVALID_REQUEST),
        ("ACP_HARNESS_MISSING", ErrorCode.UNAVAILABLE),
        ("ACP_AGENT_NOT_ALLOWED", ErrorCode.FORBIDDEN),
        ("ACP_UNKNOWN_AGENT", ErrorCode.NOT_FOUND),
        ("ACP_TURN_FAILED", ErrorCode.INTERNAL_ERROR),
    ],
)
def test_error_category(code: str, category: ErrorCode) -> None:
    assert error_category(code) is category


def test_unknown_upper_case_code_has_no_category() -> None:
    assert error_category("SOMETHING_NEW") is None
    assert error_category("") is None
    assert error_category(None) is None


def _literal_codes() -> list[tuple[str, str]]:
    """``(file, code)`` for every literal code a gateway module emits.

    Covers ``RpcHandlerError("CODE", ...)`` / ``RpcHandlerError(code="CODE")``
    and ``make_error_res(id, "CODE", ...)``.
    """
    found: list[tuple[str, str]] = []
    for path in sorted(_GATEWAY_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            candidates: list[ast.expr] = []
            if name == "RpcHandlerError":
                candidates = node.args[:1]
            elif name == "make_error_res":
                candidates = node.args[1:2]
            candidates += [kw.value for kw in node.keywords if kw.arg == "code"]
            for arg in candidates:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((path.name, arg.value))
    return found


def test_no_handler_emits_a_code_without_a_category() -> None:
    codes = _literal_codes()
    assert len(codes) > 20  # the walk really saw the handlers
    unknown = sorted({(file, code) for file, code in codes if error_category(code) is None})
    assert unknown == [], (
        "Add each new upper-case code to protocol.LEGACY_ERROR_CATEGORIES "
        f"(or use an ErrorCode member with details.code): {unknown}"
    )


def test_every_legacy_entry_maps_to_a_category_code() -> None:
    assert all(category in CATEGORY_CODES for category in LEGACY_ERROR_CATEGORIES.values())


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("INVALID_REQUEST", 400),
        ("UNAUTHORIZED", 403),
        ("FORBIDDEN", 403),
        ("NOT_FOUND", 404),
        ("METHOD_NOT_FOUND", 404),
        ("RATE_LIMITED", 429),
        ("UNAVAILABLE", 503),
        ("QUEUE_FULL", 503),
        ("agent.not_found", 404),
        ("trading.operator_required", 403),
        ("SOMETHING_NEW", 500),
    ],
)
def test_http_status_follows_the_category(code: str, status: int) -> None:
    result = SimpleNamespace(error=SimpleNamespace(code=code))
    assert rpc_status_code(result) == status


def test_every_category_has_an_http_status() -> None:
    assert set(RPC_CATEGORY_STATUS) == set(CATEGORY_CODES)
