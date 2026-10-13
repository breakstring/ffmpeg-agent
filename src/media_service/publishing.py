import hashlib
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from media_service.database import Transaction
from media_service.errors import ServiceError
from media_service.files import (
    check_cancel,
    copy_verified,
    file_job,
    fsync_directory,
    safe_open,
    sha256_fd,
)
from media_service.products import Product
from media_service.state import State, encode, uid


class Publisher:
    def __init__(self, state: State):
        self.state = state
        self.settings = state.settings
        self.checkpoint_hook: Callable[[str, str], Awaitable[None]] | None = None

    async def checkpoint(self, name: str, artifact_id: str):
        if self.checkpoint_hook:
            await self.checkpoint_hook(name, artifact_id)

    async def publish(
        self, job: dict, step_id: str, product: Product, source_dir: Path, public: bool
    ) -> str:
        artifact_id = uid("art")
        storage = self.settings.storages[job["output_storage"]]
        staging_key = f".staging_{artifact_id}"
        final_key = artifact_id
        files = []
        for name in product.files:
            if "/" in name or name in {"", ".", ".."}:
                raise ServiceError("INVALID_MEDIA")
            fd = safe_open(source_dir, name, self.settings.max_output_bytes)
            try:
                size, digest = await file_job(lambda event, fd=fd: sha256_fd(fd, event))
            finally:
                os.close(fd)
            files.append({"key": name, "size_bytes": size, "sha256": digest})
        if not files or len({f["key"] for f in files}) != len(files):
            raise ServiceError("INVALID_MEDIA")
        total = sum(f["size_bytes"] for f in files)
        if total > self.settings.max_output_bytes:
            raise ServiceError("OUTPUT_LIMIT")
        items = []
        by_name = {f["key"]: f for f in files}
        for item in product.items:
            entry = dict(item)
            if entry.get("member_key") is not None:
                meta = by_name.get(entry["member_key"])
                if meta is None:
                    raise ServiceError("INVALID_MEDIA")
                entry.update({key: meta[key] for key in ("size_bytes", "sha256")})
            items.append(entry)
        manifest = {"files": files, "items": items}
        manifest_text = encode(manifest)
        digest = (
            files[0]["sha256"]
            if product.kind == "file"
            else hashlib.sha256(manifest_text.encode()).hexdigest()
        )

        async def intent(tx: Transaction):
            await self.state.eligible(tx, job["id"], job["current_attempt"])
            step = await tx.one(
                "SELECT status FROM steps WHERE job_id=? AND id=?", (job["id"], step_id)
            )
            if step is None or step["status"] != "running":
                raise ServiceError("STATE_CONFLICT", 409)
            usage = await tx.one(
                "SELECT COALESCE(sum(size_bytes),0) n FROM artifacts WHERE storage_id=? AND state!='deleted'",
                (job["output_storage"],),
            )
            job_usage = await tx.one(
                "SELECT COALESCE(sum(size_bytes),0) n FROM artifacts WHERE job_id=? AND state!='deleted'",
                (job["id"],),
            )
            assert usage is not None and job_usage is not None
            if usage["n"] + total > storage.quota_bytes:
                raise ServiceError("DISK_FULL", retryable=True)
            if job_usage["n"] + total > self.settings.max_output_bytes:
                raise ServiceError("OUTPUT_LIMIT")
            await tx.execute(
                "INSERT INTO artifacts(id,caller,job_id,step_id,attempt_id,storage_id,directory,staging,state,public,kind,media,manifest,size_bytes,sha256,created,result_status) VALUES(?,?,?,?,?,?,?,?,'publishing',?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    job["caller"],
                    job["id"],
                    step_id,
                    job["current_attempt"],
                    job["output_storage"],
                    final_key,
                    staging_key,
                    int(public),
                    product.kind,
                    encode(product.media),
                    manifest_text,
                    total,
                    digest,
                    time.time(),
                    product.status,
                ),
            )

        await self.state.db.run(intent)
        await self.checkpoint("intent", artifact_id)

        def stage(event: threading.Event):
            target = storage.root / staging_key
            target.mkdir(mode=0o700)
            for meta in files:
                copy_verified(
                    source_dir / meta["key"],
                    target / meta["key"],
                    event,
                    max_bytes=self.settings.max_output_bytes,
                    expected_size=meta["size_bytes"],
                    expected_sha256=meta["sha256"],
                )
                os.chmod(target / meta["key"], 0o444)
            check_cancel(event)
            manifest_path = target / "manifest.json"
            with manifest_path.open("xb") as handle:
                handle.write(manifest_text.encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(manifest_path, 0o444)
            os.chmod(target, 0o755)
            fsync_directory(target, event)
            fsync_directory(storage.root, event)

        await file_job(stage)
        await self.checkpoint("staged", artifact_id)
        await self.state.db.run(
            lambda tx: self.state.eligible(tx, job["id"], job["current_attempt"])
        )

        def rename(event: threading.Event):
            check_cancel(event)
            target = storage.root / final_key
            if target.exists():
                raise ServiceError("STATE_CONFLICT", 409)
            os.rename(storage.root / staging_key, target)
            fsync_directory(storage.root, event)

        await file_job(rename)
        await self.checkpoint("renamed", artifact_id)
        await self.state.db.run(
            lambda tx: self.commit(tx, artifact_id, job["id"], job["current_attempt"])
        )
        await self.checkpoint("committed", artifact_id)
        return artifact_id

    async def commit(
        self,
        tx: Transaction,
        artifact_id: str,
        job_id: str,
        attempt_id: str,
        *,
        recovery: bool = False,
    ):
        if recovery:
            job = await tx.one("SELECT * FROM jobs WHERE id=?", (job_id,))
            assert job is not None
            if job["cancel_requested"] or job["deadline"] <= time.time():
                raise ServiceError("CANCELED")
        else:
            job = await self.state.eligible(tx, job_id, attempt_id)
        artifact = await tx.one(
            "SELECT * FROM artifacts WHERE id=? AND state='publishing'", (artifact_id,)
        )
        if artifact is None or artifact["attempt_id"] != attempt_id:
            raise ServiceError("STATE_CONFLICT", 409)
        now = time.time()
        await tx.execute(
            "UPDATE artifacts SET state='ready',published=?,retain_until=? WHERE id=? AND state='publishing'",
            (now, now + (job["retention_seconds"] if artifact["public"] else 0), artifact_id),
        )
        changed = await tx.execute(
            "UPDATE steps SET status=?,artifact_id=? WHERE job_id=? AND id=? AND artifact_id IS NULL AND status IN ('pending','running')",
            (artifact["result_status"], artifact_id, job_id, artifact["step_id"]),
        )
        if changed != 1:
            raise ServiceError("STATE_CONFLICT", 409)
        await tx.execute(
            "INSERT INTO refs(id,artifact_id,job_id,kind,created) VALUES(?,?,?,'job',?)",
            (uid("ref"), artifact_id, job_id, now),
        )

    def verify_directory(self, artifact: dict, key: str, event: threading.Event) -> bool:
        root = self.settings.storages[artifact["storage_id"]].root
        try:
            fd = safe_open(root, f"{key}/manifest.json", self.settings.max_output_bytes)
            try:
                with os.fdopen(fd, "rb") as stream:
                    if stream.read() != artifact["manifest"].encode():
                        return False
            except BaseException:
                raise
            for entry in json.loads(artifact["manifest"])["files"]:
                fd = safe_open(root, f"{key}/{entry['key']}", self.settings.max_output_bytes)
                try:
                    size, digest = sha256_fd(fd, event)
                finally:
                    os.close(fd)
                if size != entry["size_bytes"] or digest != entry["sha256"]:
                    return False
            return True
        except OSError, ServiceError:
            return False

    async def reconcile(self, engine: dict):
        artifacts = await self.state.db.all("SELECT * FROM artifacts WHERE state='publishing'")
        for artifact in artifacts:
            job = await self.state.db.one("SELECT * FROM jobs WHERE id=?", (artifact["job_id"],))
            can_commit = (
                job is not None
                and job["status"] in {"queued", "running", "canceling"}
                and not job["cancel_requested"]
                and job["deadline"] > time.time()
                and json.loads(job["engine"]) == engine
            )
            valid = can_commit and await file_job(
                lambda event, artifact=artifact: self.verify_directory(
                    artifact, artifact["directory"], event
                )
            )
            if not valid and can_commit:
                staged = await file_job(
                    lambda event, artifact=artifact: self.verify_directory(
                        artifact, artifact["staging"], event
                    )
                )
                if staged:
                    root = self.settings.storages[artifact["storage_id"]].root

                    def rename(event: threading.Event, root=root, artifact=artifact):
                        check_cancel(event)
                        if (root / artifact["directory"]).exists():
                            return False
                        os.rename(root / artifact["staging"], root / artifact["directory"])
                        fsync_directory(root, event)
                        return True

                    valid = await file_job(rename)
            if valid:
                try:
                    await self.state.db.run(
                        lambda tx, artifact=artifact: self.commit(
                            tx,
                            artifact["id"],
                            artifact["job_id"],
                            artifact["attempt_id"],
                            recovery=True,
                        )
                    )
                except ServiceError as error:
                    if error.code != "CANCELED":
                        raise
                    # 完整性检查消耗时间；提交时到期只撤销未发布结果，不阻断启动。
                    valid = False
            if not valid:
                await self.state.db.run(
                    lambda tx, artifact=artifact: tx.execute(
                        "UPDATE artifacts SET state='deleting' WHERE id=? AND state='publishing'",
                        (artifact["id"],),
                    )
                )
        for artifact in await self.state.db.all(
            "SELECT * FROM artifacts WHERE state IN ('ready','retiring')"
        ):
            if not await file_job(
                lambda event, artifact=artifact: self.verify_directory(
                    artifact, artifact["directory"], event
                )
            ):
                raise RuntimeError("published artifact integrity check failed")
