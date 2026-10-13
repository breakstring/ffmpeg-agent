import argparse
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

LABEL = "io.ffmpeg-agent.verification"


def docker(*args: str, timeout: float = 90, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"docker {args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


def cleanup(owner: str, volumes: list[str], network: str) -> None:
    containers = docker("ps", "-aq", "--filter", f"label={LABEL}={owner}").split()
    for container in containers:
        info = json.loads(docker("inspect", container))[0]
        assert info["Config"]["Labels"][LABEL] == owner
        docker("rm", "--force", container)
    for volume in volumes:
        result = docker("volume", "inspect", volume, check=False)
        if result:
            assert json.loads(result)[0]["Labels"][LABEL] == owner
            docker("volume", "rm", volume)
    result = docker("network", "inspect", network, check=False)
    if result:
        assert json.loads(result)[0]["Labels"][LABEL] == owner
        docker("network", "rm", network)


def verify(image: str, mode: str) -> dict:
    root = Path(__file__).resolve().parents[1]
    owner = uuid4().hex
    project = f"ffmpeg-verify-{owner[:12]}"
    network = f"{project}-network"
    volumes = {
        part: f"{project}-{part}" for part in ("state", "work", "shared_input", "shared_output")
    }
    token = secrets.token_urlsafe(48)
    with tempfile.TemporaryDirectory(prefix="ffmpeg-compose-") as temp:
        private = Path(temp)
        config_path = private / "media-config.json"
        config = json.loads((root / "deploy/media-config.example.json").read_text())
        config["callers"]["demo"]["tokens"] = [token]
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o644)
        token_path = private / "caller-token"
        token_path.write_text(token)
        token_path.chmod(0o644)
        external_volumes = {
            name: {"external": True, "name": value} for name, value in volumes.items()
        }
        shared_network = {"verified": {"external": True, "name": network}}
        business = {
            "image": image,
            "entrypoint": ["python", "/test/shared-client.py"],
            "command": [],
            "init": True,
            "user": "10001:10001",
            "labels": {LABEL: owner},
            "environment": {"MEDIA_SERVICE_URL": "http://media:8080"},
            "networks": ["verified"],
            "secrets": [{"source": "caller_token", "target": "caller-token"}],
            "volumes": [
                "shared_input:/business/input",
                "shared_output:/business/output:ro",
                f"{root / 'tests/fixtures'}:/fixtures:ro",
                f"{root / 'deploy/shared-client.py'}:/test/shared-client.py:ro",
            ],
        }
        overlay = {
            "services": {
                "media": {
                    "image": image,
                    "labels": {LABEL: owner},
                    "networks": ["verified"],
                    "restart": "no",
                }
            },
            "volumes": external_volumes,
            "networks": shared_network,
            "secrets": {"media_config": {"file": str(config_path)}},
        }
        consumer = {
            "services": {"business": business},
            "volumes": {name: external_volumes[name] for name in ("shared_input", "shared_output")},
            "networks": shared_network,
            "secrets": {"caller_token": {"file": str(token_path)}},
        }
        if mode == "same-compose":
            overlay["services"]["business"] = business
            overlay["secrets"].update(consumer["secrets"])
        overlay_file = private / "overlay.json"
        overlay_file.write_text(json.dumps(overlay))
        consumer_file = private / "consumer.json"
        consumer_file.write_text(json.dumps(consumer))
        media_args = [
            "compose",
            "-p",
            project,
            "-f",
            str(root / "deploy/compose.yaml"),
            "-f",
            str(overlay_file),
        ]
        client_args = (
            media_args
            if mode == "same-compose"
            else [
                "compose",
                "-p",
                project + "-business",
                "-f",
                str(consumer_file),
            ]
        )
        verification_env = {"MEDIA_BIND_ADDRESS": "127.0.0.1", "MEDIA_PORT": "0"}
        previous_env = {key: os.environ.get(key) for key in verification_env}
        os.environ.update(verification_env)
        try:
            for volume in volumes.values():
                docker("volume", "create", "--label", f"{LABEL}={owner}", volume)
            docker("network", "create", "--label", f"{LABEL}={owner}", network)
            docker(*media_args, "up", "-d", "--no-build", "media")
            docker(*client_args, "up", "-d", "--no-build", "business")
            media = docker(*media_args, "ps", "-q", "media")
            client = docker(*client_args, "ps", "-aq", "business")
            exit_code = int(docker("wait", client, timeout=90))
            result = docker("logs", client)
            assert exit_code == 0, f"业务侧验收退出 {exit_code}"
            parsed = json.loads(result)
            inspections = json.loads(docker("inspect", media, client))
            projects = [
                item["Config"]["Labels"]["com.docker.compose.project"] for item in inspections
            ]
            assert (projects[0] == projects[1]) == (mode == "same-compose")
            media_mounts = {mount["Destination"]: mount for mount in inspections[0]["Mounts"]}
            client_mounts = {mount["Destination"]: mount for mount in inspections[1]["Mounts"]}
            assert media_mounts["/media/input"]["Name"] == client_mounts["/business/input"]["Name"]
            assert (
                media_mounts["/media/output"]["Name"] == client_mounts["/business/output"]["Name"]
            )
            assert not media_mounts["/media/input"]["RW"]
            assert client_mounts["/business/input"]["RW"]
            assert media_mounts["/media/output"]["RW"]
            assert not client_mounts["/business/output"]["RW"]
            assert "/var/lib/ffmpeg-agent/state" not in client_mounts
            assert "/var/lib/ffmpeg-agent/work" not in client_mounts
            return {
                "deployment": mode,
                "image": image,
                "image_id": inspections[0]["Image"],
                **parsed,
                "checks": parsed["checks"]
                + ["actual_shared_volume_identity", "private_state_and_work"],
            }
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            cleanup(owner, list(volumes.values()), network)


def main() -> None:
    parser = argparse.ArgumentParser(description="同 Compose / 跨 Compose 的真实共享卷验收")
    parser.add_argument("--image", default="ffmpeg-agent:v1-local")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    reports = [verify(args.image, mode) for mode in ("same-compose", "cross-compose")]
    text = json.dumps(reports, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
