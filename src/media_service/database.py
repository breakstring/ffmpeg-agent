import asyncio
import fcntl
import os
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

import aiosqlite

T = TypeVar("T")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, caller TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, definition TEXT, public_definition TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','canceling','succeeded','partial','failed','canceled')),
 phase TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, deadline REAL NOT NULL, terminal_at REAL, idempotency_expires REAL,
 inputs_released INTEGER NOT NULL DEFAULT 0, current_attempt TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0, engine TEXT NOT NULL, error TEXT,
 output_storage TEXT NOT NULL, retention_seconds INTEGER NOT NULL,
 work_cleaned INTEGER NOT NULL DEFAULT 0,
 UNIQUE(caller, idempotency_key),
 CHECK((status IN ('succeeded','partial','failed','canceled')) = (inputs_released = 1))
);
CREATE INDEX jobs_queue ON jobs(status, created);
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 number INTEGER NOT NULL, status TEXT NOT NULL, lease_until REAL NOT NULL,
 path TEXT NOT NULL, started REAL NOT NULL, stopped REAL,
 UNIQUE(job_id, number)
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, caller TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
 step_id TEXT NOT NULL, attempt_id TEXT NOT NULL, storage_id TEXT NOT NULL,
 directory TEXT NOT NULL UNIQUE, staging TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('publishing','ready','retiring','deleting','deleted')),
 public INTEGER NOT NULL, kind TEXT NOT NULL, media TEXT NOT NULL,
 manifest TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
 created REAL NOT NULL, published REAL, retain_until REAL,
 result_status TEXT NOT NULL, deleted_at REAL
);
CREATE INDEX artifacts_expiry ON artifacts(state, retain_until);
CREATE TABLE steps (
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 id TEXT NOT NULL, ordinal INTEGER NOT NULL, operation TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', artifact_id TEXT REFERENCES artifacts(id), error TEXT,
 PRIMARY KEY(job_id, id)
);
CREATE TABLE refs (
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE, kind TEXT NOT NULL CHECK(kind IN ('job','send')),
 created REAL NOT NULL
);
CREATE INDEX refs_artifact ON refs(artifact_id);
CREATE TABLE leases (
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 caller TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, released INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX leases_artifact ON leases(artifact_id, expires);
CREATE TABLE downloads (
 job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
 input_name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
 PRIMARY KEY(job_id, input_name)
);
"""


async def wait_settled[T](task: asyncio.Task[T]) -> T:
    # 取消等待者不能让已排队的 SQLite 或文件操作逃离资源锁。
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class Transaction:
    def __init__(self, connection: aiosqlite.Connection):
        self.connection = connection

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        async with self.connection.execute(sql, parameters) as cursor:
            return cursor.rowcount

    async def one(self, sql: str, parameters: Sequence[Any] = ()) -> dict | None:
        async with self.connection.execute(sql, parameters) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row is not None else None

    async def all(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict]:
        async with self.connection.execute(sql, parameters) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


class Database:
    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection | None = None
        self.lock_fd: int | None = None
        self.healthy = True

    async def open(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.lock_fd = os.open(self.directory / "instance.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("state directory already has an application instance") from None
        try:
            self.connection = await aiosqlite.connect(
                self.directory / "state.sqlite", isolation_level=None
            )
            self.connection.row_factory = sqlite3.Row
            os.chmod(self.directory / "state.sqlite", 0o600)
            for pragma in (
                "journal_mode=DELETE",
                "synchronous=FULL",
                "foreign_keys=ON",
                "busy_timeout=2000",
                "secure_delete=ON",
            ):
                await self.connection.execute(f"PRAGMA {pragma}")
            async with self.connection.execute("PRAGMA user_version") as cursor:
                row = await cursor.fetchone()
                version = row[0] if row else -1
            if version == 0:
                await self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=1;\nCOMMIT;"
                )
            elif version != 1:
                raise RuntimeError("unsupported state schema; explicit migration required")
            async with self.connection.execute("PRAGMA quick_check") as cursor:
                row = await cursor.fetchone()
                if row is None or row[0] != "ok":
                    raise RuntimeError("state integrity check failed")
        except BaseException:
            await self.close()
            raise

    async def run(self, operation: Callable[[Transaction], Awaitable[T]]) -> T:
        if not self.healthy or self.connection is None:
            raise RuntimeError("state unavailable")
        aborted = asyncio.Event()

        async def serialized() -> T:
            async with self.lock:
                connection = self.connection
                assert connection is not None
                await connection.execute("BEGIN IMMEDIATE")
                try:
                    result = await operation(Transaction(connection))
                    if aborted.is_set():
                        raise asyncio.CancelledError
                    await connection.commit()
                    return result
                except BaseException:
                    try:
                        await connection.rollback()
                    except BaseException:
                        self.healthy = False
                        raise
                    raise

        task = asyncio.create_task(serialized())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            aborted.set()
            try:
                await wait_settled(task)
            except asyncio.CancelledError:
                pass
            raise

    async def one(self, sql: str, parameters: Sequence[Any] = ()) -> dict | None:
        return await self.run(lambda tx: tx.one(sql, parameters))

    async def all(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict]:
        return await self.run(lambda tx: tx.all(sql, parameters))

    async def close(self):
        async with self.lock:
            if self.connection is not None:
                await self.connection.close()
                self.connection = None
            if self.lock_fd is not None:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
                self.lock_fd = None
