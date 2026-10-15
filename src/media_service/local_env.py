import io
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from dotenv.parser import parse_stream
from pydantic import TypeAdapter, ValidationError

from media_service.config import Settings

_ALIASES = {
    "llm_api_key": "llm_api_key",
    "llm_default_model": "llm_model",
    "llm_base_url": "llm_base_url",
}
_MAX_FILE_BYTES = 256 * 1024


def _settings_values(values: Mapping[str, str | None], *, aliases: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in values.items():
        lowered = key.lower()
        if aliases:
            name = _ALIASES.get(lowered)
        else:
            name = lowered.removeprefix("media_") if lowered.startswith("media_") else None
        if name not in Settings.model_fields or value is None:
            continue
        result[name] = value
    return result


def _validated_settings(values: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in values.items():
        field = Settings.model_fields[name]
        annotation: Any = field.annotation
        if field.metadata:
            annotation = Annotated[annotation, *field.metadata]
        adapter = TypeAdapter(annotation)
        try:
            result[name] = adapter.validate_json(value)
        except ValidationError:
            try:
                result[name] = adapter.validate_python(value)
            except ValidationError:
                raise ValueError("invalid local environment configuration") from None
    return result


def load_local_env(path: str | Path, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """显式读取本地配置并返回 Settings 覆盖，不修改环境，也不展开变量或执行 shell。"""
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(_MAX_FILE_BYTES + 1)
        if len(content) > _MAX_FILE_BYTES or b"\0" in content:
            raise ValueError
        text = content.decode("utf-8")
    except OSError, UnicodeError, ValueError:
        raise ValueError("cannot read local environment configuration") from None

    values: dict[str, str | None] = {}
    # 直接解析，避免 dotenv 的默认变量插值与解析失败警告。
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError("invalid local environment configuration")
        if binding.key is not None:
            values[binding.key] = binding.value
    environment = os.environ if environ is None else environ
    # llm_provider 仅兼容接受；实际接口由 base URL 决定，不加载供应商专属执行器。
    merged = (_settings_values(values, aliases=True)
              | _settings_values(values, aliases=False)
              | _settings_values(environment, aliases=True)
              | _settings_values(environment, aliases=False))
    return _validated_settings(merged)
