import json
import os

import pytest

from media_service.config import Settings
from media_service.envfile import ENV_ONLY_FIELDS, read_env_file
from media_service.local_env import load_local_env

TOKEN = "isolated-env-service-token-0123456789"


@pytest.fixture
def configuration(tmp_path, settings, monkeypatch):
    for key in os.environ:
        if key.startswith("MEDIA_") or key.lower().startswith("llm_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    config = settings.model_dump(mode="json", exclude=ENV_ONLY_FIELDS)
    for caller in config["callers"].values():
        caller.pop("tokens", None)
    path = tmp_path / "structure.json"
    path.write_text(json.dumps(config))
    environment = tmp_path / ".env"
    environment.write_text(f"MEDIA_CONFIG_PATH={path}\nMEDIA_SERVICE_TOKEN={TOKEN}\n")
    return path, environment


def test_default_dotenv_is_shared_source_and_host_port_is_not_app_port(configuration):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_PORT=9123\nMEDIA_LLM_API_KEY=\nMEDIA_PUBLIC_BASE_URL=https://media.invalid\n")
    result = Settings.from_file()
    assert result.service_token.get_secret_value() == TOKEN
    assert result.port == 8080
    assert result.public_base_url == "https://media.invalid"
    assert result.llm_api_key is None and not result.llm_enabled and not result.a2a_enabled
    assert result.planning_timeout_seconds == 300
    assert result.log_level == "INFO" and not result.log_model_content
    assert result.llm_enable_thinking is False


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True), ("null", None)])
def test_thinking_setting_supports_explicit_three_states(configuration, value, expected):
    _, path = configuration
    with path.open("a") as stream:
        stream.write(f"MEDIA_LLM_ENABLE_THINKING={value}\n")
    assert Settings.from_file().llm_enable_thinking is expected


@pytest.mark.parametrize("value,expected", [("false", False), ("true", True), ("null", None)])
def test_thinking_process_setting_overrides_file(configuration, monkeypatch, value, expected):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_LLM_ENABLE_THINKING=true\n")
    monkeypatch.setenv("MEDIA_LLM_ENABLE_THINKING", value)
    assert Settings.from_file().llm_enable_thinking is expected


@pytest.mark.parametrize("value", ["", "NULL", "private-invalid-thinking-value"])
def test_invalid_thinking_setting_is_rejected_without_echoing(configuration, monkeypatch, value):
    monkeypatch.setenv("MEDIA_LLM_ENABLE_THINKING", value)
    with pytest.raises(ValueError, match="invalid local environment configuration") as error:
        Settings.from_file()
    assert "private-invalid-thinking-value" not in str(error.value)


@pytest.mark.parametrize("value", [False, True, None])
def test_explicit_thinking_constructor_values_remain_supported(settings, value):
    configured = Settings.model_validate(settings.model_dump() | {"llm_enable_thinking": value})
    assert configured.llm_enable_thinking is value


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_log_settings_load_from_env_with_process_precedence(configuration, monkeypatch, level):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_LOG_LEVEL=ERROR\nMEDIA_LOG_MODEL_CONTENT=false\n")
    monkeypatch.setenv("MEDIA_LOG_LEVEL", level)
    monkeypatch.setenv("MEDIA_LOG_MODEL_CONTENT", "true")
    result = Settings.from_file()
    assert result.log_level == level and result.log_model_content


def test_debug_content_is_opt_in(configuration):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_LOG_LEVEL=DEBUG\n")
    result = Settings.from_file()
    assert result.log_level == "DEBUG" and not result.log_model_content


@pytest.mark.parametrize("name,value", [
    ("MEDIA_LOG_LEVEL", ""), ("MEDIA_LOG_LEVEL", "debug"),
    ("MEDIA_LOG_LEVEL", "private-invalid-level"),
    ("MEDIA_LOG_MODEL_CONTENT", "private-invalid-switch"),
])
def test_invalid_log_settings_fail_without_echoing_values(configuration, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="invalid local environment configuration") as error:
        Settings.from_file()
    assert "private-invalid" not in str(error.value)


def test_main_configures_selected_log_level_without_enabling_access_logs(
        configuration, monkeypatch):
    from media_service import __main__ as entrypoint

    monkeypatch.setattr(entrypoint.sys, "argv", ["media-service"])
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("MEDIA_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(entrypoint.os, "umask", lambda _mask: None)
    levels = []
    calls = []
    app = object()
    monkeypatch.setattr(entrypoint, "configure", levels.append)
    monkeypatch.setattr(entrypoint, "create_app", lambda _settings: app)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    entrypoint.main()
    assert levels == ["DEBUG"]
    assert calls[0][0] == (app,)
    assert calls[0][1]["access_log"] is False


def test_main_rejects_invalid_log_settings_before_server_start(configuration, monkeypatch):
    from media_service import __main__ as entrypoint

    monkeypatch.setattr(entrypoint.sys, "argv", ["media-service"])
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("MEDIA_LOG_LEVEL", "private-invalid-level")
    monkeypatch.setattr(entrypoint.os, "umask", lambda _mask: None)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("started"))
    with pytest.raises(SystemExit, match="Invalid private service configuration") as error:
        entrypoint.main()
    assert "private-invalid-level" not in str(error.value)


