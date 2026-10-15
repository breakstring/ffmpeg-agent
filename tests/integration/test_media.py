import asyncio
import json
import os
import runpy
import subprocess
import time
import wave
from pathlib import Path

import pytest
from PIL import Image

from media_service.config import Caller, Settings, Storage
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import file_job, safe_open
from media_service.media import PRESETS, MediaEngine

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def boundaries(tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("media-boundaries")
    prototype = runpy.run_path(str(ROOT / "scripts" / "prototype_media.py"))
    prototype["generate"](directory)
    ffmpeg = prototype["ffmpeg"]
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x48:rate=5:duration=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p10le",
            str(directory / "10bit.mp4"),
        ]
    )
    ffmpeg(
        [
            "-i",
            str(directory / "known_pts.mp4"),
            "-c:v",
            "libx264",
            "-x264-params",
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            str(directory / "hdr.mp4"),
        ]
    )
    ffmpeg(
        [
            "-display_rotation",
            "45",
            "-i",
            str(directory / "known_pts.mp4"),
            "-c",
            "copy",
            str(directory / "rotation45.mp4"),
        ]
    )
    return directory


@pytest.fixture
def engine(tmp_path: Path):
    for name in ["state", "work", "out"]:
        (tmp_path / name).mkdir()
    version = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=True
    ).stdout.split()[2]
    settings = Settings(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        storages={"out": Storage(root=tmp_path / "out", role="output", callers=["test"])},
        callers={
            "test": Caller(tokens=["test-media-credential-00000000"], default_output_storage="out")
        },
        disk_reserve_bytes=0,
        engine_series=".".join(version.split(".")[:2]),
    )
    return MediaEngine(settings, Executor(settings))


async def run_media(engine: MediaEngine, source: Path, operation: str, params: dict | None = None):
    destination = engine.settings.work_dir / str(time.monotonic_ns())
    destination.mkdir()
    fd = safe_open(source.parent, source.name, engine.settings.max_input_bytes)
    try:
        product = await engine.run(
            operation, params or {}, fd, destination, time.time() + 120, asyncio.Event()
        )
        assert os.fstat(fd).st_size == await file_job(lambda _: source.stat().st_size)
        return product, destination
    finally:
        os.close(fd)


async def test_inspect_freezes_preset_content(engine: MediaEngine, monkeypatch: pytest.MonkeyPatch):
    before = await engine.inspect()
    assert before["ffmpeg"] == before["ffprobe"] and len(before["presets_sha256"]) == 64
    altered = PRESETS["web_720p_v1"] | {"crf": 24}
    monkeypatch.setitem(PRESETS, "web_720p_v1", altered)
    after = await engine.inspect()
    assert before["presets_sha256"] != after["presets_sha256"]
    assert before["contract_sha256"] != after["contract_sha256"]


async def test_probe_keeps_normalized_report_contract(engine: MediaEngine):
    product, directory = await run_media(
        engine, FIXTURES / "Feline_Apocalypse.mp4", "media.probe.v1"
    )
    assert product.files == ["report.json"] and product.media["type"] == "json"
    report = json.loads((directory / "report.json").read_text())
    assert product.media["effective_params"]["sections"] == ["format", "streams"]
    assert report["type"] == "video" and report["duration_ms"] == 30834
    assert report["streams"][0]["width"] == 720
    assert "tags" not in report["streams"][0]


@pytest.mark.parametrize(
    "name,size",
    [
        ("Feline_Apocalypse.mp4", (720, 720)),
        ("ffmpeg_high_bitrate_1080p.mp4", (1280, 720)),
        ("ffmpeg_hevc_640x360.mp4", (640, 360)),
        ("ffmpeg_h264_640x360.mkv", (640, 360)),
    ],
)
async def test_video_preset_real_formats(engine: MediaEngine, name: str, size: tuple[int, int]):
    product, directory = await run_media(
        engine, FIXTURES / name, "video.transcode.v1", {"preset": "web_720p_v1"}
    )
    video = next(stream for stream in product.media["streams"] if stream["type"] == "video")
    audio = next(stream for stream in product.media["streams"] if stream["type"] == "audio")
    assert (video["width"], video["height"]) == size
    assert (video["codec"], video["pixel_format"], video["frame_rate"]) == (
        "h264",
        "yuv420p",
        "30/1",
    )
    assert (audio["codec"], audio["sample_rate"], audio["channels"]) == ("aac", 48000, 2)
    assert (directory / "video.mp4").stat().st_size > 0


