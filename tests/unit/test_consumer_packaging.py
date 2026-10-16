from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ("ffmpeg-agent-connect", "ffmpeg-agent-media")


def _run(script: Path, *arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith(("PYTHON", "MEDIA_")):
            environment.pop(name)
    return subprocess.run(
        [sys.executable, "-S", "-B", str(script), *arguments],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_consumer_skill_copies_are_current(tmp_path: Path) -> None:
    result = _run(ROOT / "scripts/sync-consumer-skills.py", "--check", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_consumer_skill_packages_and_manifests_match() -> None:
    first, second = (ROOT / "skills" / skill / "scripts" for skill in SKILLS)
    manifest = json.loads((first / "consumer-manifest.json").read_text())
    assert (first / "consumer-manifest.json").read_bytes() == (
        second / "consumer-manifest.json"
    ).read_bytes()
    assert manifest["schema_version"] == 1
    assert manifest["generator_sha256"] == hashlib.sha256(
        (ROOT / "scripts/sync-consumer-skills.py").read_bytes()
    ).hexdigest()
    for relative, expected_sha256 in manifest["files"].items():
        content = (first / relative).read_bytes()
        assert content == (second / relative).read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        ast.parse(content, feature_version=(3, 10))
    assert (ROOT / "consumer/ffmpeg_agent_client/envfile.py").read_bytes() == (
        ROOT / "src/media_service/envfile.py"
    ).read_bytes()


@pytest.mark.parametrize("skill", SKILLS)
def test_each_skill_runs_independently_without_site_packages(tmp_path: Path, skill: str) -> None:
    standalone = tmp_path / "installed" / skill
    shutil.copytree(ROOT / "skills" / skill, standalone, ignore=shutil.ignore_patterns("__pycache__"))
    cwd = tmp_path / "unrelated-working-directory"
    cwd.mkdir()
    script = standalone / "scripts/media_client.py"
    result = _run(script, "--help", cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    commands = re.search(r"\{([a-z][a-z0-9_,\-]*)\}", result.stdout)
    assert commands is not None, "CLI help must list its available commands"
    for command in commands.group(1).split(","):
        result = _run(script, "--env-file", "does-not-exist.env", command, "--help", cwd=cwd)
        assert result.returncode == 0, f"{command}: {result.stdout}{result.stderr}"
    assert not (cwd / "does-not-exist.env").exists()


def _isolated_generator(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    for relative in ("consumer", "skills"):
        shutil.copytree(ROOT / relative, repository / relative,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("scripts/sync-consumer-skills.py", "src/media_service/envfile.py"):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return repository


def test_sync_check_detects_drift_without_writing(tmp_path: Path) -> None:
    repository = _isolated_generator(tmp_path)
    generated = repository / "skills/ffmpeg-agent-media/scripts/ffmpeg_agent_client/envfile.py"
    generated.write_text("# local edit\n")
    script = repository / "scripts/sync-consumer-skills.py"
    result = _run(script, "--check", cwd=tmp_path)
    assert result.returncode == 1
    assert generated.read_text() == "# local edit\n"
    result = _run(script, cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert generated.read_bytes() == (repository / "src/media_service/envfile.py").read_bytes()
    result = _run(script, "--check", cwd=tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sync_refuses_unknown_python_files_without_deleting(tmp_path: Path) -> None:
    repository = _isolated_generator(tmp_path)
    unknown = repository / "skills/ffmpeg-agent-connect/scripts/ffmpeg_agent_client/unknown.py"
    unknown.write_text("# consumer-owned file\n")
    before = (unknown.parent / "envfile.py").read_bytes()
    script = repository / "scripts/sync-consumer-skills.py"
    for arguments in (("--check",), ()):
        result = _run(script, *arguments, cwd=tmp_path)
        assert result.returncode == 1
        assert "unregistered generated Python files" in result.stdout
        assert unknown.read_text() == "# consumer-owned file\n"
        assert (unknown.parent / "envfile.py").read_bytes() == before


def test_sync_rejects_symlinked_generated_targets(tmp_path: Path) -> None:
    repository = _isolated_generator(tmp_path)
    generated = repository / "skills/ffmpeg-agent-media/scripts/media_client.py"
    outside = tmp_path / "consumer-owned-file.py"
    outside.write_text("# do not overwrite\n")
    generated.unlink()
    generated.symlink_to(outside)
    result = _run(repository / "scripts/sync-consumer-skills.py", cwd=tmp_path)
    assert result.returncode == 1
    assert "symlink" in result.stdout
    assert outside.read_text() == "# do not overwrite\n"


def test_entrypoint_rejects_old_python_before_importing_client(tmp_path: Path) -> None:
    entrypoint = tmp_path / "media_client.py"
    shutil.copyfile(ROOT / "skills/ffmpeg-agent-connect/scripts/media_client.py", entrypoint)
    runner = tmp_path / "simulate_old_python.py"
    runner.write_text(
        "import runpy, sys\n"
        "sys.version_info = (3, 9, 0)\n"
        f"runpy.run_path({str(entrypoint)!r}, run_name='__main__')\n"
    )
    result = _run(runner, cwd=tmp_path)
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"ok": False, "code": "PYTHON_310_REQUIRED"}
    assert result.stderr == ""
