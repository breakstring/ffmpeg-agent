import importlib.util
import json
import stat
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("config_generator", ROOT / "deploy/generate-config.py")
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def generate(tmp_path):
    environment = tmp_path / ".env"
    output = tmp_path / "private" / "media-config.json"
    generator.generate(environment, output, "http://localhost:18888")
    return environment, output


def test_new_environment_has_unique_token_and_repeat_is_idempotent(tmp_path, capsys):
    environment, output = generate(tmp_path)
    before_env, before_json = environment.read_bytes(), output.read_bytes()
    values = generator.env_support.read_env_file(environment)
    token = values["MEDIA_SERVICE_TOKEN"]
    assert values["MEDIA_PLANNING_TIMEOUT_SECONDS"] == "300"
    assert values["MEDIA_LOG_LEVEL"] == "INFO"
    assert values["MEDIA_LOG_MODEL_CONTENT"] == "false"
    assert values["MEDIA_LLM_ENABLE_THINKING"] == "false"
    assert len(token) >= 24 and not generator.is_placeholder(token)
    assert stat.S_IMODE(environment.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert not generator.env_support.ENV_ONLY_FIELDS.intersection(json.loads(before_json))
    assert json.loads(before_json)["public_base_url"] == "http://localhost:18888"
    generate(tmp_path)
    assert environment.read_bytes() == before_env
    assert output.read_bytes() == before_json
    assert token not in capsys.readouterr().out


def test_migrate_old_json_preserves_explicit_environment_and_comments(tmp_path, capsys):
    environment = tmp_path / ".env"
    original = "# retain this\nMEDIA_SERVICE_TOKEN='isolated-env-token-with-24-characters'\nCUSTOM=unchanged\n"
    environment.write_text(original)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "media-config.json"
    output.write_text(json.dumps({
        "service_token": "isolated-json-token-with-24-characters",
        "llm_enabled": True, "a2a_enabled": True, "llm_api_key": "isolated-migrated-key",
        "llm_model": "test-model", "planning_concurrency": 1, "state_dir": "/state",
    }))
    generate(tmp_path)
    text = environment.read_text()
    assert text.startswith(original)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_LLM_API_KEY"] == "isolated-migrated-key"
    assert values["MEDIA_LLM_ENABLED"] == "true"
    assert json.loads(output.read_text()) == {"state_dir": "/state"}
    printed = capsys.readouterr().out
    assert "已有 .env 配置优先" in printed
    assert "isolated-" not in printed


def test_old_json_null_preserves_open_mode_without_new_random_token(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "media-config.json").write_text(json.dumps({"service_token": None, "llm_enabled": True}))
    environment, _ = generate(tmp_path)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_SERVICE_TOKEN"] == "null"
    assert values["MEDIA_LLM_ENABLED"] == "true"


def test_migration_recovers_when_json_write_is_interrupted(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "media-config.json"
    old = {"service_token": "isolated-old-token-with-24-characters", "llm_api_key": "isolated-old-key"}
    output.write_text(json.dumps(old))
    original_write = generator.env_support.atomic_write

    def interrupted(path, content, mode):
        if path == output:
            raise OSError("isolated failure")
        original_write(path, content, mode)

    monkeypatch.setattr(generator.env_support, "atomic_write", interrupted)
    with pytest.raises(OSError):
        generate(tmp_path)
    assert json.loads(output.read_text()) == old
    before = (tmp_path / ".env").read_bytes()
    monkeypatch.setattr(generator.env_support, "atomic_write", original_write)
    environment, _ = generate(tmp_path)
    assert environment.read_bytes() == before
    assert json.loads(output.read_text()) == {}


def test_runtime_configuration_keeps_secrets_only_in_env_and_round_trips_special_values(tmp_path):
    tmp_path.chmod(0o700)
    target = tmp_path / "config.json"
    key = "isolated-$key\\with'quote"
    environment = generator.env_support.write_runtime_configuration({
        "llm_api_key": key, "service_token": None, "a2a_enabled": False, "state_dir": "/state",
    }, target)
    assert json.loads(target.read_text()) == {"state_dir": "/state"}
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_LLM_API_KEY"] == key
    assert values["MEDIA_SERVICE_TOKEN"] == "null"
    generator.env_support.write_runtime_configuration({"llm_enabled": True}, target)
    assert generator.env_support.read_env_file(environment)["MEDIA_LLM_API_KEY"] == key
    assert key not in " ".join(generator.env_support.environment_mount(environment))


def test_explicit_public_url_and_output_are_the_effective_environment(tmp_path):
    environment, output = generate(tmp_path)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_PUBLIC_BASE_URL"] == "http://localhost:18888"
    assert values["MEDIA_CONFIG_PATH"] == str(output.resolve())
    generator.generate(environment)
    assert generator.env_support.read_env_file(environment) == values


def test_legacy_environment_gains_defaults_without_overwriting_alias_or_custom_values(tmp_path):
    environment = tmp_path / ".env"
    environment.write_text(
        "# preserve comment\nllm_api_key='isolated-existing-key'\n"
        "llm_default_model='existing-model'\nllm_base_url='https://existing.invalid/v1'\n"
        "MEDIA_LLM_ENABLED=true\nMEDIA_PUBLIC_BASE_URL=https://existing-service.invalid\n"
        "MEDIA_PLANNING_CONCURRENCY=1\nMEDIA_PLANNING_TIMEOUT_SECONDS=60\n"
        "MEDIA_LOG_LEVEL=DEBUG\nMEDIA_LOG_MODEL_CONTENT=true\n"
    )
    output = tmp_path / "private/config.json"
    generator.generate(environment, output)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_LLM_API_KEY"] == "isolated-existing-key"
    assert values["MEDIA_LLM_MODEL"] == "existing-model"
    assert values["MEDIA_LLM_BASE_URL"] == "https://existing.invalid/v1"
    assert values["MEDIA_LLM_ENABLED"] == "true"
    assert values["MEDIA_PUBLIC_BASE_URL"] == "https://existing-service.invalid"
    assert values["MEDIA_PLANNING_CONCURRENCY"] == "1"
    assert values["MEDIA_PLANNING_TIMEOUT_SECONDS"] == "60"
    assert values["MEDIA_LOG_LEVEL"] == "DEBUG"
    assert values["MEDIA_LOG_MODEL_CONTENT"] == "true"
    assert values["MEDIA_A2A_ENABLED"] == "false"
    assert values["MEDIA_PLANNING_MAX_OUTPUT_TOKENS"] == "4096"
    assert environment.read_text().startswith("# preserve comment\n")


def test_generator_migrates_log_options_from_json_into_environment(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    output.write_text(json.dumps({
        "log_level": "DEBUG", "log_model_content": True, "state_dir": "/state",
    }))
    environment = tmp_path / ".env"
    generator.generate(environment, output)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_LOG_LEVEL"] == "DEBUG"
    assert values["MEDIA_LOG_MODEL_CONTENT"] == "true"
    assert json.loads(output.read_text()) == {"state_dir": "/state"}


@pytest.mark.parametrize("value,expected", [(False, "false"), (True, "true"), (None, "null")])
def test_generator_migrates_thinking_option_without_losing_null(tmp_path, value, expected):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    output.write_text(json.dumps({"llm_enable_thinking": value, "state_dir": "/state"}))
    environment = tmp_path / ".env"
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_LLM_ENABLE_THINKING"] == expected
    assert json.loads(output.read_text()) == {"state_dir": "/state"}


def test_generator_preserves_explicit_null_thinking_setting(tmp_path):
    environment = tmp_path / ".env"
    environment.write_text("MEDIA_LLM_ENABLE_THINKING=null\n")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    output.write_text(json.dumps({"llm_enable_thinking": False}))
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_LLM_ENABLE_THINKING"] == "null"
    assert json.loads(output.read_text()) == {}


def test_migration_preserves_old_public_url_without_explicit_cli_override(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    output.write_text(json.dumps({
        "public_base_url": "https://original-service.invalid",
        "service_token": "isolated-original-service-token",
    }))
    environment = tmp_path / ".env"
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_PUBLIC_BASE_URL"] == "https://original-service.invalid"
    generator.generate(environment, output, "https://explicit-new-service.invalid/")
    assert generator.env_support.read_env_file(environment)["MEDIA_PUBLIC_BASE_URL"] == "https://explicit-new-service.invalid"


def test_legacy_caller_first_token_migrates_and_all_json_credentials_are_removed(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    first = "isolated-first-legacy-token-24-characters"
    output.write_text(json.dumps({
        "service_caller": "selected",
        "callers": {"selected": {"tokens": [first, "isolated-second-legacy-token"]},
                    "other": {"tokens": ["isolated-other-legacy-token"]}},
    }))
    environment = tmp_path / ".env"
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_SERVICE_TOKEN"] == first
    assert all("tokens" not in account for account in json.loads(output.read_text())["callers"].values())
    assert "isolated-" not in output.read_text()


def test_ambiguous_legacy_callers_fail_before_any_write(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    original = json.dumps({"callers": {"one": {"tokens": ["isolated-first-service-token"]},
                                      "two": {"tokens": ["isolated-second-service-token"]}}})
    output.write_text(original)
    environment = tmp_path / ".env"
    with pytest.raises(ValueError, match="explicit service_caller"):
        generator.generate(environment, output)
    assert not environment.exists()
    assert output.read_text() == original


@pytest.mark.parametrize("value", [
    "fakecanary-token-abcdefghijklmnopqrstuvwxyz",
    "fakecanary\\", "fakecanary\\\\", "fakecanary\\'quote", "fakecanary\\\\'quote",
    "fakecanary$VAR", "fakecanary${VAR}", "fakecanary$$", 'fakecanary"quote',
    "fakecanary\\$VAR", "fakecanary\\${VAR}", 'fakecanary\\$$"quote',
])
def test_environment_encoding_round_trips_compose_compatible_literals(tmp_path, value):
    environment = tmp_path / "canary.env"
    environment.write_text("FAKE_VALUE=" + generator.env_support.encode_value(value) + "\n")
    assert generator.env_support.read_env_file(environment)["FAKE_VALUE"] == value
    if "\\" not in value:
        assert environment.read_text().startswith("FAKE_VALUE='")


def test_environment_encoding_rejects_control_characters():
    with pytest.raises(ValueError, match="control characters"):
        generator.env_support.encode_value("fakecanary\nother-setting")


def test_valid_token_with_replace_prefix_is_preserved(tmp_path):
    environment = tmp_path / ".env"
    token = "replaceable-but-valid-existing-service-token-12345"
    environment.write_text("MEDIA_SERVICE_TOKEN=" + token + "\n")
    output = tmp_path / "private/config.json"
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_SERVICE_TOKEN"] == token
    generator.generate(environment, output)
    assert generator.env_support.read_env_file(environment)["MEDIA_SERVICE_TOKEN"] == token


def test_copied_template_blank_model_fields_migrate_without_changing_false_switches(tmp_path):
    environment = tmp_path / ".env"
    environment.write_text((ROOT / ".env.example").read_text())
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    output.write_text(json.dumps({
        "service_token": "isolated-old-service-token-24-characters",
        "llm_api_key": "isolated-old-model-key",
        "llm_model": "isolated-old-model",
        "llm_base_url": "https://old-provider.invalid/v1",
        "llm_enabled": True, "a2a_enabled": True,
    }))
    generator.generate(environment, output)
    values = generator.env_support.read_env_file(environment)
    assert values["MEDIA_SERVICE_TOKEN"] == "isolated-old-service-token-24-characters"
    assert values["MEDIA_LLM_API_KEY"] == "isolated-old-model-key"
    assert values["MEDIA_LLM_MODEL"] == "isolated-old-model"
    assert values["MEDIA_LLM_BASE_URL"] == "https://old-provider.invalid/v1"
    assert values["MEDIA_LLM_ENABLED"] == values["MEDIA_A2A_ENABLED"] == "false"
    assert not generator.env_support.ENV_ONLY_FIELDS.intersection(json.loads(output.read_text()))


def test_explicit_empty_service_token_fails_before_migration_writes(tmp_path):
    environment = tmp_path / ".env"
    original_env = "MEDIA_SERVICE_TOKEN=\n"
    environment.write_text(original_env)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "config.json"
    original_json = json.dumps({"service_token": "isolated-old-service-token-24-characters"})
    output.write_text(original_json)
    with pytest.raises(ValueError, match="service token must be explicit"):
        generator.generate(environment, output)
    assert environment.read_text() == original_env
    assert output.read_text() == original_json
