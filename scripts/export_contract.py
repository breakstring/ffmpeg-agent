import json
from pathlib import Path
from types import SimpleNamespace

from media_service.api import create_app
from media_service.config import Settings
from media_service.models import (
    ArtifactResult,
    ErrorEnvelope,
    FileResult,
    JobCreate,
    JobResult,
    MembersResult,
)


def main():
    # 仅构造schema，不启动服务、不创建状态库、不读取部署凭据。
    settings = Settings(
        storages={"output": {"root": "/schema/output", "role": "output", "callers": ["schema"]}},
        callers={"schema": {"tokens": ["schema-only-placeholder-no-access"], "default_output_storage": "output"}},
    )
    app = create_app(service=SimpleNamespace(settings=settings))
    root = Path(__file__).resolve().parents[1] / "docs" / "api"
    root.mkdir(exist_ok=True, parents=True)
    values = {"v1-openapi": app.openapi()}
    values.update({model.__name__: model.model_json_schema() for model in
                   (JobCreate, JobResult, ArtifactResult, MembersResult, FileResult, ErrorEnvelope)})
    for name, value in values.items():
        (root / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(f"exported {len(values)} contracts")


if __name__ == "__main__":
    main()