@pytest.mark.parametrize(
    "preset,codec,rate,channels",
    [("aac_stereo_128k_v1", "aac", 48000, 2), ("wav_mono_16k_v1", "pcm_s16le", 16000, 1)],
)
async def test_audio_presets(
    engine: MediaEngine, preset: str, codec: str, rate: int, channels: int
):
    product, _ = await run_media(
        engine, FIXTURES / "222.mp3", "audio.transcode.v1", {"preset": preset}
    )
    stream = product.media["streams"][0]
    assert (stream["codec"], stream["sample_rate"], stream["channels"]) == (codec, rate, channels)


async def test_audio_track_selection_is_ordinal_and_audible(engine: MediaEngine, boundaries: Path):
    products = []
    for track in [0, 1]:
        product, directory = await run_media(
            engine,
            boundaries / "two_audio_tracks.mp4",
            "video.extract_audio.v1",
            {"preset": "wav_mono_16k_v1", "audio_stream": track},
        )
        with wave.open(str(directory / product.files[0]), "rb") as audio:
            data = audio.readframes(audio.getnframes())
        import array

        samples = array.array("h", data)
        crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:], strict=False))
        products.append(crossings)
    assert products[1] > products[0] * 1.8
    for name, track in [("known_pts.mp4", 0), ("two_audio_tracks.mp4", 2)]:
        with pytest.raises(ServiceError, match="NO_AUDIO_TRACK"):
            await run_media(
                engine, boundaries / name, "video.extract_audio.v1", {"audio_stream": track}
            )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("known_pts.mp4", [600, 0, 400, 400, None]),
        ("nonzero_start.mp4", [600, 0, 400, 400, None]),
        ("variable_pts.mp4", [600, 0, 600, 600, 800]),
    ],
)
async def test_frames_use_pts_keep_order_duplicates_and_partial(
    engine: MediaEngine, boundaries: Path, name: str, expected: list
):
    product, directory = await run_media(
        engine,
        boundaries / name,
        "video.extract_frames.v1",
        {"sampling": {"mode": "timestamps", "timestamps_ms": [600, 0, 201, 201, 800]}, "image": {"format": "png"}},
    )
    assert [item["actual_time_ms"] for item in product.items] == expected
    assert [item["index"] for item in product.items] == list(range(5))
    assert product.status == ("partial" if None in expected else "succeeded")
    assert len(product.files) == sum(value is not None for value in expected)
    for name in product.files:
        with Image.open(directory / name) as image:
            assert image.size == (64, 48)


async def test_all_frame_requests_fail_without_collection(engine: MediaEngine, boundaries: Path):
    with pytest.raises(ServiceError, match="TIMESTAMP_OUT_OF_RANGE") as caught:
        await run_media(
            engine,
            boundaries / "known_pts.mp4",
            "video.extract_frames.v1",
            {"sampling": {"mode": "timestamps", "timestamps_ms": [601, 800, 900]}},
        )
    assert caught.value.details is not None
    assert [item["index"] for item in caught.value.details["items"]] == [0, 1, 2]
    assert all(item["member_key"] is None for item in caught.value.details["items"])


