import asyncio
import json
import os
import time
from fractions import Fraction
from pathlib import Path

import pytest

from media_service.av import run_audio, run_video
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import safe_open
from media_service.media import MediaEngine, input_args
from media_service.parameters import AudioParams, VideoParams

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def engine(settings):
    settings.work_dir.mkdir()
    return MediaEngine(settings, Executor(settings))


async def run(engine: MediaEngine, name: str | Path, params: dict, operation: str = "video"):
    directory = engine.settings.work_dir / str(time.monotonic_ns())
    directory.mkdir()
    source = FIXTURES / name if isinstance(name, str) else name
    fd = safe_open(source.parent, source.name, engine.settings.max_input_bytes)
    try:
        deadline, cancel = time.time() + 120, asyncio.Event()
        report = await engine.probe(fd, deadline, cancel)
        if operation == "video":
            result = await run_video(
                engine,
                VideoParams.model_validate(params).model_dump(),
                report,
                fd,
                directory,
                deadline,
                cancel,
            )
        else:
            result = await run_audio(
                engine,
                AudioParams.model_validate(params).model_dump(),
                report,
                fd,
                directory,
                deadline,
                cancel,
                extract=operation == "extract",
            )
        return result, directory
    finally:
        os.close(fd)


def stream(product, kind):
    return next(s for s in product.media["streams"] if s["type"] == kind)


async def test_explicit_video_preset_preserves_fixed_specs(engine):
    result, directory = await run(
        engine, "ffmpeg_high_bitrate_1080p.mp4", {"preset": "web_720p_v1"}
    )
    video, audio = stream(result, "video"), stream(result, "audio")
    assert (video["codec"], video["width"], video["height"], video["frame_rate"]) == (
        "h264",
        1280,
        720,
        "30/1",
    )
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("aac", 48000, 2)
    assert result.media["preset"] == "web_720p_v1"
    assert result.media["effective_params"]["rate_control"]["value"] == 23
    assert (directory / result.files[0]).stat().st_size > 0


async def test_default_video_preserves_dimensions_and_source_audio(engine):
    product, _ = await run(engine, "ffmpeg_high_bitrate_1080p.mp4", {})
    video, audio = stream(product, "video"), stream(product, "audio")
    assert (video["width"], video["height"], video["frame_rate"]) == (1920, 1080, "30/1")
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("aac", 44100, 1)
    assert product.media["preset"] is None
    assert product.media["effective_params"]["resolved"]["audio"]["rate_control"] == {
        "mode": "bitrate",
        "bitrate_bps": 64000,
    }


@pytest.mark.parametrize(
    "codec,container", [("h264", "mp4"), ("hevc", "mp4"), ("vp9", "webm"), ("av1", "webm")]
)
async def test_video_software_codecs_and_container_defaults(engine, codec, container):
    result, _ = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {
            "codec": codec,
            "speed": "fast",
            "resize": {"width": 320},
            "frame_rate": {"mode": "constant", "value": "30000/1001"},
            "audio": {"mode": "drop"},
        },
    )
    video = stream(result, "video")
    assert (video["codec"], video["width"], video["height"], video["pixel_format"]) == (
        codec,
        320,
        180,
        "yuv420p",
    )
    assert Fraction(video["frame_rate"]) == Fraction(30000, 1001)
    assert result.files == ["video." + container]
    assert not any(s["type"] == "audio" for s in result.media["streams"])


@pytest.mark.parametrize(
    "resize,dimensions",
    [
        ({}, (640, 360)),
        ({"height": 100}, (176, 100)),
        ({"width": 200, "height": 200, "fit": "cover", "crop_x": 0.0}, (200, 200)),
        ({"width": 800, "height": 600, "fit": "pad"}, (800, 600)),
        ({"width": 1920, "height": 1080}, (640, 360)),
    ],
)
async def test_video_dimension_modes(engine, resize, dimensions):
    result, _ = await run(engine, "ffmpeg_h264_640x360.mkv", {"resize": resize})
    video = stream(result, "video")
    assert (video["width"], video["height"]) == dimensions


