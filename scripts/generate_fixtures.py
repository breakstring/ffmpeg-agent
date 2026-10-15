"""离线生成六个基础媒体素材；只允许写入全新目录，验证后再由维护者替换。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import platform
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw, ImageFont
from PIL import __version__ as pillow_version

SEED = 20261015
NAMES = (
    "video_h264_aac_square.mp4",
    "audio_mp3_mono_22050.mp3",
    "image_jpeg_portrait_odd.jpg",
    "video_h264_high_bitrate_1080p.mp4",
    "video_hevc_640x360.mp4",
    "video_h264_640x360.mkv",
)


def linear_rgb_icc() -> bytes:
    """保留 sRGB 原色/白点，将传递曲线换成线性，用于真实 ICC 转换边界测试。"""
    profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    for index in range(struct.unpack_from(">I", profile, 128)[0]):
        tag, offset, size = struct.unpack_from(">4sII", profile, 132 + 12 * index)
        if tag in (b"rTRC", b"gTRC", b"bTRC"):
            curve = struct.pack(">4s4xIH", b"curv", 1, 256)
            profile[offset : offset + size] = curve.ljust(size, b"\0")
        elif tag == b"desc":
            label = "Linear RGB".encode("utf-16-be")
            description = struct.pack(">4s4xII4sII", b"mluc", 1, 12, b"enUS", len(label), 28)
            profile[offset : offset + size] = (description + label).ljust(size, b"\0")
    struct.pack_into(">6H", profile, 24, 2026, 10, 15, 0, 0, 0)
    profile[84:100] = bytes(16)
    # 由 LittleCMS 解析完整配置，避免测试错误地退化为无效 ICC 拒绝用例。
    ImageCms.ImageCmsProfile(io.BytesIO(profile))
    return bytes(profile)


def run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed with exit code {result.returncode}")
    return result.stdout


def encode(args: list[str], destination: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-filter_complex_threads",
            "1",
            *args,
            "-map_metadata",
            "-1",
            "-threads",
            "1",
            str(destination),
        ]
    )


def write_tones(path: Path, *, sample_rate: int, duration: int, stereo: bool) -> None:
    samples = bytearray()
    for sample in range(sample_rate * duration):
        time = sample / sample_rate
        edge = min(1.0, time / 0.025, (duration - time) / 0.025)
        if stereo:
            pulse = 0.5 if time % 1 < 0.15 else 0.2
            values = (pulse * math.sin(2 * math.pi * frequency * time) for frequency in (440, 880))
        else:
            section = int(time // 2)
            frequency = (440, 880, 0, 1320)[section]
            section_edge = min(1.0, (time % 2) / 0.025, (2 - time % 2) / 0.025)
            values = (0.35 * section_edge * math.sin(2 * math.pi * frequency * time),)
        samples.extend(
            struct.pack(
                "<" + "h" * (2 if stereo else 1), *[round(32767 * edge * value) for value in values]
            )
        )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2 if stereo else 1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples)


def write_portrait(path: Path) -> None:
    width, height = 1279, 1706
    pixels = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(
                (x * 255 // (width - 1), y * 255 // (height - 1), 128)
            )
    image = Image.frombytes("RGB", (width, height), bytes(pixels))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=42)
    small_font = ImageFont.load_default(size=28)
    draw.rectangle((0, 0, width - 1, 115), fill="black")
    draw.text((35, 30), "FFMPEG-AGENT / 1279 x 1706 / RGB", fill="white", font=font)
    colors = ("red", "green", "blue", "cyan", "magenta", "yellow", "white", "black")
    for index, color in enumerate(colors):
        left = 40 + index * 150
        draw.rectangle((left, 180, left + 139, 420), fill=color)
    for index, (x, y, label) in enumerate(
        ((35, 470, "TL"), (1050, 470, "TR"), (35, 1540, "BL"), (1050, 1540, "BR"))
    ):
        draw.rectangle((x, y, x + 185, y + 95), fill="black", outline="white", width=index + 1)
        draw.text((x + 42, y + 25), label, fill="white", font=font)
    for index in range(60):
        x = 280 + index * 12
        draw.line((x, 620, x, 850), fill="black" if index % 2 else "white", width=1 + index % 4)
    for index in range(32):
        gray = index * 255 // 31
        draw.rectangle((280 + index * 24, 930, 303 + index * 24, 1120), fill=(gray,) * 3)
    draw.ellipse((395, 1190, 885, 1510), outline="white", width=5)
    draw.line((width // 2, 1170, width // 2, 1530), fill="black", width=2)
    draw.text((340, 1150), "1-4 px lines / gray ramp / crop target", fill="black", font=small_font)
    image.save(path, quality=92, subsampling=0, optimize=False)


def write_frame_labels(directory: Path) -> None:
    font = ImageFont.load_default(size=27)
    for frame in range(900):
        image = Image.new("RGB", (720, 48), "black")
        text = f"FRAME {frame:04d} / {frame // 30:02d}s + {frame % 30:02d}/30 / L440 R880"
        ImageDraw.Draw(image).text((12, 9), text, font=font, fill="white")
        image.save(directory / f"label-{frame:04d}.png")


def describe(path: Path) -> dict:
    if path.suffix == ".jpg":
        with Image.open(path) as image:
            image.load()
            return {
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "mode": image.mode,
                "icc_profile": bool(image.info.get("icc_profile")),
            }
    return json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "format=format_name,duration,bit_rate:stream=index,codec_type,codec_name,width,height,"
                "pix_fmt,r_frame_rate,avg_frame_rate,time_base,start_time,duration,nb_frames,nb_read_frames,"
                "sample_rate,channels,channel_layout,bit_rate",
                "-of",
                "json",
                str(path),
            ]
        )
    )


def validate(destination: Path, metadata: dict) -> None:
    if metadata[NAMES[2]] != {
        "format": "JPEG",
        "width": 1279,
        "height": 1706,
        "mode": "RGB",
        "icc_profile": False,
    }:
        raise ValueError("Unexpected JPEG contract")
    expected_video = {
        NAMES[0]: ("h264", 720, 720, "30/1", 900, 2),
        NAMES[3]: ("h264", 1920, 1080, "30/1", 36, 1),
        NAMES[4]: ("hevc", 640, 360, "24/1", 24, 1),
        NAMES[5]: ("h264", 640, 360, "24/1", 24, 1),
    }
    for name, (codec, width, height, rate, frames, channels) in expected_video.items():
        streams = metadata[name]["streams"]
        if [stream["codec_type"] for stream in streams] != ["video", "audio"]:
            raise ValueError(f"Unexpected stream layout: {name}")
        video, audio = streams
        actual = (
            video["codec_name"],
            video["width"],
            video["height"],
            video["r_frame_rate"],
            int(video["nb_read_frames"]),
            audio["channels"],
        )
        if actual != (codec, width, height, rate, frames, channels):
            raise ValueError(f"Unexpected media contract: {name}")
        if video["pix_fmt"] != "yuv420p":
            raise ValueError(f"Unexpected pixel format: {name}")
        if audio["codec_name"] != "aac" or audio["sample_rate"] != "44100":
            raise ValueError(f"Unexpected audio contract: {name}")
    mp3_streams = metadata[NAMES[1]]["streams"]
    if len(mp3_streams) != 1 or mp3_streams[0]["codec_type"] != "audio":
        raise ValueError("Unexpected MP3 stream layout")
    mp3 = mp3_streams[0]
    if (mp3["codec_name"], mp3["sample_rate"], mp3["channels"]) != ("mp3", "22050", 1):
        raise ValueError("Unexpected MP3 contract")
    if float(metadata[NAMES[0]]["format"]["duration"]) != 30.0:
        raise ValueError("The square video must be exactly 30 seconds")
    for name, duration, tolerance in (
        (NAMES[1], 8, 0.1),
        (NAMES[3], 1.2, 0.001),
        (NAMES[4], 1, 0.001),
        (NAMES[5], 1.023, 0.001),
    ):
        if abs(float(metadata[name]["format"]["duration"]) - duration) > tolerance:
            raise ValueError(f"Unexpected duration: {name}")
    if any(stream["time_base"] != "1/1000" for stream in metadata[NAMES[5]]["streams"]):
        raise ValueError("Unexpected Matroska time base")
    if int(metadata[NAMES[3]]["streams"][0]["bit_rate"]) < 10000000:
        raise ValueError("The high bitrate fixture must exceed 10 Mbit/s")
    if (destination / NAMES[4]).stat().st_size >= 100000:
        raise ValueError("The HEVC fixture must fit the bounded upload test")
    hevc_frames = json.loads(
        run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(destination / NAMES[4]),
            ]
        )
    )["frames"]
    hevc_times = [float(frame["best_effort_timestamp_time"]) for frame in hevc_frames]
    if any(abs(actual - index / 24) > 0.000001 for index, actual in enumerate(hevc_times)):
        raise ValueError("Unexpected HEVC frame timestamps")
    for name in NAMES:
        if not name.endswith(".jpg"):
            run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(destination / name),
                    "-map",
                    "0",
                    "-f",
                    "null",
                    "-",
                ]
            )


def generate(destination: Path, engine_image_id: str) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    ffmpeg_version = run(["ffmpeg", "-version"])
    if not ffmpeg_version.startswith("ffmpeg version 7.1."):
        raise ValueError("Use the project's pinned FFmpeg 7.1 container")
    with tempfile.TemporaryDirectory(prefix="media-fixture-source-") as temporary:
        source = Path(temporary)
        write_portrait(destination / NAMES[2])
        write_tones(source / "mono.wav", sample_rate=22050, duration=8, stereo=False)
        write_tones(source / "stereo.wav", sample_rate=44100, duration=30, stereo=True)
        write_frame_labels(source)
        encode(
            ["-i", str(source / "mono.wav"), "-c:a", "libmp3lame", "-b:a", "128k"],
            destination / NAMES[1],
        )
        encode(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=720x720:rate=30:duration=30",
                "-i",
                str(source / "stereo.wav"),
                "-framerate",
                "30",
                "-i",
                str(source / "label-%04d.png"),
                "-filter_complex",
                f"[0:v]noise=alls=3:allf=t:all_seed={SEED}[pattern];"
                "[pattern][2:v]overlay=x=0:y=0:shortest=1[v]",
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "24",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-t",
                "30",
            ],
            destination / NAMES[0],
        )
        encode(
            [
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=1920x1080:rate=30:duration=1.2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:sample_rate=44100:duration=1.2",
                "-vf",
                f"noise=alls=20:allf=t:all_seed={SEED}",
                "-c:v",
                "libx264",
                "-b:v",
                "35M",
                "-minrate",
                "35M",
                "-maxrate",
                "35M",
                "-bufsize",
                "70M",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                "-shortest",
            ],
            destination / NAMES[3],
        )
        for name, frequency, video_args, container_args in (
            (
                NAMES[4],
                880,
                [
                    "-c:v",
                    "libx265",
                    "-preset",
                    "ultrafast",
                    "-x265-params",
                    "log-level=error:pools=none:frame-threads=1",
                    "-crf",
                    "32",
                ],
                ["-movflags", "+faststart"],
            ),
            (NAMES[5], 660, ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"], []),
        ):
            encode(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=640x360:rate=24:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={frequency}:sample_rate=44100:duration=1",
                    *video_args,
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    *container_args,
                    "-shortest",
                ],
                destination / name,
            )
    metadata = {name: describe(destination / name) for name in NAMES}
    validate(destination, metadata)
    checksums = [
        {
            "name": name,
            "size_bytes": (destination / name).stat().st_size,
            "sha256": hashlib.sha256((destination / name).read_bytes()).hexdigest(),
        }
        for name in NAMES
    ]
    (destination / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n")
    manifest = {
        "schema_version": 1,
        "generator": "scripts/generate_fixtures.py",
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "engine_image_id": engine_image_id,
        "versions": {
            "ffmpeg": ffmpeg_version.splitlines()[0],
            "ffprobe": run(["ffprobe", "-version"]).splitlines()[0],
            "python": platform.python_version(),
            "pillow": pillow_version,
            "littlecms": ImageCms.core.littlecms_version,
        },
        "parameters": {
            "seed": SEED,
            "encoder_threads": 1,
            "square_crf": 24,
            "square_noise_strength": 3,
            "high_bitrate_noise_strength": 20,
            "high_bitrate_target": "35M",
            "jpeg_quality": 92,
            "jpeg_subsampling": 0,
            "mp3_segments_hz": [440, 880, 0, 1320],
            "mp3_segment_seconds": 2,
            "stereo_channels_hz": [440, 880],
            "fade_seconds": 0.025,
            "labels": "Pillow builtin font; frame 0000-0899 and seconds+frame/30",
        },
        "media": metadata,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "files": len(checksums),
                "total_bytes": sum(row["size_bytes"] for row in checksums),
                "engine_image_id": engine_image_id,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--engine-image-id", required=True)
    args = parser.parse_args()
    if not args.engine_image_id.startswith("sha256:") or len(args.engine_image_id) != 71:
        parser.error("--engine-image-id must be an exact sha256 image ID")
    generate(args.output_dir, args.engine_image_id)


if __name__ == "__main__":
    main()
