import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image, JpegImagePlugin

from media_service import visual
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import safe_open
from media_service.media import MediaEngine

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.fixture
def engine(settings):
    settings.work_dir.mkdir(parents=True)
    return MediaEngine(settings, Executor(settings))


@pytest.fixture(scope="module")
def visual_sources(tmp_path_factory):
    directory = tmp_path_factory.mktemp("visual")
    image = Image.new("RGB", (60, 30), "red")
    image.paste("blue", (30, 0, 60, 30))
    image.save(directory / "colors.png")
    Image.new("RGBA", (20, 10), (255, 0, 0, 128)).save(directory / "alpha.png")
    Image.new("I;16", (10, 10)).save(directory / "16bit.png")
    metadata = directory / "chapters.txt"
    metadata.write_text(
        ";FFMETADATA1\ntitle=private-input-path\ncomment=secret-source-token\n"
        "creation_time=2024-02-03T04:05:06Z\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=600\ntitle=secret-chapter-path\n"
    )
    for name, offset in [("known.mp4", "0"), ("nonzero.mp4", "2")]:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-n",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=64x48:rate=5:duration=0.8",
                "-i",
                str(metadata),
                "-map",
                "0:v",
                "-map_metadata",
                "1",
                "-map_chapters",
                "1",
                "-metadata:s:v:0",
                "language=eng",
                "-c:v",
                "libx264",
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-output_ts_offset",
                offset,
                str(directory / name),
            ],
            check=True,
            capture_output=True,
        )
    return directory


async def run_media(engine, path, operation, params):
    directory = engine.settings.work_dir / str(time.monotonic_ns())
    directory.mkdir()
    fd = safe_open(path.parent, path.name, engine.settings.max_input_bytes)
    try:
        product = await engine.run(
            operation, params, fd, directory, time.time() + 120, asyncio.Event()
        )
        assert os.fstat(fd).st_size == path.stat().st_size
        return product, directory
    finally:
        os.close(fd)


async def test_image_encoding_without_resize_and_jpeg_controls(engine, visual_sources):
    product, directory = await run_media(
        engine,
        visual_sources / "colors.png",
        "image.resize.v1",
        {"quality": 72, "jpeg_subsampling": 0, "progressive": True},
    )
    assert (product.media["width"], product.media["height"]) == (60, 30)
    assert product.media["effective_params"]["quality"] == 72
    assert product.media["effective_params"]["resolved"] == {
        "width": 60,
        "height": 30,
        "format": "jpeg",
        "color_space": "srgb",
        "quality": 72,
        "jpeg_subsampling": 0,
        "progressive": True,
    }
    with Image.open(directory / product.files[0]) as image:
        assert image.size == (60, 30)
        assert image.info["progressive"] == 1
        assert JpegImagePlugin.get_sampling(image) == 0
        assert not image.getexif() and "icc_profile" not in image.info


@pytest.mark.parametrize(
    "params,size",
    [
        ({"width": 18}, (18, 9)),
        ({"height": 18}, (36, 18)),
        ({"width": 120}, (60, 30)),
        ({"height": 60, "allow_upscale": True, "resampling": "nearest"}, (120, 60)),
    ],
)
async def test_image_single_dimension_and_upscale(engine, visual_sources, params, size):
    product, directory = await run_media(
        engine, visual_sources / "colors.png", "image.resize.v1", params | {"format": "png"}
    )
    with Image.open(directory / product.files[0]) as image:
        assert image.size == size


async def test_image_pad_transparency_and_background(engine, visual_sources):
    product, directory = await run_media(
        engine,
        visual_sources / "alpha.png",
        "image.resize.v1",
        {"width": 40, "height": 40, "fit": "pad", "background": "#00ff00", "format": "png"},
    )
    with Image.open(directory / product.files[0]) as image:
        assert image.size == (40, 40)
        assert image.convert("RGB").getpixel((0, 0)) == (0, 255, 0)
        assert image.convert("RGB").getpixel((20, 20)) == (128, 127, 0)
        assert image.mode == "RGB"
    product, directory = await run_media(
        engine, visual_sources / "alpha.png", "image.resize.v1", {"format": "png"}
    )
    with Image.open(directory / product.files[0]) as image:
        assert image.mode == "RGBA" and image.getpixel((0, 0)) == (255, 0, 0, 128)


