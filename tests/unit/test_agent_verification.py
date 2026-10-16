import importlib.util
import io
import json
import logging
import stat
from pathlib import Path

import pytest
from PIL import Image
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("agent_verification", ROOT / "deploy/verify-agent.py")
assert SPEC and SPEC.loader
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


@pytest.fixture(autouse=True)
def restore_logging_after_cli():
    previous = logging.root.manager.disable
    yield
    logging.disable(previous)


def model_configuration():
    return {
        "llm_api_key": SecretStr("isolated-verification-key-no-provider-access"),
        "llm_model": "offline-model", "llm_base_url": "https://provider.invalid/v1",
    }


def test_local_model_file_is_explicit_and_cannot_change_isolation(tmp_path, monkeypatch):
    environment = tmp_path / ".env.local"
    environment.write_text(
        'llm_api_key="isolated-file-key-no-provider-access"\n'
        'llm_default_model="offline-model"\n'
        'llm_base_url="https://provider.invalid/v1"\n'
        'MEDIA_WORK_DIR="/unrelated/private-path"\n'
        'MEDIA_SERVICE_TOKEN="not-the-generated-service-token"\n'
        'MEDIA_LLM_ENABLED=false\n'
        'llm_provider="ignored-provider-label"\n'
    )
    monkeypatch.setenv("MEDIA_LLM_API_KEY", "unrelated-host-key")
    model = verification.read_model_settings(environment)
    assert set(model) == {"llm_api_key", "llm_model", "llm_base_url"}
    assert model["llm_api_key"].get_secret_value() == "isolated-file-key-no-provider-access"
    config = verification.private_configuration(
        "isolated-generated-token-with-enough-characters", "http://127.0.0.1:18888", model,
    )
    assert config["work_dir"] == "/var/lib/ffmpeg-agent/work"
    assert config["llm_enabled"] is True and config["a2a_enabled"] is True
    assert config["planning_concurrency"] == 1
    assert config["callers"]["demo"]["source_rules"] == []
    assert config["engine_series"] == "7.1" and config["runtime_mode"] == "container"


def test_private_file_is_readable_to_container_without_keys_in_arguments():
    model = model_configuration()
    harness = verification.Harness("sha256:" + "1" * 64, model)
    try:
        harness.base = "http://127.0.0.1:18888"
        config = harness.write_private_configuration()
        assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(config.stat().st_mode) == 0o644
        data = json.loads(config.read_text())
        assert "llm_api_key" not in data and "service_token" not in data
        environment = config.with_suffix(".env")
        assert stat.S_IMODE(environment.stat().st_mode) == 0o644
        values = verification.shared.env_support.read_env_file(environment)
        assert values["MEDIA_LLM_API_KEY"] == model["llm_api_key"].get_secret_value()
        assert values["MEDIA_SERVICE_TOKEN"] == harness.token
        arguments = harness.container_arguments(config)
        assert harness.monitor.observe(" ".join(arguments), "arguments")
        assert f"type=bind,src={config},dst=/run/secrets/media-config.json,readonly" in arguments
        assert f"type=bind,src={environment},dst=/run/secrets/media.env,readonly" in arguments
        assert "MEDIA_ENV_FILE=/run/secrets/media.env" in arguments
        assert "MEDIA_TEST_ENGINE_SERIES=7.1" in arguments
        assert "127.0.0.1:18888:8080" in arguments
        assert arguments[-1] == harness.image
        assert "--read-only" in arguments and "--init" in arguments
        assert not any("/media/input" in item or "/fault-tests" in item for item in arguments)
    finally:
        harness.temp.cleanup()


def test_disclosure_checks_are_sticky_and_never_include_needles():
    secret = "private-test-key-甲"
    monitor = verification.DisclosureMonitor([secret, "private-test-token"])
    assert monitor.observe({"status": "succeeded"})
    assert not monitor.observe(json.dumps({"data": secret}, ensure_ascii=True))
    assert monitor.observe(b"safe next response")
    assert monitor.checks["public_responses"] is False
    assert monitor.observe(b"safe log", "container_logs")
    assert secret not in json.dumps(monitor.checks)
    assert "private-test-token" not in json.dumps(monitor.checks)


@pytest.mark.parametrize("dimensions,operation", [
    ((80, 107), "image.resize.v1"), ((80, 45), "video.extract_frames.v1"),
])
def test_offline_decoding_verifies_actual_image_dimensions(dimensions, operation):
    data = io.BytesIO()
    Image.new("RGB", dimensions, color="orange").save(data, format="PNG")
    result = verification.inspect_bytes("unused-container", data.getvalue(), operation)
    assert result == {"format": "PNG", "width": dimensions[0], "height": dimensions[1]}
    wrong = io.BytesIO()
    Image.new("RGB", (80, 80)).save(wrong, format="PNG")
    with pytest.raises(verification.VerificationFailure, match="image_aspect_ratio_mismatch"):
        verification.inspect_bytes("unused-container", wrong.getvalue(), operation)


