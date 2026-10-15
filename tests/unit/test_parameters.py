import json

import pytest
from pydantic import ValidationError

from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.media import MediaEngine
from media_service.models import OPERATIONS, JobCreate, capabilities
from media_service.parameters import (
    AudioParams,
    FrameRate,
    FramesParams,
    ImageParams,
    ProbeParams,
    VideoParams,
    available_parameters,
)
from media_service.service import Service


def definition(operation, params):
    port, output, _, _ = OPERATIONS[operation]
    return JobCreate.model_validate({
        "inputs": {"main": {"kind": "shared_file", "storage_id": "input", "key": "sample"}},
        "steps": [{"id": "convert", "operation": operation,
                   "inputs": {port: {"source": "main"}}, "params": params}],
        "outputs": {"result": {"step": "convert", "output": output}},
    })


@pytest.mark.parametrize("operation,params", [
    ("media.probe.v1", {}),
    ("media.probe.v1", {"sections": ["streams", "format", "streams"], "stream_indices": [1, 0, 1]}),
    ("video.transcode.v1", {}),
    ("video.transcode.v1", {"preset": "web_720p_v1", "audio": {"channel_layout": "mono"}}),
    ("video.transcode.v1", {"codec": "vp9"}),
    ("video.transcode.v1", {"codec": "av1", "audio": {"mode": "drop"}}),
    ("video.transcode.v1", {"resize": {}, "frame_rate": {"mode": "source"}}),
    ("video.transcode.v1", {"rate_control": {"mode": "bitrate", "bitrate_bps": 500000}}),
    ("video.transcode.v1", {"audio": {"mode": "copy"}}),
    ("audio.transcode.v1", {}),
    ("audio.transcode.v1", {"preset": "wav_mono_16k_v1"}),
    ("audio.transcode.v1", {"preset": "wav_mono_16k_v1", "codec": "pcm"}),
    ("audio.transcode.v1", {"codec": "mp3", "rate_control": {"mode": "quality", "value": 2}}),
    ("audio.transcode.v1", {"codec": "flac"}),
    ("audio.transcode.v1", {"codec": "mp3", "sample_rate": "source", "channels": "source"}),
    ("audio.transcode.v1", {"preset": "wav_mono_16k_v1", "channel_layout": "stereo"}),
    ("audio.transcode.v1", {"codec": "pcm", "sample_format": "f32"}),
    ("video.extract_audio.v1", {"mode": "copy", "container": "m4a"}),
    ("video.extract_frames.v1", {"sampling": {"mode": "timestamps", "timestamps_ms": [100, 0, 100]}}),
    ("video.extract_frames.v1", {"sampling": {"mode": "interval", "interval_ms": 200}}),
    ("video.extract_frames.v1", {"sampling": {"mode": "count", "count": 10}, "image": {"format": "png"}}),
    ("image.resize.v1", {}),
    ("image.resize.v1", {"format": "png", "width": 100}),
    ("image.resize.v1", {"format": "webp", "lossless": True}),
])
def test_normalized_definitions_roundtrip_without_resetting_defaults(operation, params):
    request = definition(operation, params)
    assert JobCreate.model_validate_json(request.canonical()).canonical() == request.canonical()
    effective = request.steps[0].params
    assert definition(operation, effective).digest() == request.digest()


def test_explicit_presets_and_codec_changes_rebuild_related_defaults():
    video = VideoParams.model_validate({"preset": "web_720p_v1"}).model_dump()
    assert (video["codec"], video["resize"]["width"], video["resize"]["height"]) == ("h264", 1280, 720)
    assert video["frame_rate"] == {"mode": "constant", "value": "30/1"}
    assert video["rate_control"] == {"mode": "crf", "value": 23}
    audio = video["audio"]
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("aac", 48000, 2)
    pcm = AudioParams.model_validate({"preset": "wav_mono_16k_v1", "codec": "pcm"})
    assert (pcm.codec, pcm.sample_rate, pcm.channels, pcm.sample_format) == ("pcm", 16000, 1, "s16")
    assert pcm.rate_control is None
    webm = VideoParams.model_validate({"codec": "vp9"})
    assert webm.container == "webm" and webm.audio.codec == "opus"
    assert webm.audio.rate_control.model_dump() == {"mode": "auto"}


