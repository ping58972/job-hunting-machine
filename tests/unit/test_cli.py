"""CLI only inspects the foundation; it cannot start workflows."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_hunting_machine import __version__
from job_hunting_machine.cli import app

runner = CliRunner()


def test_cli_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Phase 0" in result.stdout


def test_cli_default_configuration(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "--env-file", str(tmp_path / "missing.env")])
    assert result.exit_code == 0, result.output
    config = json.loads(result.stdout)
    assert config["configured_runtime_mode"] == "DRY_RUN"
    assert config["workflow_available"] is False
    assert config["phase"] == 0
    log = json.loads(result.stderr)
    assert log["event"] == "configuration_validated"


def test_live_configuration_is_inspection_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHM_RUNTIME_MODE", "LIVE")
    result = runner.invoke(app, ["config", "--env-file", str(tmp_path / "missing.env")])
    assert result.exit_code == 0, result.output
    config = json.loads(result.stdout)
    assert config["configured_runtime_mode"] == "LIVE"
    assert config["workflow_available"] is False


def test_configuration_errors_return_two_and_hide_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JHM_RUNTIME_MODE", "secret-test-value")
    result = runner.invoke(app, ["config", "--env-file", str(tmp_path / "missing.env")])
    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
    assert "secret-test-value" not in result.output


def test_workflow_execution_is_unavailable() -> None:
    result = runner.invoke(app, ["run", "--live"])
    assert result.exit_code == 2
    assert "No such command" in result.stderr
