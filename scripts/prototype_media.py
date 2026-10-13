"""运行 P0 真实媒体原型；生成的边界文件只写入指定临时目录。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

FORMATS = "mov,matroska,mp3,wav,flac,ogg,aac,jpeg_pipe,png_pipe,webp_pipe"
ROOT = Path(__file__).resolve().parents[1]


def run(
    args: list[str], *, pass_fds: tuple[int, ...] = (), ok: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, pass_fds=pass_fds, timeout=120)
    if ok and result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): {result.stderr[-3000:]}")
    return result


def ffmpeg(args: list[str]) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args])


def safe_open(root: Path, key: str) -> int:
    parts = key.split("/")
    if not key or any(part in ("", ".", "..") for part in parts) or key.startswith("/"):
        raise ValueError("invalid relative key")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        source = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        if not stat.S_ISREG(os.fstat(source).st_mode):
            os.close(source)
            raise ValueError("not a regular file")
        return source
    finally:
        os.close(fd)


def input_args(fd: int) -> list[str]:
    return ["-protocol_whitelist", "fd", "-format_whitelist", FORMATS, "-fd", str(fd), "-i", "fd:"]


def probe(path: Path, frames: bool = False) -> dict:
    fd = safe_open(path.parent, path.name)
    try:
        extra = ["-show_frames", "-select_streams", "v:0"] if frames else []
        result = run(
            [
                "ffprobe",
                "-v",
                "error",
                *input_args(fd),
                "-show_format",
                "-show_streams",
                *extra,
                "-of",
                "json",
            ],
            pass_fds=(fd,),
        )
        return json.loads(result.stdout)
    finally:
        os.close(fd)


def generate(destination: Path) -> None:
    from PIL import Image

    destination.mkdir(parents=True, exist_ok=True)
    base = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=5:duration=0.8",
        "-an",
        "-c:v",
        "libx264",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
    ]
    ffmpeg([*base, str(destination / "known_pts.mp4")])
    ffmpeg([*base, "-output_ts_offset", "2", str(destination / "nonzero_start.mp4")])
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=1",
            "-vf",
            "select='eq(n,0)+eq(n,2)+eq(n,6)+eq(n,8)'",
            "-fps_mode",
            "vfr",
            "-an",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            str(destination / "variable_pts.mp4"),
        ]
    )
    ffmpeg(
        [
            "-display_rotation",
            "90",
            "-i",
            str(destination / "known_pts.mp4"),
            "-c",
            "copy",
            str(destination / "rotated.mp4"),
        ]
    )
    ffmpeg(
        [
            "-i",
            str(destination / "known_pts.mp4"),
            "-vf",
            "setsar=2/1",
            "-c:v",
            "libx264",
            str(destination / "non_square_pixels.mp4"),
        ]
    )
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=65x49:rate=5:duration=0.4",
            "-pix_fmt",
            "yuv444p",
            "-c:v",
            "ffv1",
            str(destination / "odd_dimensions.mkv"),
        ]
    )
    ffmpeg(
        [
            "-i",
            str(destination / "known_pts.mp4"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.8",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.8",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(destination / "two_audio_tracks.mp4"),
        ]
    )
    (destination / "zero.mp4").write_bytes(b"")
    (destination / "truncated.mp4").write_bytes((destination / "known_pts.mp4").read_bytes()[:100])
    (destination / "corrupt.mp4").write_bytes(b"this is not media\n")
    (destination / "external.m3u8").write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/private.ts\n#EXT-X-ENDLIST\n"
    )
    (destination / "external.ffconcat").write_text(
        "ffconcat version 1.0\nfile 'file:/etc/passwd'\n"
    )
    image = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[274] = 6
    image.save(destination / "orientation_6.jpg", exif=exif)
    Image.new("RGBA", (40, 20), (255, 0, 0, 0)).save(destination / "transparent.png")
    image.save(destination / "static.webp")
    image.save(
        destination / "animated.webp",
        save_all=True,
        append_images=[Image.new("RGB", (40, 20), "blue")],
        duration=100,
        loop=0,
    )
    image.save(destination / "multi_page.tiff", save_all=True, append_images=[image])
    Image.new("CMYK", (40, 20)).save(destination / "cmyk.jpg")
    Image.new("I;16", (40, 20)).save(destination / "16bit.png")
    # 小图用于以低于其像素数的测试限额检验拒绝，避免普通测试制造巨型图片。
    Image.new("RGB", (101, 100)).save(destination / "pixel_limit.png")
    image.save(destination / "icc.jpg", icc_profile=b"unsupported-test-profile")
    rows = [
        {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(destination.iterdir())
        if path.is_file() and path.name != "generated-checksums.json"
    ]
    (destination / "generated-checksums.json").write_text(json.dumps(rows, indent=2) + "\n")


def image_resize(
    path: Path,
    output: Path,
    *,
    size: tuple[int, int],
    fit: str,
    upscale: bool = False,
    pixel_limit: int = 40_000_000,
) -> dict:
    from PIL import Image, ImageCms, ImageOps

    with Image.open(path) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
            raise ValueError("unsupported format or animation")
        if source.width * source.height > pixel_limit or source.mode not in {
            "1",
            "L",
            "LA",
            "P",
            "RGB",
            "RGBA",
        }:
            raise ValueError("unsupported pixels, mode or ICC")
        image = ImageOps.exif_transpose(source)
        image.load()
        if source.info.get("icc_profile"):
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info["icc_profile"]))
                if profile.profile.xcolor_space.strip() != "RGB":
                    raise ValueError("unsupported ICC color space")
                alpha = (
                    image.convert("RGBA").getchannel("A")
                    if image.mode in {"RGBA", "LA", "P"}
                    else None
                )
                image = ImageCms.profileToProfile(
                    image.convert("RGB"), profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
                )
                if alpha is not None:
                    image.putalpha(alpha)
            except (OSError, ImageCms.PyCMSError) as error:
                raise ValueError("invalid ICC profile") from error
        elif image.mode == "P":
            image = image.convert("RGBA")
        if fit == "cover":
            if not upscale and (size[0] > image.width or size[1] > image.height):
                raise ValueError("cover requires upscaling")
            image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        else:
            scale = min(size[0] / image.width, size[1] / image.height)
            if not upscale:
                scale = min(scale, 1)
            dims = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
            image = image.resize(dims, Image.Resampling.LANCZOS)
        if output.suffix.lower() == ".jpg":
            base = Image.new("RGB", image.size, "white")
            if image.mode in {"RGBA", "LA", "P"}:
                rgba = image.convert("RGBA")
                base.paste(rgba, mask=rgba.getchannel("A"))
            else:
                base.paste(image.convert("RGB"))
            image = base
        image.info.clear()
        image.save(output)
        return {"width": image.width, "height": image.height}


def verify(destination: Path) -> dict:
    from PIL import Image
    from PIL import __version__ as pillow_version

    checks: dict = {}
    fixtures = ROOT / "tests" / "fixtures"
    expected = json.loads((fixtures / "checksums.json").read_text())
    for item in expected:
        path = fixtures / item["name"]
        assert path.stat().st_size == item["size_bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        probe(path)
    checks["six_fixture_hashes_and_fd_probe"] = "passed"
    a = destination / "replace_target.mp4"
    shutil.copyfile(destination / "known_pts.mp4", a)
    fd = safe_open(destination, a.name)
    try:
        a.rename(destination / "old_inode.mp4")
        shutil.copyfile(fixtures / "Feline_Apocalypse.mp4", a)
        report = json.loads(
            run(
                ["ffprobe", "-v", "error", *input_args(fd), "-show_streams", "-of", "json"],
                pass_fds=(fd,),
            ).stdout
        )
        assert report["streams"][0]["width"] == 64
    finally:
        os.close(fd)
    checks["opened_fd_survives_path_replacement"] = "passed"
    link = destination / "symlink.mp4"
    link.symlink_to(destination / "known_pts.mp4")
    (destination / "directory").mkdir()
    os.mkfifo(destination / "fifo")
    rejected = []
    for name in ["../known_pts.mp4", "/etc/passwd", link.name, "directory", "fifo", ".", ""]:
        try:
            descriptor = safe_open(destination, name)
        except OSError, ValueError:
            rejected.append(name)
        else:
            os.close(descriptor)
            raise AssertionError(f"unsafe path accepted: {name}")
    checks["path_rejections"] = len(rejected)
    for name in ["external.m3u8", "external.ffconcat", "zero.mp4", "truncated.mp4", "corrupt.mp4"]:
        try:
            probe(destination / name)
        except RuntimeError:
            continue
        raise AssertionError(f"invalid input accepted: {name}")
    checks["external_and_invalid_media_rejected"] = "passed"
    external = safe_open(destination, "external.ffconcat")
    try:
        blocked = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "fd",
                "-format_whitelist",
                "concat",
                "-f",
                "concat",
                "-safe",
                "0",
                "-fd",
                str(external),
                "-i",
                "fd:",
                "-show_format",
            ],
            pass_fds=(external,),
            ok=False,
        )
        assert blocked.returncode != 0 and "not on whitelist" in blocked.stderr
    finally:
        os.close(external)
    checks["nested_file_protocol_blocked_even_if_demuxer_allowed"] = "passed"
    rotation = probe(destination / "rotated.mp4")["streams"][0]
    assert any(abs(item.get("rotation", 0)) == 90 for item in rotation.get("side_data_list", []))
    source = safe_open(destination, "rotated.mp4")
    try:
        run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                *input_args(source),
                "-frames:v",
                "1",
                str(destination / "rotated.png"),
            ],
            pass_fds=(source,),
        )
    finally:
        os.close(source)
    with Image.open(destination / "rotated.png") as rotated:
        assert rotated.size == (48, 64)
    sar = probe(destination / "non_square_pixels.mp4")["streams"][0]
    odd = probe(destination / "odd_dimensions.mkv")["streams"][0]
    assert sar["sample_aspect_ratio"] == "2:1"
    assert (odd["width"], odd["height"]) == (65, 49)
    checks["rotation_sar_odd_metadata"] = {
        "rotation_output": [48, 64],
        "sar": "2:1",
        "odd_input": [65, 49],
    }
    pts = {}
    for name in ["known_pts.mp4", "nonzero_start.mp4", "variable_pts.mp4"]:
        report = probe(destination / name, frames=True)
        timeline = [float(frame["best_effort_timestamp_time"]) for frame in report["frames"]]
        normalized = [round((value - timeline[0]) * 1000) for value in timeline]
        assert normalized == (
            [0, 200, 600, 800] if name == "variable_pts.mp4" else [0, 200, 400, 600]
        )
        results = []
        for index, requested in enumerate([0, 201, 600, 601, 800, 201]):
            output = destination / f"{name}.{index}.png"
            source = safe_open(destination, name)
            try:
                process = run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "info",
                        "-nostdin",
                        "-y",
                        *input_args(source),
                        "-map",
                        "0:v:0",
                        "-vf",
                        f"setpts=PTS-STARTPTS,select='gte(t,{requested / 1000})',showinfo",
                        "-frames:v",
                        "1",
                        "-fps_mode",
                        "passthrough",
                        str(output),
                    ],
                    pass_fds=(source,),
                )
            finally:
                os.close(source)
            matched = re.findall(r"\bn:\s*0\s+pts:.*?pts_time:([\d.-]+)", process.stderr)
            actual = round(float(matched[0]) * 1000) if matched else None
            expected_point = next((value for value in normalized if value >= requested), None)
            assert actual == expected_point, (name, requested, actual, expected_point)
            assert output.exists() == (actual is not None)
            results.append(
                {"index": index, "requested_time_ms": requested, "actual_time_ms": actual}
            )
        pts[name] = {"normalized_frame_pts_ms": normalized, "results": results}
    checks["known_pts_extraction"] = pts
    transcodes = []
    for name in [
        "Feline_Apocalypse.mp4",
        "ffmpeg_high_bitrate_1080p.mp4",
        "ffmpeg_hevc_640x360.mp4",
        "ffmpeg_h264_640x360.mkv",
    ]:
        path = fixtures / name
        output = destination / (path.stem + "-web.mp4")
        source = safe_open(fixtures, name)
        try:
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    *input_args(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-vf",
                    "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=30",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "23",
                    "-pix_fmt",
                    "yuv420p",
                    "-threads",
                    "2",
                    "-filter_threads",
                    "1",
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
                    str(output),
                ],
                pass_fds=(source,),
            )
        finally:
            os.close(source)
        report = probe(output)
        video = next(stream for stream in report["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in report["streams"] if stream["codec_type"] == "audio")
        assert (
            video["codec_name"] == "h264"
            and video["pix_fmt"] == "yuv420p"
            and video["avg_frame_rate"] == "30/1"
        )
        assert (
            video["width"] <= 1280
            and video["height"] <= 720
            and video["width"] % 2 == video["height"] % 2 == 0
        )
        assert (
            audio["codec_name"] == "aac"
            and audio["sample_rate"] == "48000"
            and audio["channels"] == 2
        )
        transcodes.append(
            {
                "source": name,
                "width": video["width"],
                "height": video["height"],
                "frame_rate": video["avg_frame_rate"],
            }
        )
    checks["web_720p_v1"] = transcodes
    for suffix, codec, rate, channels in [("m4a", "aac", 48000, 2), ("wav", "pcm_s16le", 16000, 1)]:
        for input_path in [fixtures / "222.mp3", destination / "two_audio_tracks.mp4"]:
            output = destination / (input_path.stem + "-audio." + suffix)
            source = safe_open(input_path.parent, input_path.name)
            try:
                run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        *input_args(source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-c:a",
                        codec,
                        "-b:a",
                        "128k",
                        "-ar",
                        str(rate),
                        "-ac",
                        str(channels),
                        str(output),
                    ],
                    pass_fds=(source,),
                )
            finally:
                os.close(source)
            stream = probe(output)["streams"][0]
            assert (
                stream["codec_name"] == codec
                and int(stream["sample_rate"]) == rate
                and stream["channels"] == channels
            )
    assert not any(
        stream["codec_type"] == "audio"
        for stream in probe(destination / "known_pts.mp4")["streams"]
    )
    checks["audio_presets_and_missing_track"] = "passed"
    assert image_resize(
        destination / "orientation_6.jpg",
        destination / "oriented.png",
        size=(40, 40),
        fit="contain",
    ) == {"width": 20, "height": 40}
    image_resize(
        destination / "transparent.png", destination / "white.jpg", size=(20, 20), fit="contain"
    )
    with Image.open(destination / "white.jpg") as image:
        assert image.getpixel((0, 0)) == (255, 255, 255) and not image.getexif()
    for name in ["animated.webp", "multi_page.tiff", "cmyk.jpg", "16bit.png", "icc.jpg"]:
        try:
            image_resize(
                destination / name, destination / "rejected.png", size=(10, 10), fit="contain"
            )
        except ValueError:
            continue
        raise AssertionError(f"unsupported image accepted: {name}")
    for path, options in [
        (destination / "pixel_limit.png", {"pixel_limit": 10000}),
        (destination / "static.webp", {"fit": "cover", "size": (80, 80)}),
    ]:
        kwargs = {"size": (10, 10), "fit": "contain", **options}
        try:
            image_resize(path, destination / "rejected.png", **kwargs)
        except ValueError:
            continue
        raise AssertionError("image boundary accepted")
    assert image_resize(
        fixtures / "haojiao.jpg", destination / "haojiao-small.png", size=(640, 640), fit="contain"
    ) == {"width": 480, "height": 640}
    with Image.open(destination / "haojiao-small.png") as resized:
        assert not resized.info.get("icc_profile") and not resized.getexif()
    assert image_resize(
        destination / "static.webp", destination / "cover.png", size=(10, 10), fit="cover"
    ) == {"width": 10, "height": 10}
    assert image_resize(
        destination / "static.webp",
        destination / "upscaled.png",
        size=(80, 40),
        fit="contain",
        upscale=True,
    ) == {"width": 80, "height": 40}
    checks["image_orientation_alpha_limits_modes"] = "passed"
    checks["display_p3_icc_to_srgb"] = {
        "source": "haojiao.jpg",
        "output_size": [480, 640],
        "output_metadata_removed": True,
    }
    return {
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": sys.version.split()[0],
        "pillow": pillow_version,
        "ffmpeg": run(["ffmpeg", "-version"]).stdout.splitlines()[0],
        "ffprobe": run(["ffprobe", "-version"]).stdout.splitlines()[0],
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--generate-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="media-p0-") if args.output_dir is None else None
    destination = args.output_dir or Path(temporary.name)
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit("output directory must be empty")
    generate(destination)
    if not args.generate_only:
        report = verify(destination)
        payload = json.dumps(report, indent=2) + "\n"
        if args.report:
            args.report.write_text(payload)
        print(payload)
    else:
        print(f"generated boundary fixtures in {destination}")
    if temporary:
        temporary.cleanup()


if __name__ == "__main__":
    main()