def test_offline_audio_validation_checks_decoded_properties(monkeypatch):
    metadata = {"streams": [{"codec_type": "audio", "codec_name": "flac",
                             "sample_rate": "48000", "channels": 1}]}
    monkeypatch.setattr(verification.parameters, "probe_bytes", lambda *args: metadata)
    assert verification.inspect_bytes("unused", b"fixture", "audio.transcode.v1") == {
        "codec": "flac", "sample_rate": 48000, "channels": 1,
    }
    metadata["streams"][0]["sample_rate"] = "22050"
    with pytest.raises(verification.VerificationFailure, match="audio_encoding_mismatch"):
        verification.inspect_bytes("unused", b"fixture", "audio.transcode.v1")


def test_report_destination_cannot_replace_env_file(tmp_path, monkeypatch, capsys):
    environment = tmp_path / ".env.local"
    original = 'llm_api_key="isolated-do-not-overwrite"\n'
    environment.write_text(original)

    def not_called(*args, **kwargs):
        raise AssertionError("configuration must not be read")

    monkeypatch.setattr(verification, "read_model_settings", not_called)
    code = verification.main(["--image", "test:tag", "--env-file", str(environment),
                              "--report", str(environment)])
    assert code == 1
    assert environment.read_text() == original
    captured = capsys.readouterr()
    assert "isolated-do-not-overwrite" not in captured.out + captured.err


@pytest.mark.parametrize("leak", [False, True])
def test_cli_offline_summary_and_report_never_include_model_key(tmp_path, monkeypatch, capsys, leak):
    model = model_configuration()
    key = model["llm_api_key"].get_secret_value()
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(verification, "read_model_settings", lambda path: model)

    async def offline_verify(image, received_model, report):
        assert image == "offline:test" and received_model is model
        report["cases"].append({"name": "offline", "passed": True})
        report["status"] = "passed"
        if leak:
            report["unexpected_response"] = key

    monkeypatch.setattr(verification, "verify", offline_verify)
    result = verification.main(["--image", "offline:test", "--env-file", str(tmp_path / "unused"),
                                "--report", str(report_path)])
    assert result == (1 if leak else 0)
    captured = capsys.readouterr()
    serialized = report_path.read_text()
    assert key not in serialized + captured.out + captured.err
    assert model["llm_base_url"] not in serialized + captured.out + captured.err
    assert json.loads(serialized)["status"] == ("failed" if leak else "passed")
    assert "unexpected_response" not in serialized
    if not leak:
        assert json.loads(serialized)["planning_timeout_seconds"] == 60
        assert len(json.loads(serialized)["selected_cases"]) == 6


def test_cli_external_error_is_replaced_with_safe_stage(tmp_path, monkeypatch, capsys):
    secret = "private-external-exception-payload"
    monkeypatch.setattr(verification, "read_model_settings", lambda path: model_configuration())

    async def failing(*args):
        raise RuntimeError(secret)

    monkeypatch.setattr(verification, "verify", failing)
    report_path = tmp_path / "error.json"
    assert verification.main(["--image", "offline:test", "--report", str(report_path)]) == 1
    captured = capsys.readouterr()
    report = json.loads(report_path.read_text())
    assert report["error_code"] == "VERIFICATION_FAILED" and report["failed_stage"] == "image_preflight"
    assert secret not in captured.out + captured.err + report_path.read_text()
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("value", ["0", "-1", "300.1", "nan", "inf", "not-a-number"])
def test_planning_timeout_cli_rejects_unbounded_values(value):
    with pytest.raises(SystemExit) as error:
        verification.parse_args(["--image", "offline:test", "--planning-timeout-seconds", value])
    assert error.value.code == 2


def test_single_case_timeout_override_reaches_private_configuration_and_report(
    tmp_path, monkeypatch, capsys,
):
    model = model_configuration() | {"planning_timeout_seconds": 60}
    monkeypatch.setattr(verification, "read_model_settings", lambda path: model)

    async def offline_verify(image, received_model, report):
        selected = verification.selected_cases(report["selected_cases"])
        assert len(selected) == 1 and selected[0]["name"] == "a2a_video"
        assert received_model["planning_timeout_seconds"] == 180
        config = verification.private_configuration(
            "isolated-generated-token-with-enough-characters", "http://127.0.0.1:18888", received_model,
        )
        assert config["planning_timeout_seconds"] == 180
        report["cases"].append({"name": selected[0]["name"], "passed": True})
        report["status"] = "passed"

    monkeypatch.setattr(verification, "verify", offline_verify)
    report_path = tmp_path / "single-case.json"
    assert verification.main([
        "--image", "offline:test", "--case", "a2a_video", "--planning-timeout-seconds", "180",
        "--report", str(report_path),
    ]) == 0
    report = json.loads(report_path.read_text())
    assert report["selected_cases"] == ["a2a_video"]
    assert report["planning_timeout_seconds"] == 180
    assert len(report["cases"]) == 1
    assert json.loads(capsys.readouterr().out)["cases_total"] == 1


def test_case_selection_and_timeout_upper_bound():
    assert len(verification.selected_cases()) == 6
    assert verification.parse_args([
        "--image", "offline:test", "--planning-timeout-seconds", "300",
    ]).planning_timeout_seconds == 300
    with pytest.raises(verification.VerificationFailure, match="invalid_case_selection"):
        verification.selected_cases(["missing-case"])
    with pytest.raises(SystemExit):
        verification.parse_args(["--image", "offline:test", "--case", "missing-case"])
