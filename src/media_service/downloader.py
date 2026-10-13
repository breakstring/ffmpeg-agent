import asyncio
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import threading
import time
import weakref
from pathlib import Path

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from yarl import URL

from media_service.config import Settings, SourceRule
from media_service.database import Transaction, wait_settled
from media_service.errors import ServiceError
from media_service.files import check_cancel, directory_size, file_job, safe_open, sha256_fd
from media_service.models import HttpFile
from media_service.state import State, uid

_REDIRECTS = {301, 302, 303, 307, 308}
_BLOCK_SIZE = 64 * 1024
_WRITE_SPACE_LOCK = threading.Lock()


def _host(value: str) -> str:
    if "%" in value:
        raise ServiceError("SOURCE_FORBIDDEN")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            return value.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ServiceError("SOURCE_FORBIDDEN") from None


class _AddressPolicy:
    def __init__(self, rules: list[SourceRule]):
        self.rules = rules

    def validate(self, raw_url: str) -> tuple[URL, SourceRule]:
        if (
            not raw_url
            or "#" in raw_url
            or "\\" in raw_url
            or any(ord(character) <= 32 or ord(character) == 127 for character in raw_url)
        ):
            raise ServiceError("SOURCE_FORBIDDEN")
        try:
            url = URL(raw_url, encoded=True)
            if url.scheme not in {"http", "https"} or url.raw_user is not None or not url.raw_host:
                raise ServiceError("SOURCE_FORBIDDEN")
            host = _host(url.raw_host)
            url = url.with_host(host)
            port = url.port
        except (ValueError, UnicodeError):
            raise ServiceError("SOURCE_FORBIDDEN") from None
        rule = next((rule for rule in self.rules if (
            _host(rule.host) == host and url.scheme in rule.schemes and port in rule.ports
        )), None)
        if rule is None:
            raise ServiceError("SOURCE_FORBIDDEN")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            self.check_ip(str(address), rule)
        return url, rule

    @staticmethod
    def check_ip(value: str, rule: SourceRule) -> None:
        try:
            if "%" in value:
                raise ValueError
            address = ipaddress.ip_address(value)
            networks = [ipaddress.ip_network(network, strict=False) for network in rule.networks]
            allowed = any(address in network for network in networks) if networks else address.is_global
            if address.is_multicast or address.is_unspecified or not allowed:
                raise ValueError
        except ValueError:
            raise ServiceError("SOURCE_FORBIDDEN") from None


class _ValidatedResolver(AbstractResolver):
    def __init__(self, rule: SourceRule):
        self.rule = rule
        self.resolver = DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        results = await self.resolver.resolve(host, port, family=family)
        if not results:
            raise ServiceError("SOURCE_UNREACHABLE", retryable=True)
        for result in results:
            _AddressPolicy.check_ip(result["host"], self.rule)
            if result["port"] != port:
                raise ServiceError("SOURCE_FORBIDDEN")
        return results

    async def close(self) -> None:
        await self.resolver.close()

    def socket(self, address_info) -> socket.socket:
        family, kind, protocol, _, address = address_info
        _AddressPolicy.check_ip(address[0], self.rule)
        return socket.socket(family=family, type=kind, proto=protocol)


