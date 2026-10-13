"""仅供独立容器上传恢复验收读取登记与文件状态，不输出定位路径。"""

import argparse
import json
import os
import re
import sqlite3
import time
from pathlib import Path

STATE = Path("/var/lib/ffmpeg-agent/state/state.sqlite")
UPLOADS = Path("/var/lib/ffmpeg-agent/work/_uploads")


def exists(directory: int, leaf: str) -> tuple[bool, int]:
    try:
        metadata = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False, 0
    return True, metadata.st_size


def snapshot(key: str) -> dict:
    with sqlite3.connect(f"file:{STATE}?mode=ro", uri=True, timeout=1) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id,state,path FROM uploaded_files WHERE caller=? AND upload_key=?",
            ("demo", key),
        ).fetchone()
    if row is None:
        return {"registered": False}
    file_id = row["id"]
    if not re.fullmatch(r"file_[0-9a-f]{32}", file_id) or row["path"] != f"{file_id}/content":
        raise RuntimeError("invalid_registered_identity")
    partial_exists = complete_exists = False
    partial_bytes = 0
    root = os.open(UPLOADS, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            directory = os.open(
                file_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
        except FileNotFoundError:
            directory = -1
        if directory >= 0:
            try:
                partial_exists, partial_bytes = exists(directory, "content.part")
                complete_exists, _ = exists(directory, "content")
            finally:
                os.close(directory)
    finally:
        os.close(root)
    return {"registered": True, "file_id": file_id, "state": row["state"],
            "partial_exists": partial_exists, "partial_bytes": partial_bytes,
            "complete_exists": complete_exists}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "wait-uploading"))
    parser.add_argument("--key", required=True)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not re.fullmatch(r"upload-mcp-[0-9a-f]{32}", args.key) or not 0 < args.timeout <= 30:
        raise SystemExit("invalid_test_argument")
    try:
        deadline = time.monotonic() + args.timeout
        while True:
            value = snapshot(args.key)
            if args.command == "snapshot" or (
                value.get("state") == "uploading" and value.get("partial_bytes", 0) > 0
                and not value.get("complete_exists")
            ):
                print(json.dumps(value), flush=True)
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("upload_checkpoint_not_observed")
            time.sleep(0.05)
    except Exception:
        print(json.dumps({"error": "test_inspection_failed"}), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