@pytest.mark.parametrize("crop_x,color", [(0.0, (255, 0, 0)), (1.0, (0, 0, 255))])
async def test_image_cover_respects_crop_position(engine, visual_sources, crop_x, color):
    product, directory = await run_media(
        engine,
        visual_sources / "colors.png",
        "image.resize.v1",
        {"width": 20, "height": 30, "fit": "cover", "crop_x": crop_x, "format": "png"},
    )
    with Image.open(directory / product.files[0]) as image:
        assert image.size == (20, 30) and image.getpixel((10, 15)) == color


async def test_image_png_compression_and_webp_lossless(engine, visual_sources):
    sizes = []
    for level in (0, 9):
        product, directory = await run_media(
            engine,
            visual_sources / "colors.png",
            "image.resize.v1",
            {"format": "png", "compression_level": level},
        )
        sizes.append((directory / product.files[0]).stat().st_size)
        with (
            Image.open(directory / product.files[0]) as output,
            Image.open(visual_sources / "colors.png") as original,
        ):
            assert output.tobytes() == original.tobytes()
    assert sizes[0] > sizes[1]
    product, directory = await run_media(
        engine,
        visual_sources / "colors.png",
        "image.resize.v1",
        {"format": "webp", "lossless": True, "quality": 100, "method": 6},
    )
    with (
        Image.open(directory / product.files[0]) as output,
        Image.open(visual_sources / "colors.png") as original,
    ):
        assert output.tobytes() == original.tobytes()


async def test_image_limits_and_unsupported_depth(engine, visual_sources):
    engine.settings.max_pixels = 2000
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await run_media(
            engine,
            visual_sources / "colors.png",
            "image.resize.v1",
            {"width": 50, "height": 50, "fit": "pad"},
        )
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run_media(engine, visual_sources / "16bit.png", "image.resize.v1", {})


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("next", [400, 0, 400, 600, None]),
        ("previous", [200, 0, 200, 600, None]),
        ("nearest", [200, 0, 200, 600, None]),
    ],
)
async def test_frames_select_exact_pts_nonzero_origin_duplicates_partial(
    engine, visual_sources, selection, expected
):
    product, directory = await run_media(
        engine,
        visual_sources / "nonzero.mp4",
        "video.extract_frames.v1",
        {
            "sampling": {"mode": "timestamps", "timestamps_ms": [300, 0, 300, 600, 601]},
            "frame_selection": selection,
            "image": {"format": "png"},
        },
    )
    assert [item["actual_time_ms"] for item in product.items] == expected
    assert [item["requested_time_ms"] for item in product.items] == [300, 0, 300, 600, 601]
    assert product.status == "partial" and len(product.files) == 4
    assert product.media["effective_params"]["frame_selection"] == selection
    assert product.media["effective_params"]["resolved"]["successful_frame_count"] == 4
    assert {path.name for path in directory.iterdir()} == set(product.files)
    assert product.items[-1]["error"]["code"] == "TIMESTAMP_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "sampling,requested,actual",
    [
        ({"mode": "interval", "interval_ms": 200}, [0, 200, 400, 600], [0, 200, 400, 600]),
        ({"mode": "count", "count": 5}, [0, 150, 300, 450, 600], [0, 200, 400, 600, 600]),
        ({"mode": "count", "count": 1, "start_ms": 201}, [201], [400]),
        (
            {"mode": "interval", "interval_ms": 300, "end_ms": 900},
            [0, 300, 600, 900],
            [0, 400, 600, None],
        ),
    ],
)
async def test_frames_sampling_and_image_parameters(
    engine, visual_sources, sampling, requested, actual
):
    product, directory = await run_media(
        engine,
        visual_sources / "known.mp4",
        "video.extract_frames.v1",
        {"sampling": sampling, "image": {"width": 32, "format": "webp", "quality": 27}},
    )
    assert [item["requested_time_ms"] for item in product.items] == requested
    assert [item["actual_time_ms"] for item in product.items] == actual
    for name in product.files:
        with Image.open(directory / name) as image:
            assert image.format == "WEBP" and image.size == (32, 24)
    assert {path.name for path in directory.iterdir()} == set(product.files)


@pytest.mark.parametrize(
    "sampling",
    [
        {"mode": "timestamps", "timestamps_ms": [0, 0, 0]},
        {"mode": "interval", "interval_ms": 1},
        {"mode": "count", "count": 3},
    ],
)
async def test_frames_deployment_limit_before_output(engine, visual_sources, sampling):
    engine.settings.max_frame_count = 2
    with pytest.raises(ServiceError, match="OUTPUT_LIMIT"):
        await run_media(
            engine, visual_sources / "known.mp4", "video.extract_frames.v1", {"sampling": sampling}
        )
    assert not list(engine.settings.work_dir.rglob("*.png"))
    assert not list(engine.settings.work_dir.rglob("*.jpg"))