@pytest.mark.parametrize("model,params", [
    (VideoParams, {"codec": "vp9", "container": "mp4"}),
    (VideoParams, {"codec": "av1", "rate_control": {"mode": "crf", "value": 32.5}}),
    (VideoParams, {"codec": "h264", "rate_control": {"mode": "crf", "value": 52}}),
    (VideoParams, {"codec": "av1", "rate_control": {"mode": "bitrate", "bitrate_bps": 100000,
                                                "maxrate_bps": 200000, "buffer_size_bits": 300000}}),
    (VideoParams, {"codec": "vp9", "audio": {"codec": "aac"}}),
    (VideoParams, {"rate_control": {"mode": "bitrate", "bitrate_bps": 100000, "value": 23}}),
    (VideoParams, {"rate_control": {"mode": "bitrate", "bitrate_bps": 100000, "maxrate_bps": 200000}}),
    (VideoParams, {"audio": {"mode": "copy", "sample_rate": 16000}}),
    (VideoParams, {"audio": {"mode": "copy"}, "range": {"start_ms": 100}}),
    (VideoParams, {"resize": {"fit": "cover", "width": 101, "height": 100}}),
    (VideoParams, {"resize": {"fit": "pad", "width": 100}}),
    (VideoParams, {"pixel_format": "yuv420p10le"}),
    (VideoParams, {"hardware": "auto"}),
    (VideoParams, {"preset": "web_720p_v1", "audio": {"range": {"start_ms": 1000}}}),
    (VideoParams, {"preset": "web_720p_v1", "audio": {"container": "mka"}}),
    (VideoParams, {"preset": "web_720p_v1", "audio": {"preset": "wav_mono_16k_v1"}}),
    (VideoParams, {"frame_rate": {"mode": "source", "value": 30}}),
    (VideoParams, {"frame_rate": {"mode": "constant", "value": "1/0"}}),
    (VideoParams, {"frame_rate": {"mode": "constant", "value": "nan"}}),
    (VideoParams, {"range": {"start_ms": 1000, "end_ms": 999}}),
    (AudioParams, {"mode": "drop"}),
    (AudioParams, {"mode": "copy", "rate_control": {"mode": "bitrate", "bitrate_bps": 128000}}),
    (AudioParams, {"mode": "copy", "range": {"end_ms": 1000}}),
    (AudioParams, {"mode": "copy", "preset": "wav_mono_16k_v1"}),
    (AudioParams, {"codec": "flac", "rate_control": {"mode": "bitrate", "bitrate_bps": 128000}}),
    (AudioParams, {"codec": "mp3", "container": "m4a"}),
    (AudioParams, {"codec": "mp3", "channels": 6}),
    (AudioParams, {"codec": "opus", "sample_rate": 16000}),
    (AudioParams, {"codec": "opus", "channel_layout": "2.1"}),
    (AudioParams, {"codec": "opus", "channels": 4}),
    (AudioParams, {"codec": "opus", "rate_control": {"mode": "quality", "value": 2}}),
    (AudioParams, {"codec": "aac", "sample_format": "s24"}),
    (AudioParams, {"codec": "aac", "channel_layout": "mono", "channels": 2}),
    (AudioParams, {"codec": "aac", "rate_control": {"mode": "quality", "value": 0}}),
    (ImageParams, {"format": "png", "quality": 90}),
    (ImageParams, {"format": "jpeg", "quality": 96}),
    (ImageParams, {"format": "webp", "progressive": False}),
    (ImageParams, {"crop_x": 0}),
    (ImageParams, {"width": True}),
    (ImageParams, {"width": 0}),
    (FramesParams, {"sampling": {"mode": "count", "count": 2, "interval_ms": 1}}),
    (FramesParams, {"sampling": {"mode": "interval", "interval_ms": 0}}),
    (ProbeParams, {"sections": ["format"], "count_frames": True}),
    (ProbeParams, {"sections": ["filename"]}),
])
def test_conflicting_or_inapplicable_parameters_are_rejected(model, params):
    with pytest.raises(ValidationError):
        model.model_validate(params)


def test_rational_frame_rate_is_canonical_without_losing_precision():
    assert FrameRate.model_validate({"value": "60000/2002"}).value == "30000/1001"
    assert FrameRate.model_validate({"value": 29.97}).value == "2997/100"


@pytest.mark.parametrize("value", ["1e10000000", "1e-10000000", "1000000000/1",
                                  "1/1000000000", 1e100, float("inf"), float("nan")])
def test_frame_rate_rejects_unbounded_expressions(value):
    with pytest.raises(ValidationError):
        FrameRate.model_validate({"value": value})


@pytest.mark.parametrize("value", [[], {}, ["aac"]])
@pytest.mark.parametrize("model,field", [(AudioParams, "codec"), (AudioParams, "channel_layout"),
                                        (ImageParams, "format"), (VideoParams, "codec")])