async def test_odd_source_cover_does_not_upscale_during_even_rounding(engine, monkeypatch):
    source_dir = engine.settings.work_dir / "odd-source"
    source_dir.mkdir()
    source = source_dir / "odd.mkv"
    fd = safe_open(FIXTURES, "ffmpeg_h264_640x360.mkv", engine.settings.max_input_bytes)
    try:
        await engine._ffmpeg(
            fd,
            [
                "-an",
                "-frames:v",
                "1",
                "-vf",
                "scale=65:49,setsar=1",
                "-c:v",
                "ffv1",
                "-pix_fmt",
                "yuv444p",
                str(source),
            ],
            source_dir,
            time.time() + 30,
            asyncio.Event(),
        )
    finally:
        os.close(fd)
    actual_scales = []
    execute = engine._ffmpeg

    async def capture_scale(fd, options, output_dir, deadline, cancel, **kwargs):
        filter_string = options[options.index("-vf") + 1]
        scale = filter_string.split(",", 1)[0].removeprefix("scale=")
        actual_scales.append(tuple(int(value) for value in scale.split(":")[:2]))
        return await execute(fd, options, output_dir, deadline, cancel, **kwargs)

    monkeypatch.setattr(engine, "_ffmpeg", capture_scale)
    resize = {"width": 64, "height": 48, "fit": "cover"}
    product, _ = await run(engine, source, {"resize": resize})
    video = stream(product, "video")
    assert (video["width"], video["height"]) == (64, 48)
    assert actual_scales[-1][0] <= 65 and actual_scales[-1][1] <= 49
    product, _ = await run(engine, source, {"resize": resize | {"allow_upscale": True}})
    video = stream(product, "video")
    assert (video["width"], video["height"]) == (64, 48)


async def test_video_bitrate_range_and_source_frame_rate(engine):
    result, _ = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {
            "rate_control": {
                "mode": "bitrate",
                "bitrate_bps": 200000,
                "maxrate_bps": 400000,
                "buffer_size_bits": 800000,
            },
            "frame_rate": {"mode": "source"},
            "range": {"start_ms": 250, "end_ms": 750},
        },
    )
    # 源 Matroska 的毫秒时间基包含 41/42ms 间隔；裁剪后的平均值不必为整 24fps。
    assert abs(Fraction(stream(result, "video")["frame_rate"]) - 24) < Fraction(1, 10)
    assert 450 <= result.media["duration_ms"] <= 550
    assert result.media["effective_params"]["rate_control"]["mode"] == "bitrate"


@pytest.mark.parametrize("codec", ["hevc", "vp9", "av1"])
async def test_alternative_video_codecs_accept_target_bitrate(engine, codec):
    product, _ = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {
            "codec": codec,
            "speed": "fast",
            "resize": {"width": 160},
            "rate_control": {"mode": "bitrate", "bitrate_bps": 150000},
            "audio": {"mode": "drop"},
        },
    )
    assert stream(product, "video")["codec"] == codec
    assert product.media["effective_params"]["rate_control"]["bitrate_bps"] == 150000


async def test_webm_default_audio_changes_to_opus(engine):
    product, _ = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {
            "codec": "vp9",
            "speed": "fast",
            "resize": {"width": 160},
        },
    )
    audio = stream(product, "audio")
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("opus", 48000, 1)


async def test_video_max_frame_rate_drops_without_duplication(engine):
    result, directory = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {
            "frame_rate": {"mode": "max", "value": "10/1"},
            "audio": {"mode": "drop"},
        },
    )
    fd = safe_open(directory, result.files[0], engine.settings.max_output_bytes)
    try:
        timeline = await engine._timeline(fd, 0, time.time() + 30, asyncio.Event())
    finally:
        os.close(fd)
    assert len(timeline.times) == 8
    assert all(
        b - a >= Fraction(1, 10) for a, b in zip(timeline.times, timeline.times[1:], strict=False)
    )


