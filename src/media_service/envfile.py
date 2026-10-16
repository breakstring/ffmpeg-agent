"""供服务、配置生成器和标准库客户端共用的无插值环境文件解析。"""

from __future__ import annotations

import re
from pathlib import Path

ENV_ONLY_FIELDS = frozenset({
    "service_token", "llm_enabled", "llm_api_key", "llm_model", "llm_base_url",
    "llm_output_mode", "a2a_enabled", "planning_concurrency", "max_agent_requests",
    "planning_timeout_seconds", "planning_max_output_tokens",
    "log_level", "log_model_content",
    "llm_enable_thinking",
})
_ASSIGNMENT = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)")
_MAX_FILE_BYTES = 256 * 1024


def read_env_file(path: str | Path) -> dict[str, str]:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES or b"\0" in raw:
            raise ValueError
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError, ValueError):
        raise ValueError("cannot read local environment configuration") from None
    result = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        if match is None:
            raise ValueError("invalid local environment configuration")
        key, value = match.groups()
        if key in result:
            raise ValueError("duplicate local environment configuration field")
        if value.startswith(("'", '"')):
            quote = value[0]
            decoded = []
            index = 1
            while index < len(value) and value[index] != quote:
                char = value[index]
                if quote == "'" and char == "\\":
                    end = index
                    while end < len(value) and value[end] == "\\":
                        end += 1
                    count = end - index
                    if end < len(value) and value[end] == quote and count % 2:
                        decoded.append("\\" * (count - 1) + quote)
                        index = end + 1
                    else:
                        decoded.append("\\" * count)
                        index = end
                    continue
                if quote == '"' and value[index:index + 2] == "$$":
                    decoded.append("$")
                    index += 2
                    continue
                if char == "\\" and index + 1 < len(value):
                    following = value[index + 1]
                    escapes = {"\\": "\\", quote: quote}
                    if quote == '"':
                        escapes.update({"n": "\n", "r": "\r", "t": "\t"})
                    if following in escapes:
                        char = escapes[following]
                        index += 1
                decoded.append(char)
                index += 1
            if index == len(value):
                raise ValueError("invalid local environment configuration")
            tail = value[index + 1:].strip()
            if tail and not tail.startswith("#"):
                raise ValueError("invalid local environment configuration")
            value = "".join(decoded)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[key] = value
    return result
