import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any

from pydantic import SecretStr, TypeAdapter, ValidationError

from media_service.config import Settings
from media_service.envfile import read_env_file

_ALIASES = {
    "llm_api_key": "llm_api_key",
    "llm_default_model": "llm_model",
    "llm_base_url": "llm_base_url",
}


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
        if name == "service_token":
            if value == "null":
                result[name] = None
            elif (len(value) >= 24 and all(33 <= ord(c) <= 126 for c in value)
                  and value not in {"replace-with-a-random-token-of-at-least-24-characters",
                                    "replace-with-generated-token",
                                    "REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_24_CHARACTERS"}):
                result[name] = SecretStr(value)
            else:
                raise ValueError("invalid MEDIA_SERVICE_TOKEN; use a strong token or explicit null")
            continue
        if name in {"llm_api_key", "llm_model", "llm_base_url"} and not value:
            result[name] = None
            continue
        field = Settings.model_fields[name]
        annotation: Any = field.annotation
        if field.metadata:
            annotation = Annotated[annotation, *field.metadata]
        adapter = TypeAdapter(annotation)
        try:
            if name in {"llm_api_key", "llm_model", "llm_base_url", "llm_output_mode",
                        "public_base_url"}:
                result[name] = adapter.validate_python(value)
            else:
                result[name] = adapter.validate_json(value)
        except ValidationError:
            try:
                result[name] = adapter.validate_python(value)
            except ValidationError:
                raise ValueError("invalid local environment configuration") from None
    return result


def load_local_env(
    path: str | Path | None, *, environ: Mapping[str, str] | None = None,
    fields: frozenset[str] | None = None,
) -> dict[str, Any]:
    """显式读取本地配置并返回 Settings 覆盖，不修改环境，也不展开变量或执行 shell。"""
    values = read_env_file(path) if path is not None else {}
    environment = os.environ if environ is None else environ
    # llm_provider 仅兼容接受；实际接口由 base URL 决定，不加载供应商专属执行器。
    merged = (_settings_values(values, aliases=True)
              | _settings_values(values, aliases=False)
              | _settings_values(environment, aliases=True)
              | _settings_values(environment, aliases=False))
    if fields is not None:
        merged = {key: value for key, value in merged.items() if key in fields}
    return _validated_settings(merged)
