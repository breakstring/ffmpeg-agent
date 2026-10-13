import asyncio
import hashlib
import json
import os
import re
import sys
import time
from bisect import bisect_left
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from PIL import __version__ as pillow_version
from PIL import features

from media_service.config import Settings
from media_service.errors import ServiceError
from media_service.executor import Executor
from media_service.files import file_job, fsync_directory, safe_open
from media_service.image_worker import MODES, SAVE_OPTIONS
from media_service.models import OPERATIONS, capabilities
from media_service.products import Product
from media_service.state import encode

FORMATS = "mov,matroska,mp3,wav,flac,ogg,aac,jpeg_pipe,png_pipe,webp_pipe"
_MODULE_ROOT = Path(__file__).resolve().parent.parent
VIDEO_FILTER = "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=30"
PRESETS = {
    "web_720p_v1": {
        "container": "mp4",
        "video_codec": "libx264",
        "pixel_format": "yuv420p",
        "filter": VIDEO_FILTER,
        "fps": "30/1",
        "crf": 23,
        "speed": "medium",
        "faststart": True,
        "audio": {"codec": "aac", "bitrate": "128k", "sample_rate": 48000, "channels": 2},
    },
    "aac_stereo_128k_v1": {
        "container": "m4a",
        "codec": "aac",
        "bitrate": "128k",
        "sample_rate": 48000,
        "channels": 2,
    },
    "wav_mono_16k_v1": {
        "container": "wav",
        "codec": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
    },
}
PIXEL_FORMATS = {
    "yuv420p",
    "yuv422p",
    "yuv444p",
    "yuvj420p",
    "yuvj422p",
    "yuvj444p",
    "yuv440p",
    "nv12",
    "nv21",
    "rgb24",
    "bgr24",
    "rgba",
    "bgra",
    "gray",
}
MEDIA_RULES = {
    "frame_time_origin": "first_decodable_pts",
    "frame_selection": "first_greater_or_equal",
    "response_rounding": "half_up_milliseconds",
    "timeline": "strictly_increasing_pts",
    "video_pixel_formats": sorted(PIXEL_FORMATS),
    "sar": "square_only",
    "rotation": "multiples_of_90",
    "unsupported_video_transfer": ["smpte2084", "arib-std-b67"],
    "unsupported_video_primaries": ["bt2020"],
    "unsupported_video_matrix": ["bt2020nc", "bt2020c"],
    "unspecified_sar": "assume_square",
    "image_modes": sorted(MODES),
    "image_icc": "valid_rgb_to_srgb",
    "image_output": SAVE_OPTIONS,
    "image_resampling": "lanczos",
    "image_crop": "center",
    "image_size_rounding": "nearest_even",
    "image_metadata": "remove",
    "image_pixel_limit": "source_and_actual_output",
}


def input_args(fd: int) -> list[str]:
    return ["-protocol_whitelist", "fd", "-format_whitelist", FORMATS, "-fd", str(fd), "-i", "fd:"]


def milliseconds(value) -> int | None:
    try:
        result = Decimal(str(value)) * 1000
        return (
            int(result.quantize(Decimal(1), rounding=ROUND_HALF_UP)) if result.is_finite() else None
        )
    except ValueError, TypeError, InvalidOperation:
        return None


def fraction_milliseconds(value: Fraction) -> int:
    return (2 * value.numerator * 1000 + value.denominator) // (2 * value.denominator)


@dataclass(frozen=True)
class FrameTimeline:
    time_base: Fraction
    pts: tuple[int, ...]
    times: tuple[Fraction, ...]

    def first_at(self, milliseconds: int) -> int | None:
        index = bisect_left(self.times, Fraction(milliseconds, 1000))
        return index if index < len(self.times) else None


