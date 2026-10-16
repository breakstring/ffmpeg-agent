from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

from .errors import ConsumerError

MAX_STATE_BYTES = 4 * 1024 * 1024


def read_json(path: Path, limit: int = MAX_STATE_BYTES) -> dict:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise ConsumerError("INVALID_LOCAL_JSON") from None


def fingerprint(path: Path) -> dict:
    path = path.expanduser().absolute()
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ConsumerError("INPUT_NOT_REGULAR_FILE")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ConsumerError("INPUT_CHANGED")
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(stream.fileno())
        current = path.lstat()
        def identity(item):
            return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (identity(opened) != identity(after) or identity(after) != identity(current)
                or size != after.st_size or size == 0):
            raise ConsumerError("INPUT_CHANGED")
        return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}
    except OSError:
        raise ConsumerError("INPUT_UNREADABLE") from None


class Record:
    def __init__(self, path: Path, base: str):
        self.path = path.expanduser().absolute()
        self.base = base
        self.data: dict = {}
        self._lock = None
        self._identity = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + ".lock")
            if lock_path.is_symlink():
                raise ConsumerError("UNSAFE_STATE_FILE")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                                 0o600)
            self._lock = os.fdopen(descriptor, "r+b")
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ConsumerError("UNSAFE_STATE_FILE")
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    self._lock.write(b"\0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or (os.name != "nt" and info.st_mode & 0o077):
                    raise ConsumerError("UNSAFE_STATE_FILE")
                self.data = read_json(self.path)
                if (self.data.get("version") != 1 or self.data.get("base") != self.base
                        or not isinstance(self.data.get("inputs"), dict)):
                    raise ConsumerError("STATE_TARGET_OR_FORMAT_MISMATCH")
                self._identity = self._stat_identity(info)
            return self
        except OSError:
            self.__exit__(None, None, None)
            raise ConsumerError("STATE_BUSY_OR_UNWRITABLE") from None
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    @staticmethod
    def _stat_identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def save(self):
        if self._lock is None:
            raise ConsumerError("STATE_NOT_LOCKED")
        temporary = None
        try:
            raw = (json.dumps(self.data, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
            if len(raw) > MAX_STATE_BYTES:
                raise ConsumerError("STATE_TOO_LARGE")
            if self._identity is not None:
                if self._stat_identity(self.path.lstat()) != self._identity:
                    raise ConsumerError("STATE_CHANGED")
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".media-state-",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if self._identity is None:
                os.link(temporary, self.path)
            else:
                os.replace(temporary, self.path)
            self._identity = self._stat_identity(self.path.lstat())
            if os.name != "nt":
                descriptor = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError, TypeError):
            raise ConsumerError("STATE_WRITE_FAILED") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def require_existing(self):
        if not self.data:
            raise ConsumerError("STATE_NOT_FOUND")
