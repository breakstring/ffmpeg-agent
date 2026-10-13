import os
from pathlib import Path

import pytest

from media_service.config import Settings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_TOKEN = "isolated-testing-token-no-business-access"


@pytest.fixture
def settings(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    return Settings(
        state_dir=tmp_path / "state",
        work_dir=tmp_path / "work",
        storages={
            "input": {"root": FIXTURES, "role": "input", "callers": ["test"]},
            "output": {"root": output, "role": "output", "callers": ["test"]},
        },
        callers={"test": {"tokens": [TEST_TOKEN], "default_output_storage": "output"}},
        runtime_mode="local",
        engine_series=os.environ.get("MEDIA_TEST_ENGINE_SERIES", "9.0"),
        disk_reserve_bytes=0,
        scheduler_interval_seconds=0.01,
        gc_interval_seconds=0.1,
        stop_grace_seconds=0.2,
    )
