"""隔离验收与配置迁移共用的文件写入；凭据不进入命令行。"""

import importlib
import importlib.util
import json
import os
import tempfile
from pathlib import Path

try:
    envfile = importlib.import_module("media_service.envfile")
except ModuleNotFoundError:
    _spec = importlib.util.spec_from_file_location(
        "media_environment_file", Path(__file__).resolve().parents[1] / "src/media_service/envfile.py",
    )
    assert _spec and _spec.loader
    envfile = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(envfile)
ENV_ONLY_FIELDS = envfile.ENV_ONLY_FIELDS
read_env_file = envfile.read_env_file


def encode_value(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise ValueError("environment values must not contain control characters")
    if "\\" in value:
        # Compose 单引号不能表示引号前或结尾的奇数个反斜杠。
        # 双引号采用常规反转义，$$ 在 Compose 和本地解析器中均还原为 $。
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
        return '"' + escaped + '"'
    # 常见 Token/Key 用单引号，$ 不触发 Compose 变量插值。
    return "'" + value.replace("'", "\\'") + "'"


def atomic_write(path, content, mode):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_runtime_configuration(config, path):
    path = Path(path)
    environment_path = path.with_suffix(".env")
    values = read_env_file(environment_path) if environment_path.exists() else {}
    for name in ENV_ONLY_FIELDS:
        if name in config:
            values["MEDIA_" + name.upper()] = config[name]
    # 单文件绑定允许容器 UID 10001 读取；临时父目录必须保持 0700。
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("runtime configuration directory must be private")
    atomic_write(environment_path, "".join(
        key + "=" + encode_value(value) + "\n" for key, value in sorted(values.items())
    ), 0o644)
    structural = {key: value for key, value in config.items() if key not in ENV_ONLY_FIELDS}
    atomic_write(path, json.dumps(structural, ensure_ascii=False, indent=2) + "\n", 0o644)
    return environment_path


def environment_mount(path):
    return [
        "--mount", f"type=bind,src={path},dst=/run/secrets/media.env,readonly",
        "--env", "MEDIA_ENV_FILE=/run/secrets/media.env",
    ]