async def test_source_frame_rate_preserves_irregular_timestamps(engine):
    source_dir = engine.settings.work_dir / "irregular"
    source_dir.mkdir()
    fd = safe_open(FIXTURES, "ffmpeg_h264_640x360.mkv", engine.settings.max_input_bytes)
    try:
        await engine._ffmpeg(
            fd,
            [
                "-an",
                "-frames:v",
                "4",
                "-vf",
                "settb=1/1000,setpts='if(eq(N,0),0,if(eq(N,1),47,if(eq(N,2),113,289)))'",
                "-c:v",
                "libx264",
                "-bf",
                "0",
                "-fps_mode",
                "passthrough",
                "-enc_time_base",
                "1:1000",
                str(source_dir / "irregular.mkv"),
            ],
            source_dir,
            time.time() + 30,
            asyncio.Event(),
        )
    finally:
        os.close(fd)
    expected = tuple(Fraction(value, 1000) for value in (0, 47, 113, 289))
    # FFmpeg 7.1 的 MP4 edit list 会遮掉此合成序列的零时长末帧，使用 Matroska 并先核验源。
    fd = safe_open(source_dir, "irregular.mkv", engine.settings.max_input_bytes)
    try:
        source_timeline = await engine._timeline(fd, 0, time.time() + 30, asyncio.Event())
    finally:
        os.close(fd)
    assert source_timeline.times == expected
    product, directory = await run(
        engine,
        source_dir / "irregular.mkv",
        {
            "resize": {},
            "audio": {"mode": "drop"},
        },
    )
    fd = safe_open(directory, product.files[0], engine.settings.max_output_bytes)
    try:
        timeline = await engine._timeline(fd, 0, time.time() + 30, asyncio.Event())
    finally:
        os.close(fd)
    assert timeline.times == expected


async def test_video_copy_audio_keeps_source_properties(engine):
    result, _ = await run(engine, "ffmpeg_h264_640x360.mkv", {"audio": {"mode": "copy"}})
    audio = stream(result, "audio")
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("aac", 44100, 1)


@pytest.mark.parametrize(
    "params,codec,rate,channels",
    [
        ({}, "aac", 22050, 1),
        ({"preset": "aac_stereo_128k_v1"}, "aac", 48000, 2),
        ({"preset": "wav_mono_16k_v1"}, "pcm_s16le", 16000, 1),
        ({"codec": "mp3"}, "mp3", 22050, 1),
        ({"codec": "opus"}, "opus", 48000, 1),
        ({"codec": "flac"}, "flac", 22050, 1),
        ({"codec": "pcm"}, "pcm_s16le", 22050, 1),
        (
            {
                "codec": "mp3",
                "sample_rate": "source",
                "channels": "source",
                "rate_control": {"mode": "quality", "value": 2},
            },
            "mp3",
            22050,
            1,
        ),
        ({"codec": "opus", "channels": 1}, "opus", 48000, 1),
        (
            {
                "codec": "flac",
                "sample_rate": "source",
                "channels": "source",
                "compression_level": 8,
            },
            "flac",
            22050,
            1,
        ),
        (
            {"codec": "pcm", "sample_format": "s24", "sample_rate": 44100, "channels": 2},
            "pcm_s24le",
            44100,
            2,
        ),
        (
            {"codec": "pcm", "sample_format": "f32", "sample_rate": 32000, "channels": 1},
            "pcm_f32le",
            32000,
            1,
        ),
        (
            {
                "codec": "aac",
                "rate_control": {"mode": "quality", "value": 2},
                "range": {"start_ms": 1000, "end_ms": 2000},
            },
            "aac",
            22050,
            1,
        ),
    ],
)
async def test_audio_encoding_parameters(engine, params, codec, rate, channels):
    result, directory = await run(engine, "222.mp3", params, "audio")
    audio = stream(result, "audio")
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == (codec, rate, channels)
    assert (directory / result.files[0]).stat().st_size > 0
    assert result.media["effective_params"]["resolved"]["sample_rate"] == rate
    if "range" in params:
        assert 950 <= result.media["duration_ms"] <= 1050