async def test_frames_default_range_preserves_fractional_last_frame(engine):
    product, _ = await run_media(
        engine,
        FIXTURES / "ffmpeg_hevc_640x360.mp4",
        "video.extract_frames.v1",
        {"sampling": {"mode": "count", "count": 2}},
    )
    assert product.status == "succeeded"
    assert [item["actual_time_ms"] for item in product.items] == [0, 958]
    assert [item["requested_time_ms"] for item in product.items] == [0, 958]


async def test_frames_cancel_reaps_image_worker_closes_descriptor_and_removes_temporary(
    engine, visual_sources, monkeypatch
):
    original_run = engine.executor.run
    original_open = visual.safe_open
    original_launch = asyncio.create_subprocess_exec
    started = asyncio.Event()
    descriptors = []

    def track_open(*args):
        fd = original_open(*args)
        descriptors.append(fd)
        return fd

    async def cancel_image_worker(args, **kwargs):
        if "media_service.image_worker" not in args:
            return await original_run(args, **kwargs)
        owned = asyncio.create_task(original_run(args, **kwargs))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
        finally:
            kwargs["cancel"].set()
            await asyncio.gather(owned, return_exceptions=True)
        return owned.result()

    async def observe_start(*args, **kwargs):
        process = await original_launch(*args, **kwargs)
        if "media_service.image_worker" in args:
            started.set()
        return process

    monkeypatch.setattr(visual, "safe_open", track_open)
    monkeypatch.setattr(engine.executor, "run", cancel_image_worker)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_start)
    with pytest.raises(ServiceError, match="CANCELED"):
        await run_media(
            engine,
            visual_sources / "known.mp4",
            "video.extract_frames.v1",
            {"sampling": {"mode": "timestamps", "timestamps_ms": [0]}},
        )
    assert descriptors and not engine.executor.active_pids
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    assert not list(engine.settings.work_dir.rglob("_frame_*.png"))


async def test_probe_sections_selected_stream_and_count(engine):
    product, directory = await run_media(
        engine,
        FIXTURES / "ffmpeg_h264_640x360.mkv",
        "media.probe.v1",
        {"sections": ["streams"], "stream_indices": [0], "count_frames": True},
    )
    report = json.loads((directory / product.files[0]).read_text())
    assert set(report) == {"streams"}
    assert len(report["streams"]) == 1
    assert report["streams"][0]["index"] == 0
    assert report["streams"][0]["decoded_frame_count"] == 24
    assert product.media["effective_params"]["resolved"]["stream_indices"] == [0]
    with pytest.raises(ServiceError, match="UNSUPPORTED_MEDIA"):
        await run_media(
            engine, FIXTURES / "ffmpeg_h264_640x360.mkv", "media.probe.v1", {"stream_indices": [31]}
        )


async def test_probe_technical_tags_chapters_do_not_leak_source_fields(engine, visual_sources):
    product, directory = await run_media(
        engine,
        visual_sources / "known.mp4",
        "media.probe.v1",
        {"sections": ["tags", "chapters"], "stream_indices": [0]},
    )
    body = (directory / product.files[0]).read_text()
    report = json.loads(body)
    assert set(report) == {"tags", "chapters"}
    assert report["tags"]["format"] == {"creation_time": "2024-02-03T04:05:06Z"}
    assert report["tags"]["streams"][0]["tags"]["language"] == "eng"
    assert report["chapters"] == [{"index": 0, "start_ms": 0, "end_ms": 600, "tags": {}}]
    assert all(
        value not in body
        for value in (
            "private-input-path",
            "secret-source-token",
            "secret-chapter-path",
            str(visual_sources),
            "known.mp4",
        )
    )


async def test_probe_default_keeps_v1_report_shape(engine):
    source = FIXTURES / "ffmpeg_h264_640x360.mkv"
    old, old_directory = await run_media(engine, source, "media.probe.v1", {})
    new, new_directory = await run_media(engine, source, "media.probe.v1", {})
    assert json.loads((old_directory / old.files[0]).read_text()) == json.loads(
        (new_directory / new.files[0]).read_text()
    )
