import asyncio
import json
import math
import os
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

from media_service.errors import ServiceError
from media_service.files import safe_open
from media_service.parameters import (
    AUDIO_CONTAINERS,
    AUDIO_ENCODERS,
    AUDIO_RATES,
    VIDEO_AUDIO,
    VIDEO_ENCODERS,
    audio_bitrate_supported,
    mp3_bitrates,
)
from media_service.products import Product

if TYPE_CHECKING:
    from media_service.media import MediaEngine


PCM_ENCODERS = {"s16": "pcm_s16le", "s24": "pcm_s24le", "f32": "pcm_f32le"}
CONTENT_TYPES = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "mka": "audio/x-matroska",
}


def select_video(engine: MediaEngine, report: dict, ordinal: int) -> dict:
    streams = [s for s in report["streams"] if s["type"] == "video" and not s["attached_pic"]]
    if ordinal >= len(streams):
        raise ServiceError("UNSUPPORTED_MEDIA")
    return engine._video(report | {"streams": [streams[ordinal]]})


def _dimensions(video: dict, resize: dict, max_pixels: int) -> tuple[list[str], tuple[int, int]]:
    width, height = video["width"], video["height"]
    if video["rotation_degrees"] % 180:
        width, height = height, width
    target_w, target_h = resize["width"], resize["height"]
    ratios = [
        target / source for target, source in [(target_w, width), (target_h, height)] if target
    ]
    fit = resize["fit"]
    ratio = (max(ratios) if fit == "cover" else min(ratios)) if ratios else 1
    if not resize["allow_upscale"]:
        if fit == "cover" and ratio > 1:
            raise ServiceError("UNSUPPORTED_MEDIA")
        ratio = min(ratio, 1)
    round_size = math.ceil if fit == "cover" else math.floor
    scaled_w, scaled_h = (2 * round_size(size * ratio / 2) for size in [width, height])
    if not resize["allow_upscale"]:
        # 偶数目标不超过源尺寸；限制到源的偶数下界仍能 cover，且不会因向上取整放大。
        scaled_w, scaled_h = min(scaled_w, width // 2 * 2), min(scaled_h, height // 2 * 2)
    if min(scaled_w, scaled_h) < 2:
        raise ServiceError("UNSUPPORTED_MEDIA")
    output_w, output_h = (target_w, target_h) if fit in {"cover", "pad"} else (scaled_w, scaled_h)
    if max(scaled_w * scaled_h, output_w * output_h) > max_pixels:
        raise ServiceError("OUTPUT_LIMIT")
    filters = [f"scale={scaled_w}:{scaled_h}:flags=lanczos", "setsar=1"]
    if fit == "cover":
        x = 2 * math.floor((scaled_w - target_w) * resize["crop_x"] / 2)
        y = 2 * math.floor((scaled_h - target_h) * resize["crop_y"] / 2)
        filters.append(f"crop={target_w}:{target_h}:{x}:{y}")
    elif fit == "pad":
        x = 2 * math.floor((target_w - scaled_w) / 4)
        y = 2 * math.floor((target_h - scaled_h) / 4)
        filters.append(f"pad={target_w}:{target_h}:{x}:{y}:color=0x{resize['background'][1:]}")
    return filters, (output_w, output_h)


def _range_options(params: dict, report: dict) -> list[str]:
    start, end = params["start_ms"], params["end_ms"]
    duration = report.get("duration_ms")
    if duration is not None and (start >= duration or (end is not None and end > duration)):
        raise ServiceError("TIMESTAMP_OUT_OF_RANGE")
    options = ["-ss", f"{start // 1000}.{start % 1000:03d}"] if start else []
    if end is not None:
        duration_ms = end - start
        options += ["-t", f"{duration_ms // 1000}.{duration_ms % 1000:03d}"]
    return options


def _audio_stream(report: dict, ordinal: int, optional: bool = False) -> dict | None:
    streams = [s for s in report["streams"] if s["type"] == "audio"]
    if optional and not streams and ordinal == 0:
        return None
    if ordinal >= len(streams):
        raise ServiceError("NO_AUDIO_TRACK")
    return streams[ordinal]


def _codec_family(codec: str) -> str:
    return "pcm" if codec in PCM_ENCODERS.values() else codec


def _audio_options(params: dict, stream: dict, allowed: tuple[str, ...]) -> tuple[list[str], dict]:
    if params["mode"] == "copy":
        if _codec_family(stream["codec"]) not in allowed:
            raise ServiceError("UNSUPPORTED_MEDIA")
        return ["-c:a", "copy"], {
            "codec": stream["codec"],
            "sample_rate": stream["sample_rate"],
            "channels": stream["channels"],
        }
    codec = params["codec"]
    if codec not in allowed:
        raise ServiceError("UNSUPPORTED_OPERATION")
    sample_rate = (
        stream["sample_rate"]
        if params["sample_rate"] in {"auto", "source"}
        else params["sample_rate"]
    )
    channels = (
        stream["channels"] if params["channels"] in {"auto", "source"} else params["channels"]
    )
    if params["sample_rate"] == "auto" and (
        not sample_rate
        or not 1 <= sample_rate <= 384000
        or (codec in AUDIO_RATES and sample_rate not in AUDIO_RATES[codec])
    ):
        sample_rate = 48000
    if params["channels"] == "auto" and (
        not channels
        or not 1 <= channels <= 8
        or (codec == "mp3" and channels not in {1, 2})
        or (codec == "opus" and channels not in {1, 2, 3, 6, 8})
    ):
        channels = 2
    if (
        not sample_rate
        or not channels
        or not 1 <= sample_rate <= 384000
        or not 1 <= channels <= 8
        or (codec in AUDIO_RATES and sample_rate not in AUDIO_RATES[codec])
    ):
        raise ServiceError("UNSUPPORTED_MEDIA")
    if codec == "mp3" and channels not in {1, 2}:
        raise ServiceError("UNSUPPORTED_MEDIA")
    if codec == "opus" and channels not in {1, 2, 3, 6, 8}:
        raise ServiceError("UNSUPPORTED_MEDIA")
    encoder = PCM_ENCODERS[params["sample_format"]] if codec == "pcm" else AUDIO_ENCODERS[codec]
    options = ["-c:a", encoder, "-ar", str(sample_rate), "-ac", str(channels)]
    layout = params["channel_layout"]
    if codec == "opus":
        layout = layout or {1: "mono", 2: "stereo", 3: "3.0", 6: "5.1", 8: "7.1"}[channels]
        if layout not in {"mono", "stereo", "3.0", "5.1", "7.1"}:
            raise ServiceError("UNSUPPORTED_MEDIA")
    if layout is not None:
        options += ["-channel_layout", layout]
    rate = params["rate_control"]
    if rate and rate["mode"] == "auto":
        bitrate = {
            "aac": 64000 * channels,
            "mp3": 96000 * min(channels, 2),
            "opus": 96000 if channels == 2 else 64000 * channels,
        }[codec]
        if codec == "mp3":
            bitrate = max(value for value in mp3_bitrates(sample_rate) if value <= bitrate)
        elif codec == "aac":
            bitrate = min(bitrate, 6 * sample_rate * channels)
        rate = {"mode": "bitrate", "bitrate_bps": bitrate}
    if rate:
        if rate["mode"] == "bitrate" and not audio_bitrate_supported(
            codec, sample_rate, channels, rate["bitrate_bps"]
        ):
            raise ServiceError("UNSUPPORTED_MEDIA")
        options += (
            ["-b:a", str(rate["bitrate_bps"])]
            if rate["mode"] == "bitrate"
            else ["-q:a", str(rate["value"])]
        )
    if codec == "opus":
        options += ["-vbr", "on", "-mapping_family", "1" if channels > 2 else "0"]
    if codec == "flac":
        options += ["-compression_level", str(params["compression_level"]), "-sample_fmt", "s16"]
    expected = {
        "codec": encoder if codec == "pcm" else codec,
        "sample_rate": sample_rate,
        "channels": channels,
        "rate_control": rate,
    }
    if layout is not None:
        expected["channel_layout"] = layout
    return options, expected


async def _audio_output_details(
    engine: MediaEngine,
    output_dir: Path,
    name: str,
    output: dict,
    deadline: float,
    cancel: asyncio.Event,
) -> None:
    # 编码结果补充受控属性，不透传原始探测正文。
    from media_service.media import input_args

    fd = safe_open(output_dir, name, engine.settings.max_output_bytes)
    try:
        result = await engine.executor.run(
            [
                engine.settings.ffprobe_path,
                "-v",
                "error",
                *input_args(fd),
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index,channel_layout,sample_fmt,bits_per_raw_sample,bit_rate",
                "-of",
                "json",
            ],
            pass_fds=(fd,),
            deadline=deadline,
            cancel=cancel,
        )
    finally:
        os.close(fd)
    if result.returncode:
        raise ServiceError("INVALID_MEDIA")
    try:
        details = json.loads(result.stdout)["streams"]
        for value in details:
            stream = next(s for s in output["streams"] if s["index"] == value["index"])
            stream["channel_layout"] = value.get("channel_layout")
            stream["sample_format"] = value.get("sample_fmt")
            stream["bitrate_bps"] = int(value["bit_rate"]) if "bit_rate" in value else None
    except ValueError, KeyError, TypeError, StopIteration:
        raise ServiceError("INVALID_MEDIA") from None


def _verify_audio(output: dict, expected: dict | None) -> None:
    streams = [s for s in output["streams"] if s["type"] == "audio"]
    if expected is None:
        if streams:
            raise ServiceError("INVALID_MEDIA")
        return
    observed_fields = {"codec", "sample_rate", "channels", "channel_layout", "bitrate_bps"}
    if len(streams) != 1 or any(
        streams[0].get(key) != value for key, value in expected.items() if key in observed_fields
    ):
        raise ServiceError("INVALID_MEDIA")


def _video_encoding(params: dict, threads: int) -> list[str]:
    codec, speed = params["codec"], params["speed"]
    options = ["-c:v", VIDEO_ENCODERS[codec], "-pix_fmt", "yuv420p"]
    if codec in {"h264", "hevc"}:
        options += ["-preset", {"fast": "veryfast", "medium": "medium", "slow": "slow"}[speed]]
        if codec == "hevc":
            options += ["-x265-params", f"pools={threads}:frame-threads={threads}:log-level=error"]
    elif codec == "vp9":
        options += [
            "-deadline",
            "good",
            "-cpu-used",
            {"fast": "5", "medium": "3", "slow": "1"}[speed],
        ]
    else:
        options += [
            "-preset",
            {"fast": "10", "medium": "8", "slow": "6"}[speed],
            "-svtav1-params",
            f"lp={threads}",
        ]
    rate = params["rate_control"]
    if rate["mode"] == "crf":
        options += ["-crf", str(rate["value"])]
        if codec == "vp9":
            options += ["-b:v", "0"]
    else:
        options += ["-b:v", str(rate["bitrate_bps"])]
        if rate["maxrate_bps"] is not None:
            options += ["-maxrate", str(rate["maxrate_bps"])]
        if rate["buffer_size_bits"] is not None:
            options += ["-bufsize", str(rate["buffer_size_bits"])]
    return options


async def run_video(
    engine: MediaEngine,
    params: dict,
    report: dict,
    fd: int,
    output_dir: Path,
    deadline: float,
    cancel: asyncio.Event,
) -> Product:
    video = select_video(engine, report, params["video_stream"])
    filters, dimensions = _dimensions(video, params["resize"], engine.settings.max_pixels)
    if params["codec"] == "av1" and min(dimensions) < 64:
        raise ServiceError("UNSUPPORTED_MEDIA")
    fps = params["frame_rate"]
    if fps["mode"] == "constant":
        filters.append(f"fps={fps['value']}")
    elif fps["mode"] == "max":
        rate = Fraction(fps["value"])
        filters.append(
            f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{rate.denominator}/{rate.numerator}-0.000000001)'"
        )
    options = ["-map", f"0:{video['index']}", "-map_metadata", "-1", "-map_chapters", "-1"]
    audio = params["audio"]
    source_audio = (
        None
        if audio["mode"] == "drop"
        else _audio_stream(report, audio["audio_stream"], optional=True)
    )
    expected_audio = None
    if source_audio:
        audio_options, expected_audio = _audio_options(
            audio, source_audio, VIDEO_AUDIO[params["container"]]
        )
        options += ["-map", f"0:{source_audio['index']}", *audio_options]
    else:
        options += ["-an"]
    options += [
        *_range_options(params["range"], report),
        "-vf",
        ",".join(filters),
        *_video_encoding(params, engine.settings.ffmpeg_threads),
        "-fps_mode",
        "cfr" if fps["mode"] == "constant" else "passthrough",
    ]
    if fps["mode"] != "constant":
        # passthrough 本身不阻止编码器按推测帧率量化 PTS；保留输入 time base。
        options += ["-enc_time_base:v", "filter"]
    if params["container"] == "mp4":
        options += ["-movflags", "+faststart"]
    name = "video." + params["container"]
    await engine._ffmpeg(fd, [*options, str(output_dir / name)], output_dir, deadline, cancel)
    output = await engine._output_probe(output_dir, name, deadline, cancel)
    if expected_audio is not None:
        await _audio_output_details(engine, output_dir, name, output, deadline, cancel)
    encoded = engine._video(output)
    if (encoded["codec"], encoded["pixel_format"], encoded["width"], encoded["height"]) != (
        params["codec"],
        "yuv420p",
        *dimensions,
    ):
        raise ServiceError("INVALID_MEDIA")
    if fps["mode"] == "constant":
        try:
            rate_matches = Fraction(encoded["frame_rate"]) == Fraction(fps["value"])
        except TypeError, ValueError, ZeroDivisionError:
            rate_matches = False
        if not rate_matches:
            raise ServiceError("INVALID_MEDIA")
    _verify_audio(output, expected_audio)
    effective = params | {
        "resolved": {"width": dimensions[0], "height": dimensions[1], "audio": expected_audio}
    }
    return Product(
        "file",
        output
        | {
            "content_type": CONTENT_TYPES[params["container"]],
            "preset": params["preset"],
            "effective_params": effective,
        },
        [name],
    )