async def test_rotation_and_odd_dimensions(engine: MediaEngine, boundaries: Path):
    product, directory = await run_media(
        engine, boundaries / "rotated.mp4", "video.extract_frames.v1", {"sampling": {"mode": "timestamps", "timestamps_ms": [0]}}
    )
    with Image.open(directory / product.files[0]) as image:
        assert image.size == (48, 64)
    product, _ = await run_media(engine, boundaries / "rotated.mp4", "video.transcode.v1")
    assert (product.media["streams"][0]["width"], product.media["streams"][0]["height"]) == (48, 64)
    product, _ = await run_media(engine, boundaries / "odd_dimensions.mkv", "video.transcode.v1")
    assert (product.media["streams"][0]["width"], product.media["streams"][0]["height"]) == (64, 48)


@pytest.mark.parametrize(
    "name", ["non_square_pixels.mp4", "rotation45.mp4", "10bit.mp4", "hdr.mp4"]
)
async def test_unsupported_video_boundaries(engine: MediaEngine, boundaries: Path, name: str):
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run_media(engine, boundaries / name, "video.transcode.v1")


@pytest.mark.parametrize(
    "name", ["zero.mp4", "truncated.mp4", "corrupt.mp4", "external.m3u8", "external.ffconcat"]
)
async def test_invalid_and_external_media_fail(engine: MediaEngine, boundaries: Path, name: str):
    with pytest.raises(ServiceError, match="INVALID_MEDIA"):
        await run_media(engine, boundaries / name, "media.probe.v1")


async def test_real_display_p3_image_resize_and_strip_metadata(engine: MediaEngine):
    product, directory = await run_media(
        engine,
        FIXTURES / "haojiao.jpg",
        "image.resize.v1",
        {"width": 640, "height": 640, "format": "png"},
    )
    assert (product.media["width"], product.media["height"], product.media["color_space"]) == (
        480,
        640,
        "srgb",
    )
    with Image.open(directory / product.files[0]) as image:
        assert not image.getexif() and not image.info.get("icc_profile")


async def test_image_orientation_transparency_and_fit(engine: MediaEngine, boundaries: Path):
    product, _ = await run_media(
        engine, boundaries / "orientation_6.jpg", "image.resize.v1", {"width": 40, "height": 40}
    )
    assert (product.media["width"], product.media["height"]) == (20, 40)
    product, directory = await run_media(
        engine,
        boundaries / "transparent.png",
        "image.resize.v1",
        {"width": 10, "height": 10, "background": "#00ff00"},
    )
    with Image.open(directory / product.files[0]) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))
        assert red < 3 and green > 252 and blue < 3
    product, _ = await run_media(
        engine,
        boundaries / "static.webp",
        "image.resize.v1",
        {"width": 10, "height": 10, "fit": "cover", "format": "webp"},
    )
    assert (product.media["width"], product.media["height"]) == (10, 10)
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run_media(
            engine,
            boundaries / "static.webp",
            "image.resize.v1",
            {"width": 80, "height": 80, "fit": "cover"},
        )
    product, _ = await run_media(
        engine,
        boundaries / "static.webp",
        "image.resize.v1",
        {"width": 80, "height": 80, "fit": "cover", "allow_upscale": True},
    )
    assert (product.media["width"], product.media["height"]) == (80, 80)


@pytest.mark.parametrize(
    "name", ["animated.webp", "multi_page.tiff", "cmyk.jpg", "16bit.png", "icc.jpg"]
)
async def test_unsupported_images(engine: MediaEngine, boundaries: Path, name: str):
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run_media(engine, boundaries / name, "image.resize.v1", {"width": 10, "height": 10})


async def test_image_pixel_limits(engine: MediaEngine, boundaries: Path):
    engine.settings.max_pixels = 10_000
    with pytest.raises(ServiceError, match="INPUT_TOO_LARGE"):
        await run_media(
            engine, boundaries / "pixel_limit.png", "image.resize.v1", {"width": 10, "height": 10}
        )
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await run_media(
            engine,
            boundaries / "static.webp",
            "image.resize.v1",
            {"width": 101, "height": 100, "fit": "cover", "allow_upscale": True},
        )
    product, _ = await run_media(
        engine, boundaries / "static.webp", "image.resize.v1", {"width": 101, "height": 100}
    )
    assert (product.media["width"], product.media["height"]) == (40, 20)
