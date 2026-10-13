import asyncio
import sqlite3
import threading

import pytest

from media_service.database import Database


async def test_cancel_during_actual_sql_waits_for_rollback_before_next_transaction(tmp_path):
    db = Database(tmp_path / "state")
    await db.open()
    executing = threading.Event()
    release = threading.Event()
    blocked = None
    reader = None

    def sql_gate():
        executing.set()
        if not release.wait(timeout=5):
            raise RuntimeError("test SQL gate timed out")
        return 1

    async def operation(tx):
        await tx.execute("INSERT INTO meta VALUES (?, ?)", ("uncommitted", "never-visible"))
        await tx.one("SELECT sql_gate() AS value")

    try:
        assert db.connection is not None
        # 闸门运行于真实 SQLite 工作线程，精确覆盖等待者取消时 SQL 尚未结束的窗口。
        await db.connection.create_function("sql_gate", 0, sql_gate)
        blocked = asyncio.create_task(db.run(operation))
        assert await asyncio.to_thread(executing.wait, 2)
        blocked.cancel()
        await asyncio.sleep(0)
        blocked.cancel()
        reader = asyncio.create_task(db.all("SELECT * FROM meta"))
        await asyncio.sleep(0)
        assert db.lock.locked()
        assert not blocked.done()
        assert not reader.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(blocked, 2)
        assert await asyncio.wait_for(reader, 2) == []
        assert not db.connection.in_transaction
        await db.run(lambda tx: tx.execute("INSERT INTO meta VALUES (?, ?)", ("later", "committed")))
        assert await db.one("SELECT value FROM meta WHERE key=?", ("later",)) == {"value": "committed"}
    finally:
        release.set()
        for task in (blocked, reader):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await db.close()


async def test_cancelled_queued_transaction_cannot_commit_between_other_writers(tmp_path):
    db = Database(tmp_path / "state")
    await db.open()
    tasks = []
    gate_held = False
    try:
        await db.lock.acquire()
        gate_held = True
        for index in range(32):
            async def write(tx, key=f"key-{index}"):
                await tx.execute("INSERT INTO meta VALUES (?, ?)", (key, "committed"))

            tasks.append(asyncio.create_task(db.run(write)))
        await asyncio.sleep(0)
        canceled = set(range(0, 32, 3))
        for index in canceled:
            tasks[index].cancel()
        await asyncio.sleep(0)
        db.lock.release()
        gate_held = False
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        for index, outcome in enumerate(outcomes):
            if index in canceled:
                assert isinstance(outcome, asyncio.CancelledError)
            else:
                assert outcome is None
        rows = await db.all("SELECT key FROM meta")
        assert {row["key"] for row in rows} == {
            f"key-{index}" for index in range(32) if index not in canceled
        }
        assert db.connection is not None and not db.connection.in_transaction
    finally:
        if gate_held:
            db.lock.release()
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.close()


async def test_statement_failure_rolls_back_prior_writes_and_releases_connection(tmp_path):
    db = Database(tmp_path / "state")
    await db.open()

    async def conflicting(tx):
        await tx.execute("INSERT INTO meta VALUES (?, ?)", ("same", "first"))
        await tx.execute("INSERT INTO meta VALUES (?, ?)", ("same", "second"))

    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db.run(conflicting)
        assert await db.all("SELECT * FROM meta") == []
        await db.run(lambda tx: tx.execute("INSERT INTO meta VALUES (?, ?)", ("same", "next")))
        assert await db.one("SELECT value FROM meta WHERE key=?", ("same",)) == {"value": "next"}
        assert db.healthy
    finally:
        await db.close()


async def test_busy_database_fails_within_budget_and_recovers_without_partial_write(tmp_path):
    directory = tmp_path / "state"
    db = Database(directory)
    await db.open()
    blocker = sqlite3.connect(directory / "state.sqlite", isolation_level=None)
    try:
        await db.run(lambda tx: tx.execute("PRAGMA busy_timeout=50"))
        # 第二连接仅作为故障注入器，持有真实数据库写锁。
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await asyncio.wait_for(db.run(lambda tx: tx.execute(
                "INSERT INTO meta VALUES (?, ?)", ("busy", "must-not-commit")
            )), 2)
        blocker.rollback()
        assert await db.all("SELECT * FROM meta") == []
        await db.run(lambda tx: tx.execute("INSERT INTO meta VALUES (?, ?)", ("healthy", "yes")))
        assert await db.one("SELECT value FROM meta WHERE key=?", ("healthy",)) == {"value": "yes"}
        assert db.healthy
    finally:
        blocker.close()
        await db.close()