async def test_extract_audio_copy_and_encode(engine):
    copied, directory = await run(
        engine, "ffmpeg_h264_640x360.mkv", {"mode": "copy", "container": "m4a"}, "extract"
    )
    encoded, _ = await run(
        engine,
        "ffmpeg_h264_640x360.mkv",
        {"codec": "pcm", "sample_rate": 16000, "channels": 1},
        "extract",
    )
    assert stream(copied, "audio")["codec"] == "aac"
    assert stream(copied, "audio")["sample_rate"] == 44100
    assert stream(encoded, "audio")["codec"] == "pcm_s16le"
    assert stream(encoded, "audio")["sample_rate"] == 16000
    packets = []
    for path in (FIXTURES / "ffmpeg_h264_640x360.mkv", directory / copied.files[0]):
        fd = safe_open(path.parent, path.name, engine.settings.max_input_bytes)
        try:
            result = await engine.executor.run(
                [
                    engine.settings.ffprobe_path,
                    "-v",
                    "error",
                    *input_args(fd),
                    "-select_streams",
                    "a:0",
                    "-show_packets",
                    "-show_data_hash",
                    "sha256",
                    "-show_entries",
                    "packet=data_hash",
                    "-of",
                    "json",
                ],
                pass_fds=(fd,),
                deadline=time.time() + 30,
                cancel=asyncio.Event(),
            )
        finally:
            os.close(fd)
        assert result.returncode == 0
        packets.append([packet["data_hash"] for packet in json.loads(result.stdout)["packets"]])
    assert packets[0] and packets[0] == packets[1]


@pytest.mark.parametrize(
    "params,operation,error",
    [
        ({"mode": "copy", "container": "mp3"}, "extract", "UNSUPPORTED_MEDIA"),
        ({"audio_stream": 2}, "extract", "NO_AUDIO_TRACK"),
        ({"resize": {"width": 1920, "height": 1080, "fit": "cover"}}, "video", "UNSUPPORTED_MEDIA"),
        ({"video_stream": 1}, "video", "UNSUPPORTED_MEDIA"),
        ({"range": {"start_ms": 10000}}, "video", "TIMESTAMP_OUT_OF_RANGE"),
    ],
)
async def test_source_dependent_rejections(engine, params, operation, error):
    with pytest.raises(ServiceError, match=error):
        await run(engine, "ffmpeg_h264_640x360.mkv", params, operation)


async def test_opus_source_rate_rejected_instead_of_resampled(engine):
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run(engine, "222.mp3", {"codec": "opus", "sample_rate": "source"}, "audio")


async def test_mp3_source_rate_rejects_incompatible_bitrate(engine):
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run(
            engine,
            "222.mp3",
            {
                "codec": "mp3",
                "sample_rate": "source",
                "rate_control": {"mode": "bitrate", "bitrate_bps": 192000},
            },
            "audio",
        )


async def test_mp3_source_rate_resolves_omitted_bitrate_after_probe(engine):
    product, _ = await run(
        engine,
        "222.mp3",
        {
            "codec": "mp3",
            "sample_rate": "source",
            "channels": "source",
        },
        "audio",
    )
    audio = stream(product, "audio")
    assert (audio["sample_rate"], audio["channels"], audio["bitrate_bps"]) == (22050, 1, 96000)
    assert product.media["effective_params"]["rate_control"] == {"mode": "auto"}
    assert product.media["effective_params"]["resolved"]["rate_control"] == {
        "mode": "bitrate",
        "bitrate_bps": 96000,
    }


