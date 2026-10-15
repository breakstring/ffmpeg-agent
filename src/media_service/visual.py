import asyncio
import json
import os
import re
import sys
from bisect import bisect_left, bisect_right
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from media_service.errors import ServiceError
from media_service.files import file_job, fsync_directory, safe_open
from media_service.image_worker import FORMATS
from media_service.products import Product
from media_service.state import encode

if TYPE_CHECKING:
    from media_service.media import FrameTimeline, MediaEngine

_MODULE_ROOT = Path(__file__).resolve().parent.parent


async def _resize(
    engine: MediaEngine,
    params: dict,
    fd: int,
    destination: Path,
    deadline: float,
    cancel: asyncio.Event,
) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    result = await engine.executor.run(
        [
            sys.executable,
            "-m",
            "media_service.image_worker",
            str(fd),
            str(destination),
            encode(params),
            str(engine.settings.max_pixels),
        ],
        pass_fds=(fd,),
        deadline=deadline,
        cancel=cancel,
        monitor_dir=destination.parent,
        env={
            "PYTHONPATH": str(_MODULE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
        },
    )
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise ServiceError("INVALID_MEDIA") from None
    if result.returncode:
        code = report.get("error", "INVALID_MEDIA")
        raise ServiceError(code, retryable=code == "DISK_FULL")
    if "media" not in report:
        raise ServiceError("INVALID_MEDIA")
    return report["media"]


async def run_image(
    engine: MediaEngine,
    params: dict,
    fd: int,
    output_dir: Path,
    deadline: float,
    cancel: asyncio.Event,
) -> Product:
    extension = "jpg" if params["format"] == "jpeg" else params["format"]
    name = f"image.{extension}"
    report = await _resize(engine, params, fd, output_dir / name, deadline, cancel)
    resolved = {key: report[key] for key in ("width", "height", "format", "color_space")}
    resolved.update(
        {
            key: params[key]
            for key in (
                "quality",
                "compression_level",
                "jpeg_subsampling",
                "progressive",
                "lossless",
                "method",
            )
            if params[key] is not None
        }
    )
    return Product("file", report | {"effective_params": params | {"resolved": resolved}}, [name])


def _requests(sampling: dict, last: Fraction, limit: int) -> list[Fraction]:
    if sampling["mode"] == "timestamps":
        if len(sampling["timestamps_ms"]) > limit:
            raise ServiceError("OUTPUT_LIMIT")
        return [Fraction(value, 1000) for value in sampling["timestamps_ms"]]
    start = Fraction(sampling["start_ms"], 1000)
    end = Fraction(sampling["end_ms"], 1000) if sampling["end_ms"] is not None else last
    if end < start:
        raise ServiceError("TIMESTAMP_OUT_OF_RANGE")
    if sampling["mode"] == "interval":
        interval = Fraction(sampling["interval_ms"], 1000)
        count = (end - start) // interval + 1
        if count > limit:
            raise ServiceError("OUTPUT_LIMIT")
        return [start + index * interval for index in range(count)]
    count = sampling["count"]
    if count > limit:
        raise ServiceError("OUTPUT_LIMIT")
    if count == 1:
        return [start]
    return [start + index * (end - start) / (count - 1) for index in range(count)]


def _select(timeline: FrameTimeline, requested: Fraction, selection: str) -> int | None:
    if requested < 0 or requested > timeline.times[-1]:
        return None
    after = bisect_left(timeline.times, requested)
    if selection == "next":
        return after
    before = bisect_right(timeline.times, requested) - 1
    if selection == "previous":
        return before
    return (
        before if requested - timeline.times[before] <= timeline.times[after] - requested else after
    )


async def run_frames(
    engine: MediaEngine,
    report: dict,
    params: dict,
    fd: int,
    output_dir: Path,
    deadline: float,
    cancel: asyncio.Event,
) -> Product:
    from media_service.media import fraction_milliseconds

    videos = [
        stream
        for stream in report["streams"]
        if stream["type"] == "video" and not stream["attached_pic"]
    ]
    if params["video_stream"] >= len(videos):
        raise ServiceError("UNSUPPORTED_MEDIA")
    video = engine._video(report | {"streams": [videos[params["video_stream"]]]})
    timeline = await engine._timeline(fd, video["index"], deadline, cancel)
    requests = _requests(params["sampling"], timeline.times[-1], engine.settings.max_frame_count)
    image_params = params["image"]
    format = image_params["format"]
    extension = "jpg" if format == "jpeg" else format
    items, files = [], []
    for index, requested in enumerate(requests):
        item = {
            "index": index,
            "requested_time_ms": fraction_milliseconds(requested),
            "actual_time_ms": None,
            "member_key": None,
            "error": None,
        }
        selected = _select(timeline, requested, params["frame_selection"])
        if selected is None:
            item["error"] = ServiceError("TIMESTAMP_OUT_OF_RANGE").public()
            items.append(item)
            continue
        name = f"frame_{index:03d}.{extension}"
        temporary = output_dir / f"_frame_{index:03d}.png"
        ticks = timeline.pts[selected] - timeline.pts[0]
        try:
            # PNG中间结果保持解码像素；最终图像统一走与resize相同的受限子进程。
            log = await engine._ffmpeg(
                fd,
                [
                    "-map",
                    f"0:{video['index']}",
                    "-map_metadata",
                    "-1",
                    "-vf",
                    f"settb={timeline.time_base.numerator}/{timeline.time_base.denominator},setpts=PTS-STARTPTS,select='eq(pts,{ticks})',showinfo",
                    "-frames:v",
                    "1",
                    "-fps_mode",
                    "passthrough",
                    "-c:v",
                    "png",
                    str(temporary),
                ],
                output_dir,
                deadline,
                cancel,
                info=True,
            )
            actual_pts = re.search(rb"\bn:\s*0\s+pts:\s*(-?\d+)\s+pts_time:", log)
            actual_base = re.search(rb"config in time_base:\s*(\d+/\d+)", log)
            if actual_pts is None or actual_base is None:
                raise ServiceError("INVALID_TIMELINE")
            actual = int(actual_pts.group(1)) * Fraction(actual_base.group(1).decode())
            if actual != timeline.times[selected]:
                raise ServiceError("INVALID_TIMELINE")
            descriptor = safe_open(output_dir, temporary.name, engine.settings.max_output_bytes)
            try:
                media = await _resize(
                    engine, image_params, descriptor, output_dir / name, deadline, cancel
                )
            finally:
                os.close(descriptor)
            files.append(name)
            item.update(
                {
                    "member_key": name,
                    "actual_time_ms": fraction_milliseconds(actual),
                    "media": media,
                }
            )
        except ServiceError as error:
            if error.code not in {"INVALID_MEDIA", "INVALID_TIMELINE"}:
                raise
            item["error"] = error.public()
            await file_job(lambda event, name=name: (output_dir / name).unlink(missing_ok=True))
        finally:
            await file_job(lambda event, temporary=temporary: temporary.unlink(missing_ok=True))
        items.append(item)
    if not files:
        raise ServiceError("TIMESTAMP_OUT_OF_RANGE", details={"items": items})
    return Product(
        "collection",
        {
            "type": "image",
            "format": format,
            "content_type": FORMATS[format][1],
            "time_origin": "first_decodable_pts",
            "frame_selection": params["frame_selection"],
            "effective_params": params
            | {
                "resolved": {
                    "video_stream_index": video["index"],
                    "requested_frame_count": len(items),
                    "successful_frame_count": len(files),
                }
            },
        },
        files,
        items,
        "succeeded" if len(files) == len(items) else "partial",
    )


def _tags(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        key = key.lower()
        if key == "language" and re.fullmatch(r"[a-z]{2,3}", value):
            result[key] = value
        elif key == "creation_time" and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
        ):
            try:
                result[key] = datetime.fromisoformat(value).isoformat().replace("+00:00", "Z")
            except ValueError:
                continue
    return result


async def run_probe(
    engine: MediaEngine,
    report: dict,
    params: dict,
    fd: int,
    output_dir: Path,
    deadline: float,
    cancel: asyncio.Event,
) -> Product:
    from media_service.media import input_args, milliseconds

    indices = params["stream_indices"]
    available = {stream["index"] for stream in report["streams"]}
    if indices is not None and not set(indices).issubset(available):
        raise ServiceError("UNSUPPORTED_MEDIA")
    selected = available if indices is None else set(indices)
    sections = set(params["sections"])
    raw = {}
    if params["count_frames"] or sections & {"tags", "chapters"}:
        entries = ["stream=index,nb_read_frames"]
        options = ["-show_streams"]
        if "tags" in sections:
            entries += ["stream_tags=language,creation_time", "format_tags=language,creation_time"]
            options += ["-show_format"]
        if "chapters" in sections:
            entries += ["chapter=id,start_time,end_time", "chapter_tags=language,creation_time"]
            options += ["-show_chapters"]
        if params["count_frames"]:
            options += ["-count_frames"]
        os.lseek(fd, 0, os.SEEK_SET)
        result = await engine.executor.run(
            [
                engine.settings.ffprobe_path,
                "-v",
                "error",
                "-threads",
                str(engine.settings.ffmpeg_threads),
                *input_args(fd),
                *options,
                "-show_entries",
                ":".join(entries),
                "-of",
                "json",
            ],
            pass_fds=(fd,),
            deadline=deadline,
            cancel=cancel,
        )
        if result.returncode:
            raise ServiceError("INVALID_MEDIA")
        try:
            raw = json.loads(result.stdout)
        except ValueError:
            raise ServiceError("INVALID_MEDIA") from None
    output = {}
    if "format" in sections:
        output.update({key: report[key] for key in ("type", "format", "duration_ms", "size_bytes")})
    if "streams" in sections:
        streams = [stream.copy() for stream in report["streams"] if stream["index"] in selected]
        counts = {
            stream.get("index"): stream.get("nb_read_frames") for stream in raw.get("streams", [])
        }
        if params["count_frames"]:
            for stream in streams:
                count = counts.get(stream["index"])
                stream["decoded_frame_count"] = (
                    int(count) if isinstance(count, str) and count.isdecimal() else None
                )
        output["streams"] = streams
    if "tags" in sections:
        output["tags"] = {
            "format": _tags(raw.get("format", {}).get("tags")),
            "streams": [
                {"index": stream["index"], "tags": _tags(stream.get("tags"))}
                for stream in raw.get("streams", [])
                if stream.get("index") in selected
            ],
        }
    if "chapters" in sections:
        chapters = []
        for index, chapter in enumerate(raw.get("chapters", [])):
            item = {
                "index": index,
                "start_ms": milliseconds(chapter.get("start_time")),
                "end_ms": milliseconds(chapter.get("end_time")),
            }
            if "tags" in sections:
                item["tags"] = _tags(chapter.get("tags"))
            chapters.append(item)
        output["chapters"] = chapters

    def write(event):
        with (output_dir / "report.json").open("xb") as handle:
            handle.write(encode(output).encode())
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory(output_dir, event)

    await file_job(write)
    return Product(
        "file",
        {
            "type": "json",
            "content_type": "application/json",
            "effective_params": params | {"resolved": {"stream_indices": sorted(selected)}},
        },
        ["report.json"],
    )
