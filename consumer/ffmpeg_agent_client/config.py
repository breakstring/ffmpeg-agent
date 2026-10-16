import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .envfile import read_env_file
from .errors import ConsumerError


@dataclass(frozen=True)
class Connection:
    base: str
    token: str | None = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.base, str) or not self.base or not self.base.isascii():
            raise ConsumerError("INVALID_SERVICE_URL")
        if any(ord(char) <= 32 or ord(char) == 127 for char in self.base):
            raise ConsumerError("INVALID_SERVICE_URL")
        try:
            url = urlsplit(self.base)
            path = unquote(url.path)
            invalid = (
                url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or "?" in self.base or "#" in self.base or "\\" in self.base
                or (url.port is not None and not 1 <= url.port <= 65535)
                or any(part in {".", ".."} for part in path.split("/"))
                or "//" in path or "\\" in path
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
            )
        except ValueError:
            raise ConsumerError("INVALID_SERVICE_URL") from None
        if invalid:
            raise ConsumerError("INVALID_SERVICE_URL")
        if self.token is not None and (
            not isinstance(self.token, str) or not 24 <= len(self.token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in self.token)
            or self.token in {"replace-with-a-random-token-of-at-least-24-characters",
                              "replace-with-generated-token",
                              "REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_24_CHARACTERS"}
        ):
            raise ConsumerError("INVALID_SERVICE_TOKEN")
        object.__setattr__(self, "base", self.base.rstrip("/"))


def load_connection(env_file: Path, environ: Mapping[str, str] | None = None) -> Connection:
    environment = os.environ if environ is None else environ
    try:
        values = read_env_file(env_file) if env_file.exists() else {}
    except (OSError, ValueError):
        raise ConsumerError("INVALID_ENV_FILE") from None
    base = environment.get("MEDIA_PUBLIC_BASE_URL", values.get("MEDIA_PUBLIC_BASE_URL"))
    token = environment.get("MEDIA_SERVICE_TOKEN", values.get("MEDIA_SERVICE_TOKEN"))
    if base is None:
        raise ConsumerError("MISSING_SERVICE_URL")
    if token is None:
        raise ConsumerError("MISSING_SERVICE_TOKEN")
    return Connection(base, None if token == "null" else token)
