"""``agentos gateway install | uninstall``: run the gateway as a user service.

macOS gets a LaunchAgent (``~/Library/LaunchAgents/dev.useagentos.gateway.plist``),
Linux a systemd user unit (``~/.config/systemd/user/agentos-gateway.service``).
Windows is not supported. Both run the absolute ``agentos`` executable with
``gateway run --port N`` and restart it when it dies:

* launchd: ``KeepAlive`` + ``ThrottleInterval 10`` (at most one respawn per
  10 s), ``ExitTimeOut`` = drain budget + margin so a stop can drain turns;
* systemd: ``Restart=always``, ``RestartSec=5``, ``RestartPreventExitStatus=78``
  (EX_CONFIG: a broken config is not restarted into a loop), ``KillMode=mixed``
  and ``TimeoutStopSec`` = the same budget.

The gateway's crash-loop breaker (safe mode) is what keeps keep-alive from
becoming a restart storm when a channel crashes it at boot.

Every function takes the home directory, the platform and the subprocess
runner as arguments, so tests (and an operator's dry run with ``--no-start``)
never touch the real service manager.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

LAUNCHD_LABEL = "dev.useagentos.gateway"
SYSTEMD_UNIT = "agentos-gateway.service"
#: Seconds on top of the drain budget before the service manager kills.
STOP_MARGIN_S = 10
#: Seconds between two respawns (launchd ``ThrottleInterval``).
LAUNCHD_THROTTLE_S = 10
#: Seconds systemd waits before a restart (``RestartSec``).
SYSTEMD_RESTART_S = 5
#: Variables carried into the unit when the installing shell sets them, so
#: the service runs against the same state and config as the operator's CLI.
CARRIED_ENV = ("AGENTOS_STATE_DIR", "AGENTOS_GATEWAY_CONFIG_PATH")

ServicePlatform = Literal["launchd", "systemd"]
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
UNSUPPORTED_MESSAGE = (
    "agentos gateway install is not supported on Windows; it supports macOS (launchd) "
    "and Linux (systemd user units)."
)


def detect_platform(sys_platform: str | None = None) -> ServicePlatform | None:
    value = sys.platform if sys_platform is None else sys_platform
    if value == "darwin":
        return "launchd"
    if value.startswith("linux"):
        return "systemd"
    return None


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argv is built here, never a shell string
            list(argv),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))


def resolve_agentos_program(
    *,
    argv0: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    executable: str | None = None,
) -> list[str]:
    """The absolute command that starts this CLI: what the unit executes.

    ``sys.argv[0]`` when it is the ``agentos`` script (made absolute but not
    resolved, so a ``~/.local/bin/agentos`` symlink keeps following
    ``uv tool upgrade``), else ``agentos`` on ``PATH``, else this interpreter
    with ``-m agentos.cli.main``.
    """
    raw = sys.argv[0] if argv0 is None else argv0
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.name in {"agentos", "agentos.exe"} and candidate.exists():
            return [str(candidate.absolute())]
    found = which("agentos")
    if found:
        return [str(Path(found).absolute())]
    return [executable or sys.executable, "-m", "agentos.cli.main"]


def service_path_env(home: Path, inherited: str | None = None) -> str:
    """``PATH`` for the unit: the user's tool dirs first, then the system ones.

    A service manager starts the gateway with a bare ``PATH``; without these
    the harness checks miss Homebrew/npm/uv-installed CLIs.
    """
    entries = [
        str(home / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for extra in (inherited or "").split(os.pathsep):
        if extra and extra not in entries:
            entries.append(extra)
    return os.pathsep.join(entries)


def stop_timeout_s() -> int:
    from agentos.gateway.readiness import drain_budget_ms

    return int(drain_budget_ms() / 1000) + STOP_MARGIN_S


@dataclass(frozen=True)
class ServiceSpec:
    platform: ServicePlatform
    home: Path
    program: list[str]
    port: int
    config_path: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LAUNCHD_LABEL if self.platform == "launchd" else SYSTEMD_UNIT

    @property
    def unit_path(self) -> Path:
        return unit_path_for(self.platform, self.home)

    @property
    def log_dir(self) -> Path:
        return self.home / "Library" / "Logs" / "AgentOS"

    @property
    def argv(self) -> list[str]:
        argv = [*self.program, "gateway", "run", "--port", str(self.port)]
        if self.config_path:
            argv.extend(["--config", self.config_path])
        return argv


def unit_path_for(platform: ServicePlatform, home: Path) -> Path:
    if platform == "launchd":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return home / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def build_spec(
    platform: ServicePlatform,
    *,
    home: Path,
    port: int,
    config_path: str | None = None,
    program: list[str] | None = None,
    environ: dict[str, str] | None = None,
) -> ServiceSpec:
    source = os.environ if environ is None else environ
    env = {"PATH": service_path_env(home)}
    for key in CARRIED_ENV:
        value = str(source.get(key, "") or "").strip()
        if value:
            env[key] = value
    resolved_config = str(Path(config_path).expanduser().absolute()) if config_path else None
    return ServiceSpec(
        platform=platform,
        home=home,
        program=program or resolve_agentos_program(),
        port=port,
        config_path=resolved_config,
        env=env,
    )


def render_launchd_plist(spec: ServiceSpec) -> str:
    payload: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": spec.argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": LAUNCHD_THROTTLE_S,
        "ExitTimeOut": stop_timeout_s(),
        "WorkingDirectory": str(spec.home),
        "StandardOutPath": str(spec.log_dir / "gateway-service.log"),
        "StandardErrorPath": str(spec.log_dir / "gateway-service.err.log"),
        "EnvironmentVariables": dict(spec.env),
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def _systemd_quote(value: str) -> str:
    """One ``ExecStart``/``Environment`` word: ``%`` doubled, quoted when needed."""
    escaped = value.replace("%", "%%")
    if escaped and not any(ch in escaped for ch in " \t\"\\'$;"):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    exec_start = " ".join(_systemd_quote(part) for part in spec.argv)
    env_lines = "".join(
        f"Environment={_systemd_quote(f'{key}={value}')}\n" for key, value in spec.env.items()
    )
    return (
        "[Unit]\n"
        "Description=AgentOS gateway\n"
        "Documentation=https://github.com/use-agent-os/agent-os/blob/main/docs/gateway.md\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={_systemd_quote(str(spec.home))}\n"
        f"{env_lines}"
        "Restart=always\n"
        f"RestartSec={SYSTEMD_RESTART_S}\n"
        "RestartPreventExitStatus=78\n"
        "KillMode=mixed\n"
        "KillSignal=SIGTERM\n"
        f"TimeoutStopSec={stop_timeout_s()}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_unit(spec: ServiceSpec) -> str:
    return render_launchd_plist(spec) if spec.platform == "launchd" else render_systemd_unit(spec)


@dataclass
class ServiceResult:
    action: str
    state: str
    ok: bool = True
    platform: str | None = None
    label: str | None = None
    path: str | None = None
    message: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "state": self.state,
            "platform": self.platform,
            "label": self.label,
            "path": self.path,
        }
        if self.message:
            payload["message"] = self.message
        if self.commands:
            payload["commands"] = self.commands
        if self.details:
            payload["details"] = self.details
        return payload


def _run(runner: Runner, argv: list[str], log: list[dict[str, Any]]) -> int:
    proc = runner(argv)
    entry: dict[str, Any] = {"argv": argv, "returncode": proc.returncode}
    stderr = (proc.stderr or "").strip()
    if stderr:
        entry["stderr"] = stderr[-500:]
    log.append(entry)
    return int(proc.returncode)


def _launchd_domain(uid: int) -> str:
    return f"gui/{uid}"


def _launchd_loaded(runner: Runner, uid: int, log: list[dict[str, Any]]) -> bool:
    return _run(runner, ["launchctl", "print", f"{_launchd_domain(uid)}/{LAUNCHD_LABEL}"], log) == 0


def _systemd_active(runner: Runner, log: list[dict[str, Any]]) -> bool:
    return _run(runner, ["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT], log) == 0


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else 0


UNINSTALL_HINT = "agentos gateway uninstall"


def install_service(
    spec: ServiceSpec,
    *,
    runner: Runner = default_runner,
    uid: int | None = None,
    start: bool = True,
) -> ServiceResult:
    """Write the unit and load it. Idempotent for an identical unit; refuses a different one."""
    uid = _current_uid() if uid is None else uid
    path = spec.unit_path
    rendered = render_unit(spec)
    commands: list[dict[str, Any]] = []
    result = ServiceResult(
        action="install",
        state="installed",
        platform=spec.platform,
        label=spec.label,
        path=str(path),
        commands=commands,
        details={
            "argv": spec.argv,
            "env": dict(spec.env),
            "stopTimeoutS": stop_timeout_s(),
            "unit": rendered,
        },
    )

    existing: str | None = None
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            existing = f"<unreadable: {exc}>"
    if existing is not None and existing != rendered:
        result.ok = False
        result.state = "conflict"
        result.message = (
            f"A service named {spec.label} is already installed at {path} with different "
            f"settings. Remove it first with '{UNINSTALL_HINT}', then install again."
        )
        return result

    if spec.platform == "launchd" and existing is None and _launchd_loaded(runner, uid, commands):
        result.ok = False
        result.state = "conflict"
        result.message = (
            f"A launchd job labelled {LAUNCHD_LABEL} is already loaded from another plist. "
            f"Unload it first: launchctl bootout {_launchd_domain(uid)}/{LAUNCHD_LABEL}"
        )
        return result

    if existing is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.platform == "launchd":
            spec.log_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, path)
    else:
        result.state = "already_installed"

    if not start:
        verb = "Wrote" if result.state == "installed" else "Already installed:"
        result.message = f"{verb} {path}; not loaded (--no-start)."
        return result

    if spec.platform == "launchd":
        if _launchd_loaded(runner, uid, commands):
            result.message = result.message or f"{LAUNCHD_LABEL} is loaded."
            return result
        _run(runner, ["launchctl", "enable", f"{_launchd_domain(uid)}/{LAUNCHD_LABEL}"], commands)
        rc = _run(runner, ["launchctl", "bootstrap", _launchd_domain(uid), str(path)], commands)
        if rc != 0:
            result.ok = False
            result.state = "load_failed"
            result.message = (
                f"Wrote {path} but 'launchctl bootstrap' failed (exit {rc}); see commands[].stderr."
            )
            return result
        result.message = f"Installed and loaded {LAUNCHD_LABEL} ({path})."
        return result

    if _run(runner, ["systemctl", "--user", "daemon-reload"], commands) != 0:
        result.ok = False
        result.state = "load_failed"
        result.message = f"Wrote {path} but 'systemctl --user daemon-reload' failed."
        return result
    rc = _run(runner, ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT], commands)
    if rc != 0:
        result.ok = False
        result.state = "load_failed"
        result.message = (
            f"Wrote {path} but 'systemctl --user enable --now {SYSTEMD_UNIT}' failed (exit {rc})."
        )
        return result
    result.message = (
        f"Installed and started {SYSTEMD_UNIT} ({path}). To keep it running after you log "
        "out: loginctl enable-linger $USER"
    )
    return result


def uninstall_service(
    platform: ServicePlatform,
    *,
    home: Path,
    runner: Runner = default_runner,
    uid: int | None = None,
) -> ServiceResult:
    """Stop and remove the unit. Idempotent: nothing installed is success."""
    uid = _current_uid() if uid is None else uid
    path = unit_path_for(platform, home)
    commands: list[dict[str, Any]] = []
    label = LAUNCHD_LABEL if platform == "launchd" else SYSTEMD_UNIT
    result = ServiceResult(
        action="uninstall",
        state="uninstalled",
        platform=platform,
        label=label,
        path=str(path),
        commands=commands,
    )
    stopped = False
    if platform == "launchd":
        if _launchd_loaded(runner, uid, commands):
            rc = _run(runner, ["launchctl", "bootout", f"{_launchd_domain(uid)}/{label}"], commands)
            if rc != 0 and _launchd_loaded(runner, uid, commands):
                result.ok = False
                result.state = "unload_failed"
                result.message = f"'launchctl bootout' failed (exit {rc}); the job is still loaded."
                return result
            stopped = True
    elif path.exists() or _systemd_active(runner, commands):
        _run(runner, ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], commands)
        stopped = True
    existed = path.exists()
    if existed:
        path.unlink()
        if platform == "systemd":
            _run(runner, ["systemctl", "--user", "daemon-reload"], commands)
    if not existed and not stopped:
        result.state = "not_installed"
        result.message = "No AgentOS gateway service is installed."
        return result
    result.message = f"{'Stopped and removed' if stopped else 'Removed'} {label} ({path})."
    return result


def service_status(
    platform: ServicePlatform | None,
    *,
    home: Path,
    runner: Runner = default_runner,
    uid: int | None = None,
) -> dict[str, Any]:
    """What ``agentos gateway status --deep`` reports under ``service``."""
    if platform is None:
        return {"supported": False, "installed": False, "message": UNSUPPORTED_MESSAGE}
    uid = _current_uid() if uid is None else uid
    path = unit_path_for(platform, home)
    commands: list[dict[str, Any]] = []
    loaded = (
        _launchd_loaded(runner, uid, commands)
        if platform == "launchd"
        else _systemd_active(runner, commands)
    )
    return {
        "supported": True,
        "platform": platform,
        "label": LAUNCHD_LABEL if platform == "launchd" else SYSTEMD_UNIT,
        "path": str(path),
        "installed": path.exists(),
        "loaded" if platform == "launchd" else "active": loaded,
    }


# ── CLI entry points (``agentos gateway install`` / ``uninstall``) ────


def _emit(result: ServiceResult, *, json_output: bool, show_unit: str | None = None) -> None:
    import json

    import typer

    if json_output:
        typer.echo(json.dumps(result.to_payload(), ensure_ascii=False, default=str))
    else:
        stream_err = not result.ok
        if result.message:
            typer.echo(result.message, err=stream_err)
        if show_unit is not None and result.ok:
            typer.echo(f"\n# {result.path}\n{show_unit}")
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def install_gateway(
    *,
    port: int | None,
    config_path: str | None,
    start: bool,
    json_output: bool,
    home: Path | None = None,
    platform: ServicePlatform | None | Literal["auto"] = "auto",
    runner: Runner = default_runner,
    program: list[str] | None = None,
) -> ServiceResult:
    import typer

    resolved_platform = detect_platform() if platform == "auto" else platform
    if resolved_platform is None:
        result = ServiceResult(
            action="install", state="unsupported", ok=False, message=UNSUPPORTED_MESSAGE
        )
        _emit(result, json_output=json_output)
        return result
    resolved_port = port
    if resolved_port is None:
        from agentos.cli.gateway_cmd import _load_config_or_exit

        resolved_port = int(_load_config_or_exit(config_path).port)
    if not 0 < int(resolved_port) < 65536:
        raise typer.BadParameter("--port must be between 1 and 65535")
    spec = build_spec(
        resolved_platform,
        home=home or Path.home(),
        port=int(resolved_port),
        config_path=config_path,
        program=program,
    )
    result = install_service(spec, runner=runner, start=start)
    _emit(
        result,
        json_output=json_output,
        show_unit=render_unit(spec) if result.state == "installed" else None,
    )
    return result


def uninstall_gateway(
    *,
    json_output: bool,
    home: Path | None = None,
    platform: ServicePlatform | None | Literal["auto"] = "auto",
    runner: Runner = default_runner,
) -> ServiceResult:
    resolved_platform = detect_platform() if platform == "auto" else platform
    if resolved_platform is None:
        result = ServiceResult(
            action="uninstall", state="unsupported", ok=False, message=UNSUPPORTED_MESSAGE
        )
    else:
        result = uninstall_service(resolved_platform, home=home or Path.home(), runner=runner)
    _emit(result, json_output=json_output)
    return result


__all__ = [
    "CARRIED_ENV",
    "LAUNCHD_LABEL",
    "STOP_MARGIN_S",
    "SYSTEMD_UNIT",
    "UNSUPPORTED_MESSAGE",
    "ServiceResult",
    "ServiceSpec",
    "build_spec",
    "default_runner",
    "detect_platform",
    "install_gateway",
    "install_service",
    "render_launchd_plist",
    "render_systemd_unit",
    "resolve_agentos_program",
    "service_path_env",
    "service_status",
    "stop_timeout_s",
    "uninstall_gateway",
    "uninstall_service",
    "unit_path_for",
]
