import array
import asyncio
import json
import math
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest
from PIL import Image

from media_service.executor import Executor
from media_service.files import safe_open
from media_service.media import MediaEngine

COLORS = ((240, 16, 16), (16, 240, 16), (16, 16, 240), (240, 240, 16))
VIDEO_SIZE = (64, 48)


def command(arguments: list[str]) -> bytes:
    return subprocess.run(arguments, check=True, capture_output=True, timeout=30).stdout


def decoded_centers(path: Path) -> list[tuple[int, ...]]:
    pixels = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
    )
    width, height = VIDEO_SIZE
    frame_bytes = width * height * 3
    assert pixels and len(pixels) % frame_bytes == 0
    center = ((height // 2) * width + width // 2) * 3
    return [
        tuple(pixels[offset + center : offset + center + 3])
        for offset in range(0, len(pixels), frame_bytes)
    ]


def assert_color(actual: tuple[int, ...], expected: tuple[int, ...]) -> None:
    assert len(actual) == len(expected) == 3
    assert all(abs(a - b) <= 5 for a, b in zip(actual, expected, strict=True))


@pytest.fixture(scope="module")
def semantic_sources(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("media-semantics")
    samples = array.array("h")
    for index in range(48000 * 2):
        frequencies = (
            (300, 500) if index < 48000 else (700, 1100) if index < 72000 else (1300, 1700)
        )
        samples.extend(
            round(12000 * math.sin(math.tau * frequency * index / 48000))
            for frequency in frequencies
        )
    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(directory / "segmented-stereo.wav"), "wb") as audio:
        audio.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(samples.tobytes())

    for index, color in enumerate(COLORS):
        Image.new("RGB", VIDEO_SIZE, color).save(directory / f"color_{index:02d}.png")
    source = directory / "four-colors.mp4"
    command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-framerate",
            "5",
            "-i",
            str(directory / "color_%02d.png"),
            "-frames:v",
            "4",
            "-c:v",
            "libx264",
            "-crf",
            "0",
            "-bf",
            "0",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )
    actual_colors = decoded_centers(source)
    assert len(actual_colors) == 4
    for actual, expected in zip(actual_colors, COLORS, strict=True):
        assert_color(actual, expected)
    timeline = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "json",
                str(source),
            ]
        )
    )
    assert [float(frame["pts_time"]) for frame in timeline["frames"]] == [0, 0.2, 0.4, 0.6]

    quadrants = Image.new("RGB", (60, 40))
    for color, box in zip(
        COLORS,
        ((0, 0, 30, 20), (30, 0, 60, 20), (0, 20, 30, 40), (30, 20, 60, 40)),
        strict=True,
    ):
        quadrants.paste(color, box)
    for orientation in (2, 3, 6, 8):
        exif = Image.Exif()
        exif[274] = orientation
        exif[270] = "generated orientation boundary"
        quadrants.save(
            directory / f"quadrants-orientation-{orientation}.jpg",
            quality=100,
            subsampling=0,
            exif=exif,
        )
    return directory


@pytest.fixture
def engine(settings):
    settings.work_dir.mkdir()
    return MediaEngine(settings, Executor(settings))


async def run_media(engine: MediaEngine, source: Path, operation: str, params: dict):
    directory = engine.settings.work_dir / str(time.monotonic_ns())
    directory.mkdir()
    fd = safe_open(source.parent, source.name, engine.settings.max_input_bytes)
    try:
        product = await engine.run(
            operation, params, fd, directory, time.time() + 60, asyncio.Event()
        )
        return product, directory
    finally:
        os.close(fd)


def tone_amplitude(samples: list[int], sample_rate: int, frequency: int) -> float:
    sine = sum(
        sample * math.sin(math.tau * frequency * index / sample_rate)
        for index, sample in enumerate(samples)
    )
    cosine = sum(
        sample * math.cos(math.tau * frequency * index / sample_rate)
        for index, sample in enumerate(samples)
    )
    return 2 * math.hypot(sine, cosine) / len(samples)


