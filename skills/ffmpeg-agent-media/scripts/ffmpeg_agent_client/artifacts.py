import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from .errors import ConsumerError, service_code
from .transport import CHUNK_SIZE, Client


def _identity(size: object, digest: object) -> None:
    if (type(size) is not int or size < 0 or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ConsumerError("INVALID_ARTIFACT_METADATA")


def _checked_directory(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        raise ConsumerError("UNSAFE_OUTPUT_PATH")
    for component in [*reversed(path.parents), path]:
        if not stat.S_ISDIR(component.lstat().st_mode):
            raise ConsumerError("UNSAFE_OUTPUT_PATH")
    return path


class _Directory:
    def __init__(self, path: Path, descriptor: int | None):
        self.path, self.descriptor = path, descriptor

    def open(self, name: str, flags: int) -> int:
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if self.descriptor is not None:
            return os.open(name, flags, 0o600, dir_fd=self.descriptor)
        _checked_directory(self.path)
        target = self.path / name
        if target.is_symlink():
            raise ConsumerError("UNSAFE_OUTPUT_PATH")
        return os.open(target, flags, 0o600)

    def link(self, source: str, target: str) -> None:
        if self.descriptor is not None:
            os.link(source, target, src_dir_fd=self.descriptor, dst_dir_fd=self.descriptor,
                    follow_symlinks=False)
        else:
            _checked_directory(self.path)
            os.link(self.path / source, self.path / target)

    def unlink(self, name: str) -> None:
        if self.descriptor is not None:
            os.unlink(name, dir_fd=self.descriptor)
        else:
            _checked_directory(self.path)
            (self.path / name).unlink()

    def stat(self, name: str) -> os.stat_result:
        if self.descriptor is not None:
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        _checked_directory(self.path)
        return (self.path / name).lstat()


@contextmanager
def _directory(path: Path):
    descriptor = None
    try:
        path = _checked_directory(path)
        if os.open in os.supports_dir_fd and os.link in os.supports_dir_fd:
            descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for name in path.parts[1:]:
                following = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
        yield _Directory(path, descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _existing(directory: _Directory, name: str, size: int, digest: str) -> bool:
    try:
        descriptor = directory.open(name, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        raise ConsumerError("UNSAFE_OUTPUT_PATH") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConsumerError("UNSAFE_OUTPUT_PATH")
        if info.st_size != size:
            raise ConsumerError("OUTPUT_EXISTS")
        actual = hashlib.sha256()
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            actual.update(chunk)
        if actual.hexdigest() != digest:
            raise ConsumerError("OUTPUT_EXISTS")
        current = directory.stat(name)
        if (not stat.S_ISREG(current.st_mode)
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (current.st_dev, current.st_ino, current.st_size,
                    current.st_mtime_ns, current.st_ctime_ns)):
            raise ConsumerError("OUTPUT_CHANGED")
    return True


def _publish(directory: _Directory, name: str, size: int, digest: str, write) -> bool:
    if name in {"", ".", ".."}:
        raise ConsumerError("UNSAFE_OUTPUT_PATH")
    if _existing(directory, name, size, digest):
        return True
    temporary = ".ffmpeg-" + uuid4().hex
    descriptor = directory.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # 原子发布且拒绝覆盖；临时文件与最终文件使用同一已固定的目录 FD。
            directory.link(temporary, name)
        except FileExistsError:
            return _existing(directory, name, size, digest)
        return False
    finally:
        directory.unlink(temporary)


def _download(client: Client, path: str, destination: Path, size: int, digest: str,
              headers: dict) -> dict:
    _identity(size, digest)

    def write(stream):
        actual, received = hashlib.sha256(), 0
        with client.raw_request("GET", path, headers=headers) as response:
            lengths = response.headers.get_all("Content-Length", [])
            if (response.status != 200 or len(lengths) != 1 or not lengths[0].isdigit()
                    or int(lengths[0]) != size):
                raise ConsumerError("DOWNLOAD_SIZE_MISMATCH")
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                received += len(chunk)
                if received > size:
                    raise ConsumerError("DOWNLOAD_SIZE_MISMATCH")
                actual.update(chunk)
                stream.write(chunk)
        if received != size:
            raise ConsumerError("DOWNLOAD_SIZE_MISMATCH")
        if actual.hexdigest() != digest:
            raise ConsumerError("DOWNLOAD_HASH_MISMATCH")

    with _directory(destination.parent) as directory:
        reused = _publish(directory, destination.name, size, digest, write)
    return {"path": str(destination), "size_bytes": size, "sha256": digest, "reused": reused}


def _members(client: Client, path: str, headers: dict, count: int) -> list[dict]:
    if type(count) is not int or not 0 <= count <= 10000:
        raise ConsumerError("INVALID_COLLECTION_METADATA")
    items, cursor = [], 0
    while True:
        page = client.api("GET", path + f"/members?cursor={cursor}&limit=100", headers=headers)
        batch = page.get("items")
        if not isinstance(batch, list) or len(batch) > 100 or len(items) + len(batch) > count:
            raise ConsumerError("INVALID_COLLECTION_PAGINATION")
        items.extend(batch)
        following = page.get("next_cursor")
        if following is None:
            if len(items) != count:
                raise ConsumerError("INVALID_COLLECTION_PAGINATION")
            return items
        if type(following) is not int or following != cursor + len(batch) or following <= cursor:
            raise ConsumerError("INVALID_COLLECTION_PAGINATION")
        cursor = following


def save_artifact(client: Client, artifact: dict, destination: Path,
                  lease_id: str | None = None) -> dict:
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or not re.fullmatch(r"art_[a-f0-9]{32}", artifact_id):
        raise ConsumerError("INVALID_ARTIFACT_METADATA")
    _identity(artifact.get("size_bytes"), artifact.get("sha256"))
    if lease_id is not None and not re.fullmatch(r"lease_[a-f0-9]{32}", lease_id):
        raise ConsumerError("INVALID_LEASE_ID")
    headers = {"X-Artifact-Lease-ID": lease_id} if lease_id else {}
    path = "/v1/artifacts/" + artifact_id
    result = {"artifact_id": artifact_id, "kind": artifact.get("kind"),
              "path": str(destination), "files": [], "failures": [], "partial": False}
    try:
        if artifact.get("kind") == "file":
            result["files"].append(_download(client, path + "/content", destination,
                                             artifact["size_bytes"], artifact["sha256"], headers))
            return result
        if artifact.get("kind") != "collection":
            raise ConsumerError("INVALID_ARTIFACT_METADATA")
        items = _members(client, path, headers, artifact.get("item_count"))
        sanitized, names = [], set()
        for index, item in enumerate(items):
            if not isinstance(item, dict) or type(item.get("index")) is not int:
                raise ConsumerError("INVALID_COLLECTION_METADATA")
            if item["index"] != index:
                raise ConsumerError("INVALID_COLLECTION_METADATA")
            if item.get("error"):
                error = item["error"]
                if not isinstance(error, dict):
                    raise ConsumerError("INVALID_COLLECTION_METADATA")
                failure = {"index": index, "code": service_code(error.get("code"))}
                result["failures"].append(failure)
                sanitized.append(failure)
                continue
            key = item.get("member_key")
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key)
                    or key in {".", "..", "manifest.json"} or key in names):
                raise ConsumerError("UNSAFE_COLLECTION_MEMBER")
            names.add(key)
            _identity(item.get("size_bytes"), item.get("sha256"))
            sanitized.append({"index": index, "member_key": key,
                              "size_bytes": item["size_bytes"], "sha256": item["sha256"]})
        if (type(artifact.get("member_count")) is not int
                or artifact["member_count"] != len(names)
                or sum(item.get("size_bytes", 0) for item in sanitized) != artifact["size_bytes"]):
            raise ConsumerError("INVALID_COLLECTION_METADATA")
        with _directory(destination.parent) as directory:
            try:
                if directory.descriptor is not None:
                    os.mkdir(destination.name, 0o700, dir_fd=directory.descriptor)
                else:
                    destination.mkdir(mode=0o700)
            except FileExistsError:
                pass
        with _directory(destination) as directory:
            manifest = json.dumps({"artifact_id": artifact_id, "sha256": artifact["sha256"],
                                   "items": sanitized}, ensure_ascii=False, indent=2).encode() + b"\n"
            _publish(directory, "manifest.json", len(manifest), hashlib.sha256(manifest).hexdigest(),
                     lambda stream: stream.write(manifest))
        for item in sanitized:
            if "member_key" not in item:
                continue
            key = item["member_key"]
            try:
                result["files"].append(_download(client, path + "/files/" + quote(key, safe=""),
                    destination / key, item["size_bytes"], item["sha256"], headers))
            except ConsumerError as error:
                if error.code in {"OUTPUT_EXISTS", "UNSAFE_OUTPUT_PATH", "OUTPUT_CHANGED"}:
                    raise
                result["failures"].append({"index": item["index"], "code": error.code})
        result["partial"] = bool(result["failures"])
        result["manifest"] = str(destination / "manifest.json")
        return result
    except ConsumerError:
        raise
    except (OSError, ValueError):
        raise ConsumerError("OUTPUT_UNWRITABLE") from None