def parse_timeline(raw: dict) -> FrameTimeline:
    try:
        time_base = Fraction(raw["streams"][0]["time_base"])
        pts = tuple(frame["pts"] for frame in raw["frames"])
        if time_base <= 0 or not pts or any(type(value) is not int for value in pts):
            raise ValueError
        if any(left >= right for left, right in zip(pts, pts[1:], strict=False)):
            raise ValueError
        if any(
            frame.get("best_effort_timestamp", frame["pts"]) != frame["pts"]
            for frame in raw["frames"]
        ):
            raise ValueError
        return FrameTimeline(time_base, pts, tuple((value - pts[0]) * time_base for value in pts))
    except KeyError, IndexError, ValueError, TypeError, ZeroDivisionError:
        raise ServiceError("INVALID_TIMELINE") from None


def _table_names(value: bytes) -> set[str]:
    result = set()
    for line in value.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[DE]+", parts[0]):
            result.update(parts[1].split(","))
    return result


def _integer(value) -> int | None:
    try:
        return int(value)
    except ValueError, TypeError:
        return None


class MediaEngine:
    def __init__(self, settings: Settings, executor: Executor):
        self.settings = settings
        self.executor = executor

    async def inspect(self) -> dict:
        if not pillow_version.startswith("12."):
            raise ServiceError("ENGINE_INCOMPATIBLE")
        versions = []
        for command in (self.settings.ffmpeg_path, self.settings.ffprobe_path):
            result = await self.executor.run(
                [command, "-version"], deadline=time.time() + 20, cancel=asyncio.Event()
            )
            match = re.search(rb"version ([^ ]+)", result.stdout)
            if result.returncode or match is None:
                raise ServiceError("ENGINE_INCOMPATIBLE")
            version = match.group(1).decode()
            if not version.startswith(self.settings.engine_series + "."):
                raise ServiceError("ENGINE_INCOMPATIBLE")
            versions.append(version)
        if versions[0] != versions[1]:
            raise ServiceError("ENGINE_INCOMPATIBLE")
        for flag, required in [
            ("-encoders", {"libx264", "aac", "pcm_s16le", "mjpeg", "png"}),
            (
                "-filters",
                {"scale", "setsar", "fps", "setpts", "settb", "select", "showinfo", "transpose"},
            ),
        ]:
            result = await self.executor.run(
                [self.settings.ffmpeg_path, "-hide_banner", flag],
                deadline=time.time() + 20,
                cancel=asyncio.Event(),
            )
            text = result.stdout.decode(errors="replace")
            if result.returncode or any(not re.search(rf"\b{name}\b", text) for name in required):
                raise ServiceError("ENGINE_INCOMPATIBLE")
        for flag, required in [
            ("-demuxers", set(FORMATS.split(","))),
            ("-muxers", {"mp4", "ipod", "wav", "image2"}),
        ]:
            result = await self.executor.run(
                [self.settings.ffmpeg_path, "-hide_banner", flag],
                deadline=time.time() + 20,
                cancel=asyncio.Event(),
            )
            if result.returncode or not required.issubset(_table_names(result.stdout)):
                raise ServiceError("ENGINE_INCOMPATIBLE")
        result = await self.executor.run(
            [self.settings.ffmpeg_path, "-hide_banner", "-protocols"],
            deadline=time.time() + 20,
            cancel=asyncio.Event(),
        )
        if (
            result.returncode
            or b"  fd\n" not in result.stdout
            or not all(features.check(x) for x in ("jpg", "webp", "littlecms2"))
        ):
            raise ServiceError("ENGINE_INCOMPATIBLE")
        frozen = {"operations": capabilities(), "presets": PRESETS, "media_rules": MEDIA_RULES}
        return {
            "ffmpeg": versions[0],
            "ffprobe": versions[1],
            "pillow": pillow_version,
            "image_version": self.settings.image_version,
            "contract_sha256": hashlib.sha256(encode(frozen).encode()).hexdigest(),
            "presets_sha256": hashlib.sha256(encode(PRESETS).encode()).hexdigest(),
        }

    async def probe(self, fd: int, deadline: float, cancel: asyncio.Event) -> dict:
        os.lseek(fd, 0, os.SEEK_SET)
        result = await self.executor.run(
            [
                self.settings.ffprobe_path,
                "-v",
                "error",
                "-threads",
                str(self.settings.ffmpeg_threads),
                *input_args(fd),
                "-show_format",
                "-show_streams",
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
        streams = raw.get("streams", [])
        if not streams or len(streams) > 32:
            raise ServiceError("INVALID_MEDIA")
        duration = milliseconds(raw.get("format", {}).get("duration"))
        if duration is not None and duration > self.settings.max_duration_seconds * 1000:
            raise ServiceError("INPUT_TOO_LARGE")
        normalized = []
        for stream in streams:
            width, height = stream.get("width"), stream.get("height")
            if width and height and width * height > self.settings.max_pixels:
                raise ServiceError("INPUT_TOO_LARGE")
            rotation = next(
                (
                    side["rotation"]
                    for side in stream.get("side_data_list", [])
                    if "rotation" in side
                ),
                0,
            )
            normalized.append(
                {
                    "index": stream.get("index"),
                    "type": stream.get("codec_type"),
                    "codec": stream.get("codec_name"),
                    "width": width,
                    "height": height,
                    "pixel_format": stream.get("pix_fmt"),
                    "sample_rate": _integer(stream.get("sample_rate")),
                    "channels": stream.get("channels"),
                    "frame_rate": stream.get("avg_frame_rate"),
                    "duration_ms": milliseconds(stream.get("duration")),
                    "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
                    "rotation_degrees": rotation,
                    "color_transfer": stream.get("color_transfer"),
                    "color_primaries": stream.get("color_primaries"),
                    "color_space": stream.get("color_space"),
                    "bits_per_raw_sample": _integer(stream.get("bits_per_raw_sample")),
                    "attached_pic": bool(stream.get("disposition", {}).get("attached_pic")),
                }
            )
        fmt = raw.get("format", {}).get("format_name")
        media_type = (
            "image"
            if fmt in {"jpeg_pipe", "png_pipe", "webp_pipe"}
            else (
                "video"
                if any(
                    s.get("codec_type") == "video"
                    and not s.get("disposition", {}).get("attached_pic")
                    for s in streams
                )
                else "audio"
            )
        )
        return {
            "type": media_type,
            "format": fmt,
            "duration_ms": duration,
            "size_bytes": os.fstat(fd).st_size,
            "streams": normalized,
        }

    def _video(self, report: dict) -> dict:
        stream = next(
            (s for s in report["streams"] if s["type"] == "video" and not s["attached_pic"]), None
        )
        if report["type"] != "video" or stream is None:
            raise ServiceError("UNSUPPORTED_MEDIA")
        if (
            stream["pixel_format"] not in PIXEL_FORMATS
            or (stream["bits_per_raw_sample"] or 8) > 8
            or stream["color_transfer"] in {"smpte2084", "arib-std-b67"}
            or stream["color_primaries"] == "bt2020"
            or stream["color_space"] in {"bt2020nc", "bt2020c"}
            or stream["sample_aspect_ratio"] not in {None, "N/A", "0:1", "1:1"}
            or stream["rotation_degrees"] % 90 != 0
            or (stream["width"] or 0) < 2
            or (stream["height"] or 0) < 2
        ):
            raise ServiceError("UNSUPPORTED_MEDIA")
        return stream

    async def _output_probe(
        self, output_dir: Path, name: str, deadline: float, cancel: asyncio.Event
    ) -> dict:
        descriptor = safe_open(output_dir, name, self.settings.max_output_bytes)
        try:
            return await self.probe(descriptor, deadline, cancel)
        finally:
            os.close(descriptor)

    async def _ffmpeg(
        self,
        fd: int,
        options: list[str],
        output_dir: Path,
        deadline: float,
        cancel: asyncio.Event,
        *,
        info: bool = False,
    ) -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        result = await self.executor.run(
            [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-n",
                "-v",
                "info" if info else "error",
                "-xerror",
                "-threads",
                str(self.settings.ffmpeg_threads),
                *input_args(fd),
                "-threads",
                str(self.settings.ffmpeg_threads),
                "-filter_threads",
                str(self.settings.ffmpeg_filter_threads),
                *options,
            ],
            pass_fds=(fd,),
            deadline=deadline,
            cancel=cancel,
            monitor_dir=output_dir,
        )
        if result.returncode:
            raise ServiceError("INVALID_MEDIA")
        return result.stderr

    async def _transcode_video(
        self, report: dict, fd: int, output_dir: Path, deadline: float, cancel: asyncio.Event
    ) -> Product:
        video = self._video(report)
        name = "video.mp4"
        maps = ["-map", f"0:{video['index']}"]
        audio = next((stream for stream in report["streams"] if stream["type"] == "audio"), None)
        if audio is not None:
            maps += ["-map", f"0:{audio['index']}"]
        await self._ffmpeg(
            fd,
            [
                *maps,
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-vf",
                VIDEO_FILTER,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "23",
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "cfr",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                str(output_dir / name),
            ],
            output_dir,
            deadline,
            cancel,
        )
        output = await self._output_probe(output_dir, name, deadline, cancel)
        stream = self._video(output)
        if (
            stream["codec"] != "h264"
            or stream["pixel_format"] != "yuv420p"
            or stream["frame_rate"] != "30/1"
            or stream["width"] > 1280
            or stream["height"] > 720
            or stream["width"] % 2
            or stream["height"] % 2
        ):
            raise ServiceError("INVALID_MEDIA")
        if audio is not None:
            encoded = next((s for s in output["streams"] if s["type"] == "audio"), {})
            if (encoded.get("codec"), encoded.get("sample_rate"), encoded.get("channels")) != (
                "aac",
                48000,
                2,
            ):
                raise ServiceError("INVALID_MEDIA")
        return Product(
            "file", output | {"content_type": "video/mp4", "preset": "web_720p_v1"}, [name]
        )

    async def _transcode_audio(
        self,
        report: dict,
        params: dict,
        fd: int,
        output_dir: Path,
        deadline: float,
        cancel: asyncio.Event,
    ) -> Product:
        streams = [s for s in report["streams"] if s["type"] == "audio"]
        if params["audio_stream"] >= len(streams):
            raise ServiceError("NO_AUDIO_TRACK")
        preset = PRESETS[params["preset"]]
        name = "audio." + preset["container"]
        options = [
            "-map",
            f"0:{streams[params['audio_stream']]['index']}",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-vn",
            "-c:a",
            preset["codec"],
            "-ar",
            str(preset["sample_rate"]),
            "-ac",
            str(preset["channels"]),
        ]
        if "bitrate" in preset:
            options += ["-b:a", preset["bitrate"], "-movflags", "+faststart"]
        await self._ffmpeg(fd, [*options, str(output_dir / name)], output_dir, deadline, cancel)
        output = await self._output_probe(output_dir, name, deadline, cancel)
        stream = next((s for s in output["streams"] if s["type"] == "audio"), {})
        if (stream.get("codec"), stream.get("sample_rate"), stream.get("channels")) != (
            preset["codec"],
            preset["sample_rate"],
            preset["channels"],
        ):
            raise ServiceError("INVALID_MEDIA")
        return Product(
            "file",
            output
            | {
                "content_type": "audio/mp4" if preset["container"] == "m4a" else "audio/wav",
                "preset": params["preset"],
            },
            [name],
        )

    async def _timeline(
        self, fd: int, index: int, deadline: float, cancel: asyncio.Event
    ) -> FrameTimeline:
        os.lseek(fd, 0, os.SEEK_SET)
        result = await self.executor.run(
            [
                self.settings.ffprobe_path,
                "-v",
                "error",
                "-threads",
                str(self.settings.ffmpeg_threads),
                *input_args(fd),
                "-select_streams",
                str(index),
                "-show_frames",
                "-show_entries",
                "frame=pts,best_effort_timestamp:stream=time_base",
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
            timeline = parse_timeline(json.loads(result.stdout))
        except ValueError:
            raise ServiceError("INVALID_TIMELINE") from None
        if timeline.times[-1] > self.settings.max_duration_seconds:
            raise ServiceError("INPUT_TOO_LARGE")
        return timeline

    async def _frames(
        self,
        report: dict,
        params: dict,
        fd: int,
        output_dir: Path,
        deadline: float,
        cancel: asyncio.Event,
    ) -> Product:
        video = self._video(report)
        timeline = await self._timeline(fd, video["index"], deadline, cancel)
        items, files = [], []
        for index, requested in enumerate(params["timestamps_ms"]):
            item = {
                "index": index,
                "requested_time_ms": requested,
                "actual_time_ms": None,
                "member_key": None,
                "error": None,
            }
            selected = timeline.first_at(requested)
            if selected is None:
                item["error"] = ServiceError("TIMESTAMP_OUT_OF_RANGE").public()
                items.append(item)
                continue
            name = f"frame_{index:03d}." + ("jpg" if params["format"] == "jpeg" else "png")
            ticks = timeline.pts[selected] - timeline.pts[0]
            try:
                log = await self._ffmpeg(
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
                        "mjpeg" if params["format"] == "jpeg" else "png",
                        "-q:v",
                        "2",
                        str(output_dir / name),
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
                if actual != timeline.times[selected] or actual < Fraction(requested, 1000):
                    raise ServiceError("INVALID_TIMELINE")
                await self._output_probe(output_dir, name, deadline, cancel)
                files.append(name)
                item.update({"member_key": name, "actual_time_ms": fraction_milliseconds(actual)})
            except ServiceError as error:
                if error.code not in {"INVALID_MEDIA", "INVALID_TIMELINE"}:
                    raise
                item["error"] = error.public()
                await file_job(lambda event, name=name: (output_dir / name).unlink(missing_ok=True))
            items.append(item)
        if not files:
            raise ServiceError("TIMESTAMP_OUT_OF_RANGE", details={"items": items})
        return Product(
            "collection",
            {
                "type": "image",
                "format": params["format"],
                "content_type": "image/jpeg" if params["format"] == "jpeg" else "image/png",
                "time_origin": "first_decodable_pts",
            },
            files,
            items,
            "succeeded" if len(files) == len(items) else "partial",
        )

    async def _resize_image(
        self, params: dict, fd: int, output_dir: Path, deadline: float, cancel: asyncio.Event
    ) -> Product:
        name = "image." + ("jpg" if params["format"] == "jpeg" else params["format"])
        os.lseek(fd, 0, os.SEEK_SET)
        result = await self.executor.run(
            [
                sys.executable,
                "-m",
                "media_service.image_worker",
                str(fd),
                str(output_dir / name),
                encode(params),
                str(self.settings.max_pixels),
            ],
            pass_fds=(fd,),
            deadline=deadline,
            cancel=cancel,
            monitor_dir=output_dir,
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
        return Product("file", report["media"], [name])

    async def run(
        self,
        operation: str,
        params: dict,
        fd: int,
        output_dir: Path,
        deadline: float,
        cancel: asyncio.Event,
    ) -> Product:
        if operation not in OPERATIONS:
            raise ServiceError("UNSUPPORTED_OPERATION")
        params = OPERATIONS[operation][2].model_validate(params).model_dump()
        if operation == "image.resize.v1":
            return await self._resize_image(params, fd, output_dir, deadline, cancel)
        report = await self.probe(fd, deadline, cancel)
        if operation == "video.transcode.v1":
            return await self._transcode_video(report, fd, output_dir, deadline, cancel)
        if operation == "video.extract_frames.v1":
            return await self._frames(report, params, fd, output_dir, deadline, cancel)
        if operation in {"audio.transcode.v1", "video.extract_audio.v1"}:
            if operation == "video.extract_audio.v1":
                self._video(report)
            elif report["type"] != "audio":
                raise ServiceError("UNSUPPORTED_MEDIA")
            return await self._transcode_audio(report, params, fd, output_dir, deadline, cancel)

        def write(event):
            path = output_dir / "report.json"
            with path.open("xb") as handle:
                handle.write(encode(report).encode())
                handle.flush()
                os.fsync(handle.fileno())
            fsync_directory(output_dir, event)

        await file_job(write)
        return Product(
            "file", {"type": "json", "content_type": "application/json"}, ["report.json"]
        )
