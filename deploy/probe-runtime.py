"""记录基础引擎真实能力，不作为服务端到端验收。"""

import asyncio
import importlib.metadata
import json
import platform
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import aiohttp
import aiosqlite
from fastapi import FastAPI
from PIL import Image, features
from pydantic import BaseModel, ConfigDict


def command(*args: str) -> str:
    return subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)


async def check_sqlite(path: Path) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=DELETE")
        await db.execute("PRAGMA synchronous=FULL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        await db.execute("INSERT INTO probe VALUES (?)", ("ok",))
        await db.commit()
        async with db.execute("SELECT value FROM probe") as cursor:
            assert await cursor.fetchone() == ("ok",)
    async with aiohttp.ClientSession(trust_env=False) as client:
        assert not client.trust_env


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


def main() -> None:
    app = FastAPI()

    @app.post("/probe")
    def endpoint(body: Body) -> Body:
        return body

    assert app.openapi()["components"]["schemas"]["Body"]["additionalProperties"] is False
    with tempfile.TemporaryDirectory(prefix="media-baseline-") as temp:
        root = Path(temp)
        asyncio.run(check_sqlite(root / "probe.sqlite3"))
        video = root / "probe.mp4"
        command(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=0.3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.3",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-y",
            str(video),
        )
        metadata = json.loads(
            command("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video))
        )
        assert metadata["streams"][0]["codec_name"] == "h264"
        assert metadata["streams"][1]["codec_name"] == "aac"
        command(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-vn",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(root / "probe.wav"),
        )
        for extension in ("png", "jpg", "webp"):
            output = root / f"image.{extension}"
            Image.new("RGB", (16, 12), "red").save(output)
            with Image.open(output) as decoded:
                decoded.load()
                assert decoded.size == (16, 12)
    report = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "packages": {
            name: importlib.metadata.version(name)
            for name in (
                "fastapi",
                "pydantic",
                "pydantic-settings",
                "uvicorn",
                "aiosqlite",
                "aiohttp",
                "Pillow",
            )
        },
        "ffmpeg": command("ffmpeg", "-version").splitlines()[0],
        "ffprobe": command("ffprobe", "-version").splitlines()[0],
        "pillow_jpeg": features.check("jpg"),
        "pillow_webp": features.check("webp"),
        "checks": [
            "strict_schema",
            "sqlite_commit",
            "h264_aac_encode_probe",
            "pcm_encode",
            "jpeg_png_webp_roundtrip",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
