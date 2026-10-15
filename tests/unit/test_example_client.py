import argparse
import hashlib
import importlib.util
import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def example():
    path = Path(__file__).resolve().parents[2] / "examples/client_common.py"
    spec = importlib.util.spec_from_file_location("example_client_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def client(example):
    return example.Client.__new__(example.Client)


class DownloadResponse(io.BytesIO):
    def __init__(self, data, *, content_length=None, fail_after=None, unpublished=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if content_length is None else content_length)}
        self.fail_after = fail_after
        self.unpublished = unpublished

    def read(self, size=-1):
        if self.unpublished is not None:
            assert not self.unpublished.exists()
        if self.fail_after is not None and self.tell() >= self.fail_after:
            raise ConnectionResetError("simulated interrupted response")
        return super().read(min(size, 4) if size >= 0 else 4)


def install_response(monkeypatch, client, responses):
    requests = []

    @contextmanager
    def response(method, path):
        assert method == "GET"
        requests.append(path)
        with responses[path] as stream:
            yield stream

    monkeypatch.setattr(client, "response", response)
    return requests


def test_download_publishes_only_complete_verified_content(client, monkeypatch, tmp_path):
    data = b"complete media payload"
    destination = tmp_path / "result.mp4"
    install_response(monkeypatch, client, {
        "/content": DownloadResponse(data, unpublished=destination),
    })

    client.download("/content", destination, len(data), hashlib.sha256(data).hexdigest())

    assert destination.read_bytes() == data
    assert list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize("failure", ["sha256", "size", "incomplete", "network"])
def test_failed_download_removes_partial_files(example, client, monkeypatch, tmp_path, failure):
    data = b"complete media payload"
    destination = tmp_path / "result.mp4"
    options = {"unpublished": destination}
    if failure == "incomplete":
        options["content_length"] = len(data) + 1
    if failure == "network":
        options["fail_after"] = 4
    response = DownloadResponse(data, **options)
    install_response(monkeypatch, client, {"/content": response})
    digest = "0" * 64 if failure == "sha256" else hashlib.sha256(data).hexdigest()
    size = len(data) + (failure == "size")
    error = ConnectionResetError if failure == "network" else example.ExampleError

    with pytest.raises(error):
        client.download("/content", destination, size, digest)

    assert response.closed
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing_kind", ["file", "symlink"])
def test_download_never_overwrites_existing_destination(
    client, monkeypatch, tmp_path, existing_kind,
):
    destination = tmp_path / "result.mp4"
    original = b"existing caller content"
    if existing_kind == "symlink":
        other = tmp_path / "original.mp4"
        other.write_bytes(original)
        destination.symlink_to(other)
    else:
        destination.write_bytes(original)
    existing = set(tmp_path.iterdir())
    data = b"replacement media"
    install_response(monkeypatch, client, {"/content": DownloadResponse(data)})

    with pytest.raises(FileExistsError):
        client.download("/content", destination, len(data), hashlib.sha256(data).hexdigest())

    assert destination.read_bytes() == original
    assert set(tmp_path.iterdir()) == existing
    assert destination.is_symlink() == (existing_kind == "symlink")