class _Target:
    def __init__(self, settings: Settings, job_id: str, name: str):
        self.settings = settings
        self.job_id = job_id
        self.key = f"{job_id}/inputs/{name}.media"
        self.name = f"{name}.media"
        self.partial = f"{name}.{uid('download')}.part"
        self.directory_fd = -1
        self.fd = -1
        self.size = 0
        self.digest = hashlib.sha256()

    def open(self, event: threading.Event) -> None:
        check_cancel(event)
        current = os.open(self.settings.work_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in (self.job_id, "inputs"):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                os.close(current)
                current = child
            self.directory_fd, current = current, -1
            # 没有 downloads 检查点的同名文件仅是本输入上次中断留下的未提交结果。
            try:
                os.unlink(self.name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
            self.fd = os.open(
                self.partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=self.directory_fd,
            )
            check_cancel(event)
        finally:
            if current >= 0:
                os.close(current)

    def write(self, data: bytes, event: threading.Event) -> None:
        check_cancel(event)
        if self.size + len(data) > self.settings.max_input_bytes:
            raise ServiceError("INPUT_TOO_LARGE")
        while not _WRITE_SPACE_LOCK.acquire(timeout=0.05):
            check_cancel(event)
        try:
            # HTTP 写入串行检查整个工作卷，避免多个任务各自重复占用同一余量。
            if directory_size(self.settings.work_dir, event) + len(data) > self.settings.max_work_bytes:
                raise ServiceError("OUTPUT_LIMIT")
            if shutil.disk_usage(self.settings.work_dir).free - len(data) < self.settings.disk_reserve_bytes:
                raise ServiceError("DISK_FULL", retryable=True)
            view = memoryview(data)
            while view:
                check_cancel(event)
                count = os.write(self.fd, view)
                view = view[count:]
            self.digest.update(data)
            self.size += len(data)
        finally:
            _WRITE_SPACE_LOCK.release()

    def finalize(self, event: threading.Event) -> None:
        check_cancel(event)
        os.fchmod(self.fd, 0o400)
        os.fsync(self.fd)
        os.close(self.fd)
        self.fd = -1
        check_cancel(event)
        os.rename(self.partial, self.name, src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)
        check_cancel(event)

    def close(self, event: threading.Event) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.directory_fd >= 0:
            try:
                try:
                    os.unlink(self.partial, dir_fd=self.directory_fd)
                except FileNotFoundError:
                    pass
            finally:
                os.close(self.directory_fd)
                self.directory_fd = -1


class Downloader:
    def __init__(self, state: State):
        self.state = state
        self.settings = state.settings
        self._locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    @staticmethod
    def _check(job: dict, cancel: asyncio.Event) -> None:
        if cancel.is_set():
            raise ServiceError("CANCELED")
        if job["deadline"] <= time.time():
            raise ServiceError("TASK_TIMEOUT")

    async def acquire(
        self, job: dict, name: str, source: HttpFile, cancel: asyncio.Event
    ) -> Path:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise ServiceError("INVALID_REQUEST")
        self._check(job, cancel)
        lock = self._locks.setdefault((job["id"], name), asyncio.Lock())
        try:
            async with asyncio.timeout(job["deadline"] - time.time()), lock:
                return await self._acquire(job, name, source, cancel)
        except TimeoutError:
            raise ServiceError("TASK_TIMEOUT") from None
        except OSError as error:
            raise ServiceError(
                "DISK_FULL" if error.errno == 28 else "SOURCE_UNREACHABLE", retryable=True
            ) from None

    async def _acquire(
        self, job: dict, name: str, source: HttpFile, cancel: asyncio.Event
    ) -> Path:
        async def previous(tx: Transaction):
            await self.state.eligible(tx, job["id"], job["current_attempt"])
            return await tx.one(
                "SELECT * FROM downloads WHERE job_id=? AND input_name=?", (job["id"], name)
            )

        checkpoint = await self.state.db.run(previous)
        key = f"{job['id']}/inputs/{name}.media"
        if checkpoint is not None:
            if checkpoint["path"] != key:
                raise ServiceError("INPUT_CHANGED")

            def verify(event: threading.Event):
                fd = safe_open(self.settings.work_dir, key, self.settings.max_input_bytes)
                try:
                    return sha256_fd(fd, event)
                finally:
                    os.close(fd)

            try:
                size, digest = await file_job(verify)
            except (OSError, ServiceError):
                raise ServiceError("INPUT_CHANGED") from None
            if size != checkpoint["size_bytes"] or digest != checkpoint["sha256"]:
                raise ServiceError("INPUT_CHANGED")
            self._verify_expected(source, size, digest)
            self._check(job, cancel)
            await self.state.db.run(lambda tx: self.state.eligible(
                tx, job["id"], job["current_attempt"]
            ))
            return self.settings.work_dir / key

        for attempt in range(self.settings.http_attempts):
            self._check(job, cancel)
            target = _Target(self.settings, job["id"], name)
            try:
                try:
                    async with asyncio.timeout(self.settings.http_download_seconds):
                        await file_job(target.open)
                        await self._fetch(job, source, cancel, target)
                        self._verify_expected(source, target.size, target.digest.hexdigest())
                        await file_job(target.finalize)
                except TimeoutError:
                    raise ServiceError("SOURCE_UNREACHABLE", retryable=True) from None

                self._check(job, cancel)

                async def checkpoint_input(tx: Transaction, target=target):
                    await self.state.eligible(tx, job["id"], job["current_attempt"])
                    await tx.execute(
                        "INSERT INTO downloads(job_id,input_name,path,size_bytes,sha256) VALUES(?,?,?,?,?)",
                        (job["id"], name, target.key, target.size, target.digest.hexdigest()),
                    )

                await self.state.db.run(checkpoint_input)
                return self.settings.work_dir / target.key
            except ServiceError as error:
                if (
                    error.code != "SOURCE_UNREACHABLE"
                    or not error.retryable
                    or attempt + 1 == self.settings.http_attempts
                ):
                    raise
            finally:
                cleanup = asyncio.create_task(file_job(target.close))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await wait_settled(cleanup)
                    raise
            self._check(job, cancel)
            await asyncio.sleep(min(0.1 * 2**attempt, 1.0))
        raise ServiceError("SOURCE_UNREACHABLE", retryable=True)

    @staticmethod
    def _verify_expected(source: HttpFile, size: int, digest: str) -> None:
        if (
            source.expected_size_bytes is not None and source.expected_size_bytes != size
        ) or (source.expected_sha256 is not None and source.expected_sha256 != digest):
            raise ServiceError("INPUT_CHANGED")

    async def _fetch(
        self, job: dict, source: HttpFile, cancel: asyncio.Event, target: _Target
    ) -> None:
        policy = _AddressPolicy(self.settings.callers[job["caller"]].source_rules)
        url, rule = policy.validate(source.url)
        headers = dict(source.headers) | {"Accept-Encoding": "identity"}
        for redirect in range(self.settings.max_redirects + 1):
            self._check(job, cancel)
            resolver = _ValidatedResolver(rule)
            connector = aiohttp.TCPConnector(
                resolver=resolver, use_dns_cache=False, force_close=True,
                socket_factory=resolver.socket, limit=1,
            )
            timeout = aiohttp.ClientTimeout(
                total=None, connect=self.settings.http_connect_seconds,
                sock_connect=self.settings.http_connect_seconds,
                sock_read=self.settings.http_idle_seconds,
            )
            try:
                async with aiohttp.ClientSession(
                    connector=connector, trust_env=False, auto_decompress=False,
                    cookie_jar=aiohttp.DummyCookieJar(), timeout=timeout,
                ) as session, session.get(url, headers=headers, allow_redirects=False) as response:
                    if response.status in _REDIRECTS:
                        location = response.headers.get("Location")
                        if not location or redirect == self.settings.max_redirects:
                            raise ServiceError("SOURCE_FORBIDDEN")
                        if any(ord(c) <= 32 or ord(c) == 127 for c in location) or "#" in location or "\\" in location:
                            raise ServiceError("SOURCE_FORBIDDEN")
                        try:
                            destination, destination_rule = policy.validate(str(url.join(URL(location, encoded=True))))
                        except ValueError:
                            raise ServiceError("SOURCE_FORBIDDEN") from None
                        if destination.origin() != url.origin():
                            headers = {key: value for key, value in headers.items()
                                       if key.lower() not in {"authorization", "x-api-key"}}
                        url, rule = destination, destination_rule
                        continue
                    if response.status != 200:
                        raise ServiceError(
                            "SOURCE_UNREACHABLE",
                            retryable=response.status == 429 or response.status >= 500,
                        )
                    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                        raise ServiceError("SOURCE_FORBIDDEN")
                    if response.content_length is not None:
                        if response.content_length > self.settings.max_input_bytes:
                            raise ServiceError("INPUT_TOO_LARGE")
                        if source.expected_size_bytes is not None and response.content_length != source.expected_size_bytes:
                            raise ServiceError("INPUT_CHANGED")
                    async for chunk in response.content.iter_chunked(_BLOCK_SIZE):
                        self._check(job, cancel)
                        await file_job(lambda event, chunk=chunk: target.write(chunk, event))
                    return
            except aiohttp.ClientSSLError:
                raise ServiceError("SOURCE_FORBIDDEN") from None
            except (aiohttp.ClientError, TimeoutError):
                raise ServiceError("SOURCE_UNREACHABLE", retryable=True) from None
            finally:
                await resolver.close()
        raise ServiceError("SOURCE_FORBIDDEN")
