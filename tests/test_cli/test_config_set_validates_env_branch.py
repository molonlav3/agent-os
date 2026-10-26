"""Issue #3100: ``agentos config set`` handed out an export line for a value it
would refuse with ``--config``, and ``agentos gateway run`` then died with a
pydantic traceback.

The env-var branch now validates the edited config through ``GatewayConfig``
exactly as the ``--config`` branch does; ``gateway run`` / ``gateway start``
report an invalid config as one line per error, naming the environment
variable when one is set for that key.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentos.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No config file: the env-var branch is the only one on offer."""
    home = tmp_path / "state"
    monkeypatch.setenv("AGENTOS_STATE_DIR", str(home))
    monkeypatch.delenv("AGENTOS_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


# ── config set without --config ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value", "accepted"),
    [
        ("tools.profile", "bogus", "'full', 'minimal', 'memory_only', 'coding' or 'messaging'"),
        ("permissions.default_mode", "yolo", "'off', 'on', 'bypass' or 'full'"),
        ("task_runtime.turn_hard_deadline_s", '"not-a-number"', "number"),
    ],
)
def test_an_invalid_value_is_refused_before_the_export_line(
    key: str, value: str, accepted: str
) -> None:
    result = runner.invoke(app, ["config", "set", key, value])

    assert result.exit_code == 2, result.output
    assert f"Invalid value for {key}" in result.output
    assert accepted in result.output
    assert "export " not in result.output.lower()


def test_the_refusal_matches_the_config_branch(tmp_path: Path) -> None:
    """Same value, same message, whichever branch: the operator learns the
    accepted values once and the two modes cannot disagree."""
    config_file = tmp_path / "config.toml"
    config_file.write_text("[tools]\nprofile = 'full'\n", encoding="utf-8")

    env_branch = runner.invoke(app, ["config", "set", "tools.profile", "bogus"])
    file_branch = runner.invoke(
        app, ["config", "set", "tools.profile", "bogus", "--config", str(config_file)]
    )

    assert env_branch.exit_code == file_branch.exit_code == 2
    assert "Invalid value for tools.profile" in env_branch.output
    assert "Invalid value for tools.profile" in file_branch.output


def test_a_valid_value_still_prints_the_export_line() -> None:
    result = runner.invoke(app, ["config", "set", "tools.profile", "coding"])

    assert result.exit_code == 0, result.output
    assert "export AGENTOS_GATEWAY_TOOLS__PROFILE=coding" in result.output


def test_a_typed_value_is_validated_as_its_type() -> None:
    ok = runner.invoke(app, ["config", "set", "skills.max_skills_prompt_chars", "32000"])
    bad = runner.invoke(app, ["config", "set", "skills.max_skills_prompt_chars", '"lots"'])

    assert ok.exit_code == 0, ok.output
    assert "export AGENTOS_GATEWAY_SKILLS__MAX_SKILLS_PROMPT_CHARS=32000" in ok.output
    assert bad.exit_code == 2
    assert "Invalid value for skills.max_skills_prompt_chars" in bad.output


def test_a_null_defaulted_key_still_reaches_the_export_line() -> None:
    """``auth.token`` is absent from the TOML view (#2031); validation must not
    turn that back into 'Key not found'."""
    result = runner.invoke(app, ["config", "set", "auth.token", "s3cr3t"])

    assert result.exit_code == 0, result.output
    assert "AGENTOS_GATEWAY_AUTH__TOKEN" in result.output


def test_an_unknown_key_is_still_key_not_found() -> None:
    result = runner.invoke(app, ["config", "set", "tools.profil", "coding"])

    assert result.exit_code == 1
    assert "Key not found" in result.output
    assert "Invalid value" not in result.output


# ── gateway run / start with an invalid config ─────────────────────────────


def test_gateway_run_reports_a_bad_env_override_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTOS_GATEWAY_TOOLS__PROFILE", "bogus")

    result = runner.invoke(app, ["gateway", "run", "--port", "18999"])

    # 78 = EX_CONFIG: the supervisor contract's "repair the config, then retry".
    assert result.exit_code == 78, result.output
    assert "AgentOS config error" in result.output
    assert "tools.profile:" in result.output
    assert "(set by AGENTOS_GATEWAY_TOOLS__PROFILE)" in result.output
    assert "Traceback" not in result.output
    assert "pydantic" not in result.output.lower()


def test_gateway_run_reports_a_bad_value_in_the_config_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[tools]\nprofile = "bogus"\n', encoding="utf-8")

    result = runner.invoke(app, ["gateway", "run", "--config", str(config_file)])

    assert result.exit_code == 78, result.output
    assert "tools.profile:" in result.output
    assert "(set by" not in result.output, "no env var is involved; do not blame one"
    assert "Traceback" not in result.output


def test_gateway_start_reports_the_same_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GATEWAY_PERMISSIONS__DEFAULT_MODE", "yolo")

    result = runner.invoke(app, ["gateway", "start", "--port", "18999"])

    assert result.exit_code == 2, result.output
    assert "permissions.default_mode:" in result.output
    assert "(set by AGENTOS_GATEWAY_PERMISSIONS__DEFAULT_MODE)" in result.output
    assert "Traceback" not in result.output


def test_every_error_is_listed_not_just_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOS_GATEWAY_TOOLS__PROFILE", "bogus")
    monkeypatch.setenv("AGENTOS_GATEWAY_PERMISSIONS__DEFAULT_MODE", "yolo")

    result = runner.invoke(app, ["gateway", "run", "--port", "18999"])

    assert result.exit_code == 78
    assert "tools.profile:" in result.output
    assert "permissions.default_mode:" in result.output
