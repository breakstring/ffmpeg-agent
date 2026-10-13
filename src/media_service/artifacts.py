import asyncio
import json
import logging
import time
from pathlib import Path

from media_service.database import Transaction, wait_settled
from media_service.errors import ServiceError
from media_service.files import file_job, remove_owned_dir
from media_service.state import State, timestamp, uid

logger = logging.getLogger(__name__)
_TERMINAL_SQL = "('succeeded','partial','failed','canceled')"


class Artifacts:
    def __init__(self, state: State):
        self.state = state
        self.settings = state.settings
        self._gc_lock = asyncio.Lock()

    async def _owned(self, tx: Transaction, caller: str, artifact_id: str) -> dict:
        artifact = await tx.one(
            "SELECT * FROM artifacts WHERE id=? AND caller=? AND public=1",
            (artifact_id, caller),
        )
        if artifact is None or artifact["state"] == "publishing":
            raise ServiceError("NOT_FOUND", 404)
        return artifact

    async def _readable(
        self, tx: Transaction, caller: str, artifact_id: str, lease_id: str | None
    ) -> dict:
        artifact = await self._owned(tx, caller, artifact_id)
        now = time.time()
        if artifact["state"] == "ready" and artifact["retain_until"] > now:
            return artifact
        if artifact["state"] not in {"ready", "retiring"}:
            raise ServiceError("ARTIFACT_GONE", 410)
        lease = await tx.one(
            "SELECT id FROM leases WHERE id=? AND artifact_id=? AND caller=? "
            "AND released=0 AND expires>?",
            (lease_id, artifact_id, caller, now),
        )
        if lease is None:
            raise ServiceError("ARTIFACT_GONE", 410)
        return artifact | {"state": "retiring"}

    async def _retainable(self, tx: Transaction, caller: str, artifact_id: str) -> dict:
        artifact = await self._owned(tx, caller, artifact_id)
        if artifact["state"] != "ready" or artifact["retain_until"] <= time.time():
            raise ServiceError("ARTIFACT_GONE", 410)
        return artifact

    @staticmethod
    def _file(artifact: dict, member_key: str | None) -> dict:
        manifest = json.loads(artifact["manifest"])
        if artifact["kind"] == "file":
            if member_key is not None:
                raise ServiceError("NOT_FOUND", 404)
            return manifest["files"][0]
        if member_key is None:
            raise ServiceError("UNSUPPORTED_OPERATION", 422)
        if not any(
            item.get("member_key") == member_key and not item.get("error")
            for item in manifest["items"]
        ):
            raise ServiceError("NOT_FOUND", 404)
        for entry in manifest["files"]:
            if entry["key"] == member_key:
                return entry
        raise ServiceError("NOT_FOUND", 404)

    def _metadata(self, caller: str, artifact: dict) -> dict:
        manifest = json.loads(artifact["manifest"])
        base = f"{self.settings.public_base_url.rstrip('/')}/v1/artifacts/{artifact['id']}"
        http = {"metadata": base}
        if artifact["kind"] == "file":
            http["content"] = f"{base}/content"
        else:
            http |= {"members": f"{base}/members", "files_base": f"{base}/files/"}
        access: dict = {"http": http}
        storage = self.settings.storages[artifact["storage_id"]]
        if caller in storage.callers:
            key = artifact["directory"]
            if artifact["kind"] == "file":
                key += f"/{manifest['files'][0]['key']}"
            access["shared"] = {"storage_id": artifact["storage_id"], "key": key}
        return {
            "artifact_id": artifact["id"],
            "job_id": artifact["job_id"],
            "kind": artifact["kind"],
            "state": artifact["state"],
            "media": json.loads(artifact["media"]),
            "size_bytes": artifact["size_bytes"],
            "sha256": artifact["sha256"],
            "retain_until": timestamp(artifact["retain_until"]),
            "published_at": timestamp(artifact["published"]),
            "member_count": len(manifest["files"]),
            "item_count": len(manifest["items"]),
            "access": access,
        }

    async def metadata(
        self, caller: str, artifact_id: str, lease_id: str | None = None
    ) -> dict:
        artifact = await self.state.db.run(
            lambda tx: self._readable(tx, caller, artifact_id, lease_id)
        )
        return self._metadata(caller, artifact)

    async def members(
        self,
        caller: str,
        artifact_id: str,
        lease_id: str | None = None,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceError("INVALID_REQUEST", 422)
        artifact = await self.state.db.run(
            lambda tx: self._readable(tx, caller, artifact_id, lease_id)
        )
        if artifact["kind"] != "collection":
            raise ServiceError("UNSUPPORTED_OPERATION", 422)
        items = json.loads(artifact["manifest"])["items"]
        end = cursor + limit
        return {"items": items[cursor:end], "next_cursor": end if end < len(items) else None}

    def _lease_duration(self, duration: int) -> int:
        if type(duration) is not int or not 1 <= duration <= 3600:
            raise ServiceError("INVALID_REQUEST", 422)
        return min(duration, self.settings.max_lease_seconds)

    async def lease_create(self, caller: str, artifact_id: str, duration: int) -> dict:
        duration = self._lease_duration(duration)

        async def create(tx: Transaction):
            await self._retainable(tx, caller, artifact_id)
            now = time.time()
            lease_id = uid("lease")
            expires = now + min(duration, self.settings.max_lease_total_seconds)
            await tx.execute(
                "INSERT INTO leases(id,artifact_id,caller,created,expires) VALUES(?,?,?,?,?)",
                (lease_id, artifact_id, caller, now, expires),
            )
            return {"lease_id": lease_id, "artifact_id": artifact_id,
                    "expires_at": timestamp(expires)}

        return await self.state.db.run(create)

    async def lease_renew(self, caller: str, lease_id: str, duration: int) -> dict:
        duration = self._lease_duration(duration)

        async def renew(tx: Transaction):
            lease = await tx.one("SELECT * FROM leases WHERE id=? AND caller=?", (lease_id, caller))
            if lease is None:
                raise ServiceError("NOT_FOUND", 404)
            now = time.time()
            if lease["released"] or lease["expires"] <= now:
                raise ServiceError("ARTIFACT_GONE", 410)
            await self._retainable(tx, caller, lease["artifact_id"])
            expires = max(lease["expires"], min(
                now + duration, lease["created"] + self.settings.max_lease_total_seconds
            ))
            await tx.execute("UPDATE leases SET expires=? WHERE id=?", (expires, lease_id))
            return {"lease_id": lease_id, "artifact_id": lease["artifact_id"],
                    "expires_at": timestamp(expires)}

        return await self.state.db.run(renew)

    async def lease_release(self, caller: str, lease_id: str) -> None:
        await self.state.db.run(lambda tx: tx.execute(
            "UPDATE leases SET released=1 WHERE id=? AND caller=?", (lease_id, caller)
        ))

    async def extend(self, caller: str, artifact_id: str, retention_seconds: int) -> dict:
        if (
            type(retention_seconds) is not int
            or not 1 <= retention_seconds <= self.settings.max_retention_seconds
        ):
            raise ServiceError("INVALID_REQUEST", 422)

        async def extend_retention(tx: Transaction):
            artifact = await self._retainable(tx, caller, artifact_id)
            retain_until = max(
                artifact["retain_until"], artifact["published"] + retention_seconds
            )
            await tx.execute(
                "UPDATE artifacts SET retain_until=? WHERE id=?", (retain_until, artifact_id)
            )
            return {"artifact_id": artifact_id, "retain_until": timestamp(retain_until)}

        return await self.state.db.run(extend_retention)

    async def release(self, caller: str, artifact_id: str) -> dict:
        async def retire(tx: Transaction):
            artifact = await self._owned(tx, caller, artifact_id)
            if artifact["state"] == "ready":
                await tx.execute("UPDATE artifacts SET state='retiring' WHERE id=?", (artifact_id,))
                artifact["state"] = "retiring"
            return {"artifact_id": artifact_id, "state": artifact["state"],
                    "retain_until": timestamp(artifact["retain_until"])}

        return await self.state.db.run(retire)

    async def acquire_send(
        self,
        caller: str,
        artifact_id: str,
        member_key: str | None = None,
        lease_id: str | None = None,
    ) -> tuple[dict, str, dict]:
        ref_id = uid("ref")

        async def acquire(tx: Transaction):
            artifact = await self._readable(tx, caller, artifact_id, lease_id)
            metadata = self._file(artifact, member_key)
            await tx.execute(
                "INSERT INTO refs(id,artifact_id,kind,created) VALUES(?,?,'send',?)",
                (ref_id, artifact_id, time.time()),
            )
            return artifact, ref_id, metadata

        try:
            return await self.state.db.run(acquire)
        except asyncio.CancelledError:
            # 取消可能与 COMMIT 重叠；未交付给响应层的引用必须在返回前收回。
            await wait_settled(asyncio.create_task(self.release_send(ref_id)))
            raise

    async def release_send(self, ref_id: str) -> None:
        task = asyncio.create_task(self.state.db.run(lambda tx: tx.execute(
            "DELETE FROM refs WHERE id=? AND kind='send'", (ref_id,)
        )))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await wait_settled(task)
            raise

    async def resolve_input(
        self, job_id: str, artifact_id: str, member_key: str | None = None
    ) -> Path:
        async def resolve(tx: Transaction):
            artifact = await tx.one(
                "SELECT a.* FROM artifacts a WHERE a.id=? AND EXISTS "
                "(SELECT 1 FROM refs r WHERE r.artifact_id=a.id AND r.job_id=? AND r.kind='job')",
                (artifact_id, job_id),
            )
            if artifact is None:
                raise ServiceError("NOT_FOUND", 404)
            if artifact["state"] not in {"ready", "retiring"}:
                raise ServiceError("ARTIFACT_GONE", 410)
            return artifact, self._file(artifact, member_key)

        artifact, metadata = await self.state.db.run(resolve)
        return self.settings.storages[artifact["storage_id"]].root / artifact["directory"] / metadata["key"]

    async def _gc_candidates(self, tx: Transaction) -> tuple[list[dict], list[dict]]:
        now = time.time()
        await tx.execute(
            "UPDATE artifacts SET state='retiring' WHERE state='ready' AND retain_until<=?", (now,)
        )
        await tx.execute("DELETE FROM leases WHERE released=1 OR expires<=?", (now,))
        await tx.execute(
            "UPDATE artifacts SET state='deleting' WHERE "
            "(state='retiring' OR (state='publishing' AND job_id IN "
            f"(SELECT id FROM jobs WHERE status IN {_TERMINAL_SQL} AND inputs_released=1))) "
            "AND NOT EXISTS (SELECT 1 FROM refs WHERE artifact_id=artifacts.id) "
            "AND NOT EXISTS (SELECT 1 FROM leases WHERE artifact_id=artifacts.id)"
        )
        artifacts = await tx.all(
            "SELECT a.* FROM artifacts a JOIN jobs j ON j.id=a.job_id WHERE a.state='deleting' "
            f"AND (a.published IS NOT NULL OR (j.status IN {_TERMINAL_SQL} AND j.inputs_released=1)) "
            "AND NOT EXISTS (SELECT 1 FROM refs WHERE artifact_id=a.id) "
            "AND NOT EXISTS (SELECT 1 FROM leases WHERE artifact_id=a.id)"
        )
        jobs = await tx.all(
            f"SELECT * FROM jobs WHERE status IN {_TERMINAL_SQL} "
            "AND inputs_released=1 AND work_cleaned=0 "
            "AND NOT EXISTS (SELECT 1 FROM attempts WHERE job_id=jobs.id AND status='running')"
        )
        return artifacts, jobs

    async def _prune_jobs(self, tx: Transaction) -> int:
        jobs = await tx.all(
            f"SELECT id FROM jobs j WHERE status IN {_TERMINAL_SQL} "
            "AND inputs_released=1 AND work_cleaned=1 AND idempotency_expires<=? "
            "AND NOT EXISTS (SELECT 1 FROM artifacts WHERE job_id=j.id AND state!='deleted') "
            "AND NOT EXISTS (SELECT 1 FROM refs WHERE job_id=j.id OR artifact_id IN "
            "(SELECT id FROM artifacts WHERE job_id=j.id)) "
            "AND NOT EXISTS (SELECT 1 FROM leases WHERE artifact_id IN "
            "(SELECT id FROM artifacts WHERE job_id=j.id)) "
            "AND NOT EXISTS (SELECT 1 FROM attempts WHERE job_id=j.id AND status='running')",
            (time.time(),),
        )
        for job in jobs:
            parameters = (job["id"],)
            await tx.execute("DELETE FROM steps WHERE job_id=?", parameters)
            await tx.execute("DELETE FROM artifacts WHERE job_id=?", parameters)
            await tx.execute("DELETE FROM attempts WHERE job_id=?", parameters)
            await tx.execute("DELETE FROM downloads WHERE job_id=?", parameters)
            await tx.execute("DELETE FROM jobs WHERE id=?", parameters)
        return len(jobs)

    async def gc(self) -> dict:
        async with self._gc_lock:
            artifacts, jobs = await self.state.db.run(self._gc_candidates)
            deleted = cleaned = 0
            for artifact in artifacts:
                root = self.settings.storages[artifact["storage_id"]].root
                try:
                    for key in (artifact["directory"], artifact["staging"]):
                        await file_job(lambda event, root=root, key=key: remove_owned_dir(root, key, event))
                except (OSError, ServiceError):
                    logger.warning("产物目录回收失败，保留登记以重试", extra={"artifact_id": artifact["id"]})
                    continue
                deleted += await self.state.db.run(lambda tx, artifact=artifact: tx.execute(
                    "UPDATE artifacts SET state='deleted',deleted_at=? WHERE id=? AND state='deleting'",
                    (time.time(), artifact["id"]),
                ))
            for job in jobs:
                try:
                    await file_job(lambda event, job=job: remove_owned_dir(
                        self.settings.work_dir, job["id"], event
                    ))
                except (OSError, ServiceError):
                    logger.warning("任务工作目录回收失败，保留登记以重试", extra={"job_id": job["id"]})
                    continue
                cleaned += await self.state.db.run(lambda tx, job=job: tx.execute(
                    "UPDATE jobs SET work_cleaned=1 WHERE id=? AND inputs_released=1 AND work_cleaned=0",
                    (job["id"],),
                ))
            pruned = await self.state.db.run(self._prune_jobs)
            return {"artifacts_deleted": deleted, "work_cleaned": cleaned, "jobs_pruned": pruned}