async def run_audio(
    engine: MediaEngine,
    params: dict,
    report: dict,
    fd: int,
    output_dir: Path,
    deadline: float,
    cancel: asyncio.Event,
    *,
    extract: bool = False,
) -> Product:
    if extract:
        engine._video(report)
    elif report["type"] != "audio":
        raise ServiceError("UNSUPPORTED_MEDIA")
    stream = _audio_stream(report, params["audio_stream"])
    assert stream is not None
    options, expected = _audio_options(params, stream, AUDIO_CONTAINERS[params["container"]])
    if (
        params["mode"] == "encode"
        and params["container"] == "mp3"
        and expected["rate_control"]["mode"] == "bitrate"
    ):
        expected["bitrate_bps"] = expected["rate_control"]["bitrate_bps"]
    options = [
        "-map",
        f"0:{stream['index']}",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vn",
        *options,
        *_range_options(params["range"], report),
    ]
    if params["container"] == "m4a":
        options += ["-movflags", "+faststart"]
    name = "audio." + params["container"]
    await engine._ffmpeg(fd, [*options, str(output_dir / name)], output_dir, deadline, cancel)
    output = await engine._output_probe(output_dir, name, deadline, cancel)
    await _audio_output_details(engine, output_dir, name, output, deadline, cancel)
    _verify_audio(output, expected)
    return Product(
        "file",
        output
        | {
            "content_type": CONTENT_TYPES[params["container"]],
            "preset": params["preset"],
            "effective_params": params | {"resolved": expected},
        },
        [name],
    )
