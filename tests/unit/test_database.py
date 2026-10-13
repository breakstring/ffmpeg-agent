import asyncio

import pytest

from media_service.database import Database


@pytest.mark.asyncio
async def test_single_instance_and_transaction_cancellation(tmp_path):
    db = Database(tmp_path / "state")
    await db.open()
    second = Database(tmp_path / "state")
    with pytest.raises(RuntimeError, match="already has"):
        await second.open()
    started = asyncio.Event()
    release = asyncio.Event()

    async def transaction(tx):
        await tx.execute("INSERT INTO meta VALUES (?, ?)", ("a", "uncommitted"))
        started.set()
        # 测试闸门仅用于精确注入事务取消；生产事务禁止等待外部I/O。
        await release.wait()
        await tx.execute("INSERT INTO meta VALUES (?, ?)", ("b", "uncommitted"))

    task = asyncio.create_task(db.run(transaction))
    await started.wait()
    task.cancel()
    reader = asyncio.create_task(db.all("SELECT * FROM meta"))
    await asyncio.sleep(0)
    assert not reader.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await reader == []
    await db.run(lambda tx: tx.execute("INSERT INTO meta VALUES (?, ?)", ("c", "committed")))
    assert await db.one("SELECT value FROM meta WHERE key=?", ("c",)) == {"value": "committed"}
    await db.close()
    await second.open()
    assert await second.one("SELECT value FROM meta WHERE key=?", ("c",)) == {"value": "committed"}
    await second.close()