async def test_aac_source_properties_resolve_omitted_bitrate_after_probe(engine):
    source, directory = await run(
        engine,
        "222.mp3",
        {
            "codec": "pcm",
            "sample_rate": 8000,
            "channels": 1,
            "range": {"end_ms": 300},
        },
        "audio",
    )
    product, _ = await run(
        engine,
        directory / source.files[0],
        {
            "codec": "aac",
            "sample_rate": "source",
            "channels": "source",
        },
        "audio",
    )
    audio = stream(product, "audio")
    assert (audio["sample_rate"], audio["channels"]) == (8000, 1)
    assert product.media["effective_params"]["rate_control"] == {"mode": "auto"}
    assert product.media["effective_params"]["resolved"]["rate_control"] == {
        "mode": "bitrate",
        "bitrate_bps": 48000,
    }
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run(
            engine,
            directory / source.files[0],
            {
                "codec": "aac",
                "sample_rate": "source",
                "channels": "source",
                "rate_control": {"mode": "bitrate", "bitrate_bps": 128000},
            },
            "audio",
        )


@pytest.mark.parametrize("sample_rate,bitrate", [(8000, 64000), (16000, 128000), (48000, 192000)])
async def test_mp3_bitrate_is_preserved(engine, sample_rate, bitrate):
    product, _ = await run(
        engine,
        "222.mp3",
        {
            "codec": "mp3",
            "sample_rate": sample_rate,
            "rate_control": {"mode": "bitrate", "bitrate_bps": bitrate},
        },
        "audio",
    )
    assert stream(product, "audio")["bitrate_bps"] == bitrate
    assert product.media["effective_params"]["resolved"]["bitrate_bps"] == bitrate


@pytest.mark.parametrize(
    "codec,layout,channels",
    [
        ("opus", "3.0", 3),
        ("opus", "5.1", 6),
        ("opus", "7.1", 8),
        ("aac", "2.1", 3),
        ("pcm", "4.0", 4),
    ],
)
async def test_audio_channel_layout_is_encoded_and_verified(engine, codec, layout, channels):
    product, _ = await run(
        engine,
        "222.mp3",
        {
            "codec": codec,
            "channel_layout": layout,
            "range": {"end_ms": 300},
        },
        "audio",
    )
    audio = stream(product, "audio")
    assert audio["channel_layout"] == layout
    assert audio["channels"] == channels
    assert product.media["effective_params"]["resolved"]["channel_layout"] == layout


async def test_opus_unsupported_source_channels_rejected(engine):
    source, directory = await run(
        engine,
        "222.mp3",
        {
            "codec": "pcm",
            "channels": 4,
            "range": {"end_ms": 300},
        },
        "audio",
    )
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run(
            engine, directory / source.files[0], {"codec": "opus", "channels": "source"}, "audio"
        )


@pytest.mark.parametrize("codec,rate,bitrate", [("mp3", 22050, 160000), ("opus", 48000, 96000)])
async def test_auto_channels_fall_back_to_stereo_when_codec_requires(engine, codec, rate, bitrate):
    source, directory = await run(
        engine,
        "222.mp3",
        {
            "codec": "pcm",
            "channels": 4,
            "range": {"end_ms": 300},
        },
        "audio",
    )
    product, _ = await run(engine, directory / source.files[0], {"codec": codec}, "audio")
    audio = stream(product, "audio")
    assert (audio["sample_rate"], audio["channels"]) == (rate, 2)
    assert product.media["effective_params"]["resolved"]["rate_control"] == {
        "mode": "bitrate",
        "bitrate_bps": bitrate,
    }


async def test_auto_sample_rate_falls_back_to_supported_rate(engine):
    source, directory = await run(
        engine,
        "222.mp3",
        {
            "codec": "pcm",
            "sample_rate": 12345,
            "range": {"end_ms": 300},
        },
        "audio",
    )
    product, _ = await run(engine, directory / source.files[0], {"codec": "aac"}, "audio")
    audio = stream(product, "audio")
    assert (audio["sample_rate"], audio["channels"]) == (48000, 1)
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run(
            engine,
            directory / source.files[0],
            {
                "codec": "aac",
                "sample_rate": "source",
            },
            "audio",
        )


async def test_video_output_pixel_limit_checked_before_encoding(engine):
    engine.settings.max_pixels = 640 * 360
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await run(
            engine,
            "ffmpeg_h264_640x360.mkv",
            {"resize": {"width": 1280, "height": 720, "allow_upscale": True}},
        )
