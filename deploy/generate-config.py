import argparse
import json
import os
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="生成独立配置；凭据只写入文件，不输出到终端")
    parser.add_argument("--output", type=Path, default=Path("deploy/local/media-config.json"))
    parser.add_argument("--public-base-url", default="http://localhost:8080")
    args = parser.parse_args()
    example = Path(__file__).with_name("media-config.example.json")
    config = json.loads(example.read_text())
    config["service_token"] = secrets.token_urlsafe(48)
    config["service_caller"] = "demo"
    config["callers"]["demo"].pop("tokens", None)
    config["public_base_url"] = args.public_base_url.rstrip("/")
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.output.parent.stat().st_mode & 0o077:
        parser.error("配置目录必须仅当前用户可访问（权限 0700）")
    # 父目录限制宿主机访问，文件只读绑定后允许容器 UID 10001 读取。
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w") as output:
        os.fchmod(output.fileno(), 0o644)
        json.dump(config, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(f"已生成配置：{args.output}；凭据未输出")


if __name__ == "__main__":
    main()