def test_malformed_discriminants_are_validation_errors(model, field, value):
    with pytest.raises(ValidationError):
        model.model_validate({field: value})


def test_preset_defaults_yield_to_explicit_channel_layout():
    audio = AudioParams.model_validate({"preset": "wav_mono_16k_v1", "channel_layout": "stereo"})
    assert (audio.sample_rate, audio.channels, audio.channel_layout) == (16000, 2, "stereo")


def test_audio_defaults_resolve_compatibility_without_overriding_explicit_bitrate():
    mp3 = AudioParams.model_validate({"codec": "mp3", "sample_rate": 8000})
    assert mp3.rate_control.model_dump() == {"mode": "auto"}
    aac = AudioParams.model_validate({"sample_rate": 8000, "channels": 1})
    assert aac.rate_control.model_dump() == {"mode": "auto"}
    source = AudioParams.model_validate({"codec": "mp3", "sample_rate": "source"})
    assert source.rate_control.model_dump() == {"mode": "auto"}
    for params in ({"codec": "mp3", "sample_rate": 8000, "channels": 1}, {"sample_rate": 8000, "channels": 1}):
        with pytest.raises(ValidationError):
            AudioParams.model_validate(params | {"rate_control": {"mode": "bitrate", "bitrate_bps": 192000}})


def test_preserves_sampling_order_but_normalizes_probe_sets():
    params = {"sampling": {"mode": "timestamps", "timestamps_ms": [2, 0, 2]}}
    assert FramesParams.model_validate(params).sampling.timestamps_ms == [2, 0, 2]
    assert ProbeParams.model_validate({"sections": ["streams", "format"]}).sections == ["format", "streams"]


def test_single_contract_accepts_empty_parameters_for_all_six_capabilities():
    assert len(OPERATIONS) == 6
    for operation in OPERATIONS:
        request = definition(operation, {})
        assert request.steps[0].params == OPERATIONS[operation][2]().model_dump()
        assert capabilities()[operation]["default_params"] == request.steps[0].params
    video = VideoParams()
    assert video.preset is None and video.resize.width is None and video.resize.height is None
    assert video.frame_rate.mode == "source"
    assert video.audio.sample_rate == video.audio.channels == "auto"
    assert FramesParams().sampling.timestamps_ms == [0]
    assert ImageParams().quality == 90


def test_capabilities_do_not_claim_missing_optional_encoders():
    result = available_parameters({"available_encoders": ["libx264", "aac"],
                                   "available_muxers": ["mp4", "ipod"]})
    assert result["video_codecs"] == {"h264": "libx264"}
    assert result["video_containers"] == {"mp4": ["h264"]}
    assert result["audio_codecs"] == {"aac": "aac"}
    assert "hevc" not in json.dumps(result["video_codecs"])


def test_service_limits_do_not_change_schema(settings):
    params = FramesParams.model_validate({"sampling": {"mode": "count", "count": 101}}).model_dump()
    engine = MediaEngine(settings, Executor(settings))
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        engine.validate_parameters("video.extract_frames.v1", params, {})
    settings.max_frame_count = 200
    engine.validate_parameters("video.extract_frames.v1", params, {})


def test_unavailable_encoder_rejected_before_acceptance(settings):
    engine = MediaEngine(settings, Executor(settings))
    with pytest.raises(ServiceError, match="UNSUPPORTED_OPERATION"):
        engine.validate_request(definition("video.transcode.v1", {"codec": "hevc"}),
                                {"available_encoders": ["libx264"], "available_muxers": ["mp4"]})


async def test_persisted_replay_keeps_parameters_despite_new_runtime_limits(settings):
    settings.work_dir.mkdir()
    request = definition("video.extract_frames.v1", {"sampling": {"mode": "count", "count": 10}})
    first = Service(settings)
    await first.db.open()
    first.ready = True
    try:
        job_id, created = await first.accept("test", "parameters-frozen-parameters", request)
        assert created
    finally:
        await first.db.close()
    settings.max_frame_count = 5
    restarted = Service(settings)
    await restarted.db.open()
    restarted.ready = True
    try:
        assert await restarted.accept("test", "parameters-frozen-parameters", request) == (job_id, False)
        job = await restarted.state.job("test", job_id)
        assert job["steps"][0]["effective_params"] == request.steps[0].params
        with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
            await restarted.accept("test", "parameters-new-definition", request)
    finally:
        await restarted.db.close()