@pytest.mark.parametrize("channels", [1, 2])
async def test_audio_trim_resampling_and_channels_preserve_selected_content(
    engine, semantic_sources, channels
):
    product, directory = await run_media(
        engine,
        semantic_sources / "segmented-stereo.wav",
        "audio.transcode.v1",
        {
            "codec": "pcm",
            "sample_format": "s16",
            "sample_rate": 16000,
            "channels": channels,
            "range": {"start_ms": 1250, "end_ms": 1750},
        },
    )
    with wave.open(str(directory / product.files[0]), "rb") as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (
            16000,
            channels,
            2,
        )
        assert audio.getnframes() == 8000
        samples = array.array("h", audio.readframes(audio.getnframes()))
    if sys.byteorder != "little":
        samples.byteswap()
    for channel in range(channels):
        channel_samples = list(samples[channel::channels])
        for segment, frequencies in enumerate(((700, 1100), (1300, 1700))):
            # 裁剪后换音点应在 250ms；避开滤波边缘的整周期窗口区分时间和声道错误。
            window = channel_samples[segment * 4000 + 320 : (segment + 1) * 4000 - 320]
            amplitudes = {
                frequency: tone_amplitude(window, 16000, frequency)
                for frequency in (300, 500, 700, 1100, 1300, 1700)
            }
            assert all(
                amplitude < 80
                for frequency, amplitude in amplitudes.items()
                if frequency not in frequencies
            )
            if channels == 1:
                assert all(amplitudes[frequency] > 2000 for frequency in frequencies)
            else:
                wanted, other = frequencies[channel], frequencies[1 - channel]
                assert amplitudes[wanted] > 5000 and amplitudes[other] < 80


@pytest.mark.parametrize(
    "selection,expected_indices",
    [
        ("next", [3, 0, 2, 2, 1, 2, 3]),
        ("previous", [2, 0, 1, 1, 0, 1, 3]),
        ("nearest", [2, 0, 1, 1, 0, 2, 3]),
    ],
)
async def test_frame_selection_manifest_binds_times_to_pixels(
    engine, semantic_sources, selection, expected_indices
):
    requested = [401, 0, 201, 201, 100, 350, 600]
    product, directory = await run_media(
        engine,
        semantic_sources / "four-colors.mp4",
        "video.extract_frames.v1",
        {
            "sampling": {"mode": "timestamps", "timestamps_ms": requested},
            "frame_selection": selection,
            "image": {"format": "png"},
        },
    )
    assert product.status == "succeeded" and len(product.items) == len(requested)
    assert product.files == [item["member_key"] for item in product.items]
    for index, (item, color_index) in enumerate(zip(product.items, expected_indices, strict=True)):
        assert item["index"] == index
        assert item["requested_time_ms"] == requested[index]
        assert item["actual_time_ms"] == color_index * 200
        assert item["error"] is None
        with Image.open(directory / item["member_key"]) as image:
            assert image.size == VIDEO_SIZE
            assert_color(image.convert("RGB").getpixel((32, 24)), COLORS[color_index])
    with (
        Image.open(directory / product.items[2]["member_key"]) as first,
        Image.open(directory / product.items[3]["member_key"]) as duplicate,
    ):
        assert first.tobytes() == duplicate.tobytes()


async def test_video_trim_selects_expected_first_and_last_frames(engine, semantic_sources):
    product, directory = await run_media(
        engine,
        semantic_sources / "four-colors.mp4",
        "video.transcode.v1",
        {"range": {"start_ms": 200, "end_ms": 600}, "audio": {"mode": "drop"}},
    )
    colors = decoded_centers(directory / product.files[0])
    assert len(colors) == 2
    assert_color(colors[0], COLORS[1])
    assert_color(colors[-1], COLORS[2])


@pytest.mark.parametrize(
    "orientation,size,expected_indices",
    [
        (2, (60, 40), (1, 0, 3, 2)),
        (3, (60, 40), (3, 2, 1, 0)),
        (6, (40, 60), (2, 0, 3, 1)),
        (8, (40, 60), (1, 3, 0, 2)),
    ],
)
async def test_exif_orientation_transforms_all_quadrants_and_removes_metadata(
    engine, semantic_sources, orientation, size, expected_indices
):
    source = semantic_sources / f"quadrants-orientation-{orientation}.jpg"
    with Image.open(source) as original:
        assert original.getexif()[274] == orientation
        assert original.size == (60, 40)
    product, directory = await run_media(engine, source, "image.resize.v1", {"format": "png"})
    assert (product.media["width"], product.media["height"]) == size
    with Image.open(directory / product.files[0]) as image:
        assert image.size == size
        assert not image.getexif() and not image.info.get("icc_profile")
        width, height = size
        points = ((5, 5), (width - 6, 5), (5, height - 6), (width - 6, height - 6))
        for point, color_index in zip(points, expected_indices, strict=True):
            assert_color(image.convert("RGB").getpixel(point), COLORS[color_index])
