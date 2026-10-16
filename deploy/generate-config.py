import argparse
import importlib.util
import json
import re
import secrets
from pathlib import Path

_support_spec = importlib.util.spec_from_file_location(
    "deployment_env_support", Path(__file__).with_name("env_support.py"),
)
assert _support_spec and _support_spec.loader
env_support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(env_support)


ALIASES = {"llm_api_key": "llm_api_key", "llm_default_model": "llm_model", "llm_base_url": "llm_base_url"}


PLACEHOLDERS = frozenset({
    "replace-with-generated-token",
    "replace-with-a-random-token-of-at-least-24-characters",
    "REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_24_CHARACTERS",
    "replace-with-your-private-api-key",
    "replace-with-your-model-name",
    "https://provider.example/v1",
})
OPTIONAL_MODEL_STRINGS = frozenset({"llm_api_key", "llm_model", "llm_base_url"})


def is_placeholder(value):
    return isinstance(value, str) and value in PLACEHOLDERS


def generate(environment_path, output_path=None, public_base_url=None):
    environment_path = Path(environment_path)
    environment_exists = environment_path.exists()
    template = Path(__file__).resolve().parents[1] / ".env.example"
    defaults = env_support.read_env_file(template)
    original = environment_path.read_text() if environment_exists else template.read_text()
    values = env_support.read_env_file(environment_path) if environment_exists else defaults
    output_path = Path(output_path) if output_path is not None else Path(values.get(
        "MEDIA_CONFIG_PATH", defaults["MEDIA_CONFIG_PATH"],
    ))
    if environment_path.resolve() == output_path.resolve():
        raise ValueError("environment and structural configuration must use separate files")
    example = Path(__file__).with_name("media-config.example.json")
    existing_config = output_path.exists()
    config = json.loads((output_path if existing_config else example).read_text())
    if not isinstance(config, dict):
        raise ValueError("service configuration must be an object")
    callers = config.get("callers", {})
    if not isinstance(callers, dict):
        raise ValueError("callers must be an object")
    if callers:
        caller = config.get("service_caller")
        if caller is None and len(callers) == 1:
            caller = next(iter(callers))
        if caller not in callers:
            raise ValueError("multiple callers require an explicit service_caller")
        legacy_tokens = callers[caller].get("tokens", [])
        if "service_token" not in config and legacy_tokens:
            config["service_token"] = legacy_tokens[0]
        for account in callers.values():
            account.pop("tokens", None)
    selected = {ALIASES[key.lower()]: value for key, value in values.items()
                if key.lower() in ALIASES}
    selected.update({key[6:].lower(): value for key, value in values.items()
                     if key.startswith("MEDIA_") and key[6:].lower() in env_support.ENV_ONLY_FIELDS})
    changes = {key: value for key, value in defaults.items() if key not in values}
    # 旧别名补成 Compose 白名单中的规范名称，保留现有文件内容。
    changes.update({"MEDIA_" + name.upper(): value for name, value in selected.items()
                    if "MEDIA_" + name.upper() not in values})
    changes["MEDIA_CONFIG_PATH"] = str(output_path.resolve())
    if public_base_url is not None:
        changes["MEDIA_PUBLIC_BASE_URL"] = public_base_url.rstrip("/")
    elif not environment_exists or "MEDIA_PUBLIC_BASE_URL" not in values:
        changes["MEDIA_PUBLIC_BASE_URL"] = config.get(
            "public_base_url", defaults["MEDIA_PUBLIC_BASE_URL"],
        )
    conflicts = False
    for name in env_support.ENV_ONLY_FIELDS:
        old = config.get(name)
        if name not in config:
            continue
        current = selected.get(name)
        # 模板的空模型字段尚未配置；false 开关和其他明确值仍优先于旧 JSON。
        model_unconfigured = name in OPTIONAL_MODEL_STRINGS and current == ""
        if (environment_exists and current is not None and not is_placeholder(current)
                and not model_unconfigured):
            old_text = "null" if old is None else str(old).lower() if isinstance(old, bool) else str(old)
            conflicts |= current != old_text
        else:
            changes["MEDIA_" + name.upper()] = old
    token = changes.get("MEDIA_SERVICE_TOKEN", selected.get("service_token"))
    if (token is None and "MEDIA_SERVICE_TOKEN" not in changes) or is_placeholder(token):
        changes["MEDIA_SERVICE_TOKEN"] = secrets.token_urlsafe(48)
    elif token == "":
        raise ValueError("service token must be explicit; use null for open access")
    lines = original.splitlines(keepends=True)
    replaced = set()
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in changes:
            key = match.group(1)
            lines[index] = key + "=" + env_support.encode_value(changes[key]) + "\n"
            replaced.add(key)
    content = "".join(lines)
    if content and not content.endswith("\n"):
        content += "\n"
    content += "".join(key + "=" + env_support.encode_value(value) + "\n"
                       for key, value in sorted(changes.items()) if key not in replaced)
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_path.parent.stat().st_mode & 0o077:
        raise ValueError("configuration directory must be private (0700)")
    environment_path.parent.mkdir(parents=True, exist_ok=True)
    # 先持久化唯一凭据来源，随后移除旧 JSON 字段；中断后可安全重跑。
    env_support.atomic_write(environment_path, content, 0o600)
    config = {key: value for key, value in config.items() if key not in env_support.ENV_ONLY_FIELDS}
    if "public_base_url" in config or not existing_config:
        config["public_base_url"] = changes.get("MEDIA_PUBLIC_BASE_URL", values.get("MEDIA_PUBLIC_BASE_URL"))
    env_support.atomic_write(output_path, json.dumps(config, ensure_ascii=False, indent=2) + "\n", 0o644)
    if conflicts:
        print("已有 .env 配置优先；已移除 JSON 中的重复配置")


def main():
    parser = argparse.ArgumentParser(description="生成或迁移配置；凭据仅存放在 .env")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, help="默认使用 .env 的 MEDIA_CONFIG_PATH")
    parser.add_argument("--public-base-url", help="显式更新 .env 的 MEDIA_PUBLIC_BASE_URL")
    args = parser.parse_args()
    try:
        generate(args.env_file, args.output, args.public_base_url)
    except (OSError, ValueError, TypeError, KeyError):
        parser.error("配置生成失败；请检查文件格式、目录权限与 Token 设置（未输出配置内容）")
    print("已生成或迁移 .env 与结构配置；凭据未输出")


if __name__ == "__main__":
    main()
