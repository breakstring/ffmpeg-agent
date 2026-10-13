"""API 死亡后保持容器 PID 1 存活，以检查孤儿 FFmpeg 持有的实例锁。"""

import json
import subprocess
import time
from pathlib import Path

STATE = Path("/var/lib/ffmpeg-agent/state")


def main():
    child = subprocess.Popen(["python", "/fault-tests/entry.py", "normal"])
    (STATE / "fault-api.json").write_text(json.dumps({"pid": child.pid}))
    code = child.wait()
    (STATE / "fault-api-exit.json").write_text(json.dumps({"returncode": code}))
    while True:
        time.sleep(30)


if __name__ == "__main__":
    main()
