from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Product:
    kind: Literal["file", "collection"]
    media: dict
    files: list[str]
    items: list[dict] = field(default_factory=list)
    status: Literal["succeeded", "partial"] = "succeeded"