@pytest.mark.parametrize("planning_timeout", [75, 180, 300])
def test_model_flags_and_budgets_load_and_process_values_override(
        configuration, monkeypatch, planning_timeout):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_LLM_ENABLED=true\nMEDIA_A2A_ENABLED=true\n"
                     "MEDIA_LLM_API_KEY='isolated-model-key'\nMEDIA_LLM_MODEL=file-model\n"
                     "MEDIA_LLM_BASE_URL=https://model.invalid/v1\n"
                     f"MEDIA_PLANNING_TIMEOUT_SECONDS={planning_timeout}\n"
                     "MEDIA_PLANNING_CONCURRENCY=3\n"
                     "MEDIA_PLANNING_MAX_OUTPUT_TOKENS=2048\nMEDIA_MAX_AGENT_REQUESTS=20\n")
    monkeypatch.setenv("MEDIA_LLM_MODEL", "process-model")
    monkeypatch.setenv("MEDIA_SERVICE_TOKEN", "null")
    result = Settings.from_file()
    assert result.service_token is None and result.llm_enabled and result.a2a_enabled
    assert result.llm_api_key.get_secret_value() == "isolated-model-key"
    assert result.llm_model == "process-model"
    assert result.planning_timeout_seconds == planning_timeout and result.planning_concurrency == 3
    assert result.planning_max_output_tokens == 2048 and result.max_agent_requests == 20


@pytest.mark.parametrize("timeout", ["0", "-1", "300.01"])
def test_out_of_range_planning_timeout_is_rejected(configuration, timeout):
    _, path = configuration
    with path.open("a") as stream:
        stream.write(f"MEDIA_PLANNING_TIMEOUT_SECONDS={timeout}\n")
    with pytest.raises(ValueError, match="invalid local environment configuration"):
        Settings.from_file()


@pytest.mark.parametrize("field", sorted(ENV_ONLY_FIELDS))
def test_json_cannot_remain_a_second_source(configuration, field):
    path, _ = configuration
    content = json.loads(path.read_text())
    content[field] = "private-value-never-printed"
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="move environment-only settings") as error:
        Settings.from_file()
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("token", ["", "short", "NULL", "replace-with-generated-token",
                                 "REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_24_CHARACTERS",
                                 "x" * 24 + " "])
def test_invalid_token_never_silently_opens_service(configuration, monkeypatch, token):
    monkeypatch.setenv("MEDIA_SERVICE_TOKEN", token)
    with pytest.raises(ValueError, match="invalid MEDIA_SERVICE_TOKEN"):
        Settings.from_file()


def test_missing_token_does_not_load_old_env_local(configuration):
    _, path = configuration
    path.write_text(path.read_text().splitlines()[0] + "\n")
    path.with_name(".env.local").write_text(f"MEDIA_SERVICE_TOKEN={TOKEN}\n")
    with pytest.raises(ValueError, match="MEDIA_SERVICE_TOKEN is required"):
        Settings.from_file()


def test_explicit_file_selection_and_missing_file_failure(configuration, monkeypatch):
    _, path = configuration
    selected = path.with_name("selected.env")
    selected.write_text(path.read_text().replace(TOKEN, "null"))
    monkeypatch.setenv("MEDIA_ENV_FILE", str(selected))
    assert Settings.from_file().service_token is None
    selected.unlink()
    with pytest.raises(ValueError, match="cannot read local environment"):
        Settings.from_file()


def test_process_only_container_configuration(configuration, monkeypatch):
    config, path = configuration
    path.unlink()
    monkeypatch.setenv("MEDIA_CONFIG_FILE", str(config))
    monkeypatch.setenv("MEDIA_SERVICE_TOKEN", TOKEN)
    assert Settings.from_file().service_token.get_secret_value() == TOKEN


def test_key_and_token_are_redacted_in_parsed_settings(configuration):
    _, path = configuration
    values = load_local_env(path, environ={})
    assert TOKEN not in repr(values)


@pytest.mark.parametrize("value", ["null", "123", '"quoted-key"'])
def test_model_credentials_are_opaque_strings(configuration, value):
    _, path = configuration
    with path.open("a") as stream:
        stream.write(f"MEDIA_LLM_API_KEY='{value}'\nMEDIA_LLM_MODEL='{value}'\n")
    result = Settings.from_file()
    assert result.llm_api_key.get_secret_value() == value
    assert result.llm_model == value


def test_parser_rejects_ambiguous_duplicates_without_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MEDIA_LLM_API_KEY=private-one\nMEDIA_LLM_API_KEY=private-two\n")
    with pytest.raises(ValueError, match="duplicate") as error:
        read_env_file(path)
    assert "private-" not in str(error.value)


def test_feature_combination_and_missing_key_fail_closed(configuration):
    _, path = configuration
    with path.open("a") as stream:
        stream.write("MEDIA_A2A_ENABLED=true\n")
    with pytest.raises(ValueError, match="A2A requires natural language"):
        Settings.from_file()
    with path.open("a") as stream:
        stream.write("MEDIA_LLM_ENABLED=true\nMEDIA_LLM_API_KEY=\n")
    with pytest.raises(ValueError, match="planning requires a model API key"):
        Settings.from_file()
