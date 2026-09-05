"""Safety defaults, explicit configuration precedence, and invalid-input behavior."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from job_hunting_machine.config import ConfigurationError, RuntimeSettings, load_settings
from job_hunting_machine.runtime import RuntimeMode
from job_hunting_machine.security.paths import PROJECT_ROOT


def test_runtime_defaults_to_dry_run() -> None:
    settings = load_settings(runtime_file=None, logging_file=None, env_file=None, environ={})
    assert settings.runtime_mode is RuntimeMode.DRY_RUN
    assert settings.log_level == "INFO"
    assert settings.project_root == PROJECT_ROOT


def test_shipped_configuration_defaults_to_dry_run() -> None:
    assert load_settings(env_file=None, environ={}).runtime_mode is RuntimeMode.DRY_RUN


@pytest.mark.parametrize("mode", list(RuntimeMode))
def test_exact_modes_load_only_when_selected(mode: RuntimeMode) -> None:
    settings = load_settings(
        runtime_file=None,
        logging_file=None,
        env_file=None,
        environ={"JHM_RUNTIME_MODE": mode.value},
    )
    assert settings.runtime_mode is mode


def test_configuration_precedence(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.yaml"
    logging = tmp_path / "logging.yaml"
    dotenv = tmp_path / ".env"
    runtime.write_text("runtime_mode: STAGING\n", encoding="utf-8")
    logging.write_text("log_level: WARNING\n", encoding="utf-8")
    dotenv.write_text("JHM_RUNTIME_MODE=DRY_RUN\nJHM_LOG_LEVEL=ERROR\n", encoding="utf-8")
    yaml_only = load_settings(runtime_file=runtime, logging_file=logging, env_file=None, environ={})
    assert (yaml_only.runtime_mode, yaml_only.log_level) == (RuntimeMode.STAGING, "WARNING")
    with_dotenv = load_settings(
        runtime_file=runtime, logging_file=logging, env_file=dotenv, environ={}
    )
    assert (with_dotenv.runtime_mode, with_dotenv.log_level) == (RuntimeMode.DRY_RUN, "ERROR")
    with_environment = load_settings(
        runtime_file=runtime,
        logging_file=logging,
        env_file=dotenv,
        environ={"JHM_RUNTIME_MODE": "STAGING", "JHM_LOG_LEVEL": "DEBUG"},
    )
    assert (with_environment.runtime_mode, with_environment.log_level) == (
        RuntimeMode.STAGING,
        "DEBUG",
    )


@pytest.mark.parametrize("mode", ["live", "TEST", "", "true", " LIVE "])
def test_invalid_mode_fails_closed(mode: str) -> None:
    with pytest.raises(ConfigurationError, match="Invalid runtime"):
        load_settings(env_file=None, environ={"JHM_RUNTIME_MODE": mode})


def test_invalid_log_level_does_not_echo_value() -> None:
    with pytest.raises(ConfigurationError) as error:
        load_settings(env_file=None, environ={"JHM_LOG_LEVEL": "secret-test-value"})
    assert "secret-test-value" not in str(error.value)


@pytest.mark.parametrize("key", ["JHM_MODE", "JHM_PROJECT_ROOT"])
def test_unknown_environment_key_rejected(key: str) -> None:
    with pytest.raises(ConfigurationError, match="Unknown JHM_"):
        load_settings(env_file=None, environ={key: "value"})


def test_settings_are_immutable_and_root_is_not_configurable() -> None:
    settings = RuntimeSettings()
    with pytest.raises(ValidationError, match="frozen"):
        settings.runtime_mode = RuntimeMode.LIVE  # type: ignore[misc]  # Check runtime freezing.
    with pytest.raises(ValidationError):
        RuntimeSettings.model_validate({"project_root": "/"})


@pytest.mark.parametrize(
    "content",
    ["", "- LIVE\n", "runtime_mode: [\n", "mode: LIVE\n", "runtime_mode: DRY_RUN\nextra: x\n"],
)
def test_malformed_or_unknown_yaml_rejected(tmp_path: Path, content: str) -> None:
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(runtime_file=runtime, env_file=None, environ={})


def test_missing_selected_yaml_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Cannot read"):
        load_settings(runtime_file=tmp_path / "missing.yaml", env_file=None, environ={})


def test_optional_dotenv_absence_is_safe(tmp_path: Path) -> None:
    assert load_settings(env_file=tmp_path / "missing.env", environ={}).runtime_mode is (
        RuntimeMode.DRY_RUN
    )


def test_dotenv_interpolation_cannot_enable_live(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("VALUE=LIVE\nJHM_RUNTIME_MODE=${VALUE}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(env_file=dotenv, environ={})


def test_external_config_paths_are_rejected_without_reading() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(runtime_file=Path("/etc/passwd"), env_file=None, environ={})
    with pytest.raises(ConfigurationError):
        load_settings(env_file=Path("/etc/passwd"), environ={})


def test_current_directory_cannot_change_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("JHM_RUNTIME_MODE=LIVE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(
        runtime_file=Path("config/runtime.yaml"),
        logging_file=Path("config/logging.yaml"),
        env_file=None,
        environ={},
    )
    assert settings.runtime_mode is RuntimeMode.DRY_RUN


def test_actual_environment_is_loaded_without_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JHM_RUNTIME_MODE", "STAGING")
    assert load_settings(env_file=None).runtime_mode is RuntimeMode.STAGING