def member(index, key, data):
    return {"index": index, "member_key": key, "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "error": None}


def test_collection_download_follows_pages_and_preserves_partial_manifest(
    client, monkeypatch, tmp_path,
):
    artifact = {"artifact_id": "art_" + "a" * 32, "kind": "collection"}
    base = "/v1/artifacts/" + artifact["artifact_id"]
    first, second = b"first frame", b"second frame"
    items = [member(0, "frame-000.jpg", first),
             {"index": 1, "member_key": None, "error": {"code": "FRAME_NOT_FOUND"}},
             member(2, "frame-002.jpg", second)]
    pages = {
        base + "/members?cursor=0&limit=100": {"items": items[:2], "next_cursor": 2},
        base + "/members?cursor=2&limit=100": {"items": items[2:], "next_cursor": None},
    }
    page_requests = []

    def api(method, path):
        assert method == "GET"
        page_requests.append(path)
        return pages[path]

    monkeypatch.setattr(client, "api", api)
    downloads = install_response(monkeypatch, client, {
        base + "/files/frame-000.jpg": DownloadResponse(first),
        base + "/files/frame-002.jpg": DownloadResponse(second),
    })
    destination = tmp_path / "frames"

    client.save_artifact(artifact, destination)

    assert page_requests == list(pages)
    assert downloads == [base + "/files/frame-000.jpg", base + "/files/frame-002.jpg"]
    assert (destination / "frame-000.jpg").read_bytes() == first
    assert (destination / "frame-002.jpg").read_bytes() == second
    assert json.loads((destination / "manifest.json").read_text()) == {"items": items}
    assert {path.name for path in destination.iterdir()} == {
        "frame-000.jpg", "frame-002.jpg", "manifest.json",
    }


@pytest.mark.parametrize("unsafe_key", [
    "../outside.jpg", "nested/frame.jpg", "/outside.jpg", "..", "manifest.json", "..\\outside.jpg",
])
def test_collection_rejects_escaping_or_reserved_names_and_cleans_its_directory(
    example, client, monkeypatch, tmp_path, unsafe_key,
):
    artifact = {"artifact_id": "art_" + "b" * 32, "kind": "collection"}
    base = "/v1/artifacts/" + artifact["artifact_id"]
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"caller owned")
    data = b"valid frame"
    items = [member(0, "frame.jpg", data), member(1, unsafe_key, data)]
    monkeypatch.setattr(client, "api", lambda *_: {"items": items, "next_cursor": None})
    downloads = install_response(monkeypatch, client, {
        base + "/files/frame.jpg": DownloadResponse(data),
    })
    destination = tmp_path / "frames"

    with pytest.raises(example.ExampleError, match="Invalid collection member name"):
        client.save_artifact(artifact, destination)

    assert downloads == [base + "/files/frame.jpg"]
    assert outside.read_bytes() == b"caller owned"
    assert list(tmp_path.iterdir()) == [outside]


@pytest.mark.parametrize("next_cursor", [0, -1, "100", True])
def test_collection_rejects_nonadvancing_pagination_before_creating_destination(
    example, client, monkeypatch, tmp_path, next_cursor,
):
    monkeypatch.setattr(client, "api", lambda *_: {"items": [], "next_cursor": next_cursor})

    with pytest.raises(example.ExampleError, match="Invalid collection pagination"):
        client.save_artifact({"artifact_id": "art_" + "c" * 32, "kind": "collection"},
                             tmp_path / "frames")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing_kind", ["file", "directory", "dangling_symlink"])
def test_prepare_rejects_existing_destination_before_reading_inputs_or_network(
    example, monkeypatch, tmp_path, existing_kind,
):
    destination = tmp_path / "result"
    if existing_kind == "file":
        destination.write_bytes(b"caller owned")
    elif existing_kind == "directory":
        destination.mkdir()
    else:
        destination.symlink_to(tmp_path / "missing")
    existing = set(tmp_path.iterdir())

    def unexpected_network(*args, **kwargs):
        pytest.fail("Destination validation must precede network requests")

    monkeypatch.setattr(example.http.client, "HTTPConnection", unexpected_network)
    monkeypatch.setattr(example.http.client, "HTTPSConnection", unexpected_network)
    args = argparse.Namespace(
        destination=destination, source=tmp_path / "missing-source.mp4",
        params=tmp_path / "missing-params.json", request_id="test", job_id=None, wait_seconds=60,
    )

    with pytest.raises(example.ExampleError, match="Destination already exists"):
        example.prepare(args)

    assert set(tmp_path.iterdir()) == existing
    if existing_kind == "file":
        assert destination.read_bytes() == b"caller owned"
