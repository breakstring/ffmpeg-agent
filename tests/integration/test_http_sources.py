import asyncio
import hashlib
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

import media_service.downloader as download_module
from media_service.config import Settings, SourceRule
from media_service.database import Database
from media_service.downloader import Downloader
from media_service.errors import ServiceError
from media_service.models import HttpFile, JobCreate
from media_service.state import State, uid

MEDIA = (Path(__file__).resolve().parents[1] / "fixtures" / "222.mp3").read_bytes()


@dataclass
class SourceServer:
    url: str
    port: int
    routes: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)


@pytest.fixture
async def servers():
    runners = []

    async def start():
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 0))
        except BaseException:
            listener.close()
            raise
        port = listener.getsockname()[1]
        source = SourceServer(f"http://127.0.0.1:{port}", port)

        async def dispatch(request):
            source.requests.append({
                "method": request.method, "path": request.path,
                "authorization": request.headers.get("Authorization"),
                "x-api-key": request.headers.get("X-Api-Key"),
            })
            handler = source.routes.get(request.path)
            if handler is None:
                return web.Response(body=MEDIA)
            return await handler(request)

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", dispatch)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
        await runner.setup()
        try:
            await web.SockSite(runner, listener).start()
        except BaseException:
            listener.close()
            await runner.cleanup()
            raise
        runners.append(runner)
        return source

    try:
        yield start
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest.fixture
async def state(tmp_path):
    settings = Settings.model_validate({
        "state_dir": tmp_path / "state", "work_dir": tmp_path / "work",
        "storages": {"output": {"root": tmp_path / "output", "role": "output",
                                  "callers": ["test"]}},
        "callers": {"test": {"tokens": ["http-test-only-no-business-access-token"],
                               "default_output_storage": "output"}},
        "disk_reserve_bytes": 0,
        "http_attempts": 1,
        "http_connect_seconds": 1,
        "http_idle_seconds": 1,
        "http_download_seconds": 2,
    })
    settings.work_dir.mkdir()
    settings.storages["output"].root.mkdir()
    db = Database(settings.state_dir)
    await db.open()
    try:
        yield State(db, settings)
    finally:
        await db.close()


def allow(state, server, *, host="127.0.0.1", networks=None):
    state.settings.callers["test"].source_rules.append(SourceRule(
        host=host, ports=[server.port], schemes=["http"],
        networks=["127.0.0.1/32"] if networks is None else networks,
    ))


async def start_job(state, url, **fields):
    source = HttpFile(kind="http_file", url=url, **fields)
    request = JobCreate.model_validate({
        "inputs": {"main": source.model_dump()},
        "steps": [{"id": "probe", "operation": "media.probe.v1",
                   "inputs": {"media": {"source": "main"}}}],
        "outputs": {"report": {"step": "probe", "output": "report"}},
    })
    job_id, _ = await state.accept("test", uid("http"), request, {})
    job = await state.claim()
    assert job is not None and job["id"] == job_id
    return job, source


async def failure(awaitable, code):
    with pytest.raises(ServiceError) as error:
        await awaitable
    assert error.value.code == code
    return error.value


async def assert_no_checkpoint_or_partial(state):
    assert await state.db.all("SELECT * FROM downloads") == []
    assert list(state.settings.work_dir.rglob("*.part")) == []


async def test_real_media_get_without_head_and_checkpoint_reuse_after_database_reopen(state, servers):
    server = await servers()
    allow(state, server)
    job, source = await start_job(
        state, f"{server.url}/source?signature=test-only",
        expected_size_bytes=len(MEDIA), expected_sha256=hashlib.sha256(MEDIA).hexdigest(),
        expires_at="2000-01-01T00:00:00Z",
    )
    path = await Downloader(state).acquire(job, "main", source, asyncio.Event())
    assert path.read_bytes() == MEDIA
    assert [request["method"] for request in server.requests] == ["GET"]

    async def changed(request):
        return web.Response(body=b"the source now contains different bytes")

    server.routes["/source"] = changed
    await state.db.close()
    await state.db.open()
    restored = await Downloader(State(state.db, state.settings)).acquire(
        job, "main", source, asyncio.Event()
    )
    assert restored == path and restored.read_bytes() == MEDIA
    assert len(server.requests) == 1
    row = await state.db.one("SELECT * FROM downloads")
    assert row is not None and row["path"] == f"{job['id']}/inputs/main.media"
    assert row["sha256"] == hashlib.sha256(MEDIA).hexdigest()


async def test_modified_fixed_copy_fails_without_refetch(state, servers):
    server = await servers()
    allow(state, server)
    job, source = await start_job(state, server.url)
    downloader = Downloader(state)
    path = await downloader.acquire(job, "main", source, asyncio.Event())
    path.chmod(0o600)
    path.write_bytes(b"changed")
    await failure(downloader.acquire(job, "main", source, asyncio.Event()), "INPUT_CHANGED")
    assert len(server.requests) == 1


@pytest.mark.parametrize("field", ["size", "digest"])
async def test_expected_integrity_mismatch_never_creates_checkpoint(state, servers, field):
    server = await servers()
    allow(state, server)
    options = {"expected_size_bytes": len(MEDIA) + 1} if field == "size" else {"expected_sha256": "0" * 64}
    job, source = await start_job(state, server.url, **options)
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "INPUT_CHANGED")
    await assert_no_checkpoint_or_partial(state)


async def test_chunked_response_enforces_actual_byte_limit(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.max_input_bytes = 100

    async def chunked(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"a" * 80)
        await response.write(b"b" * 80)
        await response.write_eof()
        return response

    server.routes["/large"] = chunked
    job, source = await start_job(state, f"{server.url}/large")
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "INPUT_TOO_LARGE")
    await assert_no_checkpoint_or_partial(state)


@pytest.mark.parametrize("reason", ["private", "wrong-port", "wrong-host", "userinfo", "fragment", "control"])
async def test_source_policy_rejects_before_connect(state, servers, reason):
    server = await servers()
    allow(state, server, networks=[] if reason == "private" else None)
    url = server.url
    if reason == "wrong-port":
        state.settings.callers["test"].source_rules[0].ports = [server.port + 1]
    elif reason == "wrong-host":
        state.settings.callers["test"].source_rules[0].host = "other.example.invalid"
    elif reason == "userinfo":
        url = url.replace("http://", "http://test-only-user@")
    elif reason == "fragment":
        url += "/source#fragment"
    elif reason == "control":
        url += "/source\n"
    job, source = await start_job(state, url)
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_FORBIDDEN")
    assert server.requests == []
    await assert_no_checkpoint_or_partial(state)


async def test_redirects_revalidate_each_hop_and_strip_cross_origin_credentials(state, servers):
    first, second = await servers(), await servers()
    allow(state, first)
    allow(state, second)

    async def redirect_same(request):
        raise web.HTTPFound("/cross")

    async def redirect_cross(request):
        raise web.HTTPFound(f"{second.url}/result")

    first.routes["/start"] = redirect_same
    first.routes["/cross"] = redirect_cross
    job, source = await start_job(state, f"{first.url}/start", headers={
        "authorization": "Bearer synthetic-test-only", "x-api-key": "synthetic-test-only-key",
    })
    assert (await Downloader(state).acquire(job, "main", source, asyncio.Event())).read_bytes() == MEDIA
    assert all(request["authorization"] == "Bearer synthetic-test-only" for request in first.requests)
    assert second.requests[0]["authorization"] is None
    assert second.requests[0]["x-api-key"] is None


async def test_redirect_to_unapproved_origin_never_connects(state, servers):
    first, second = await servers(), await servers()
    allow(state, first)

    async def redirect(request):
        raise web.HTTPFound(f"{second.url}/result")

    first.routes["/start"] = redirect
    job, source = await start_job(state, f"{first.url}/start")
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_FORBIDDEN")
    assert len(first.requests) == 1 and second.requests == []


async def test_redirect_loop_has_finite_hop_budget(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.max_redirects = 2

    async def redirect(request):
        raise web.HTTPFound("/again")

    server.routes["/again"] = redirect
    job, source = await start_job(state, f"{server.url}/again")
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_FORBIDDEN")
    assert len(server.requests) == 3
    await assert_no_checkpoint_or_partial(state)


async def test_encoded_http_response_is_rejected_without_decompression(state, servers):
    server = await servers()
    allow(state, server)

    async def encoded(request):
        return web.Response(body=b"not a supported encoded source", headers={"Content-Encoding": "gzip"})

    server.routes["/encoded"] = encoded
    job, source = await start_job(state, f"{server.url}/encoded")
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_FORBIDDEN")
    await assert_no_checkpoint_or_partial(state)


async def test_transient_http_errors_retry_within_attempt_budget(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.http_attempts = 3

    async def transient(request):
        if len(server.requests) < 3:
            return web.Response(status=429 if len(server.requests) == 1 else 503)
        return web.Response(body=MEDIA)

    server.routes["/retry"] = transient
    job, source = await start_job(state, f"{server.url}/retry")
    assert (await Downloader(state).acquire(job, "main", source, asyncio.Event())).read_bytes() == MEDIA
    assert len(server.requests) == 3
    assert list(state.settings.work_dir.rglob("*.part")) == []


async def test_forbidden_response_does_not_retry_or_claim_url_expiry(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.http_attempts = 3

    async def forbidden(request):
        return web.Response(status=403)

    server.routes["/forbidden"] = forbidden
    job, source = await start_job(state, f"{server.url}/forbidden?signature=synthetic-test-only")
    error = await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_UNREACHABLE")
    assert not error.retryable
    assert "signature" not in str(error) and "127.0.0.1" not in str(error.public())
    assert len(server.requests) == 1


async def test_midstream_disconnect_retries_without_partial_checkpoint(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.http_attempts = 2

    async def disconnected(request):
        response = web.StreamResponse(headers={"Content-Length": "1000"})
        await response.prepare(request)
        await response.write(b"only-a-prefix")
        request.transport.close()
        return response

    server.routes["/disconnect"] = disconnected
    job, source = await start_job(state, f"{server.url}/disconnect")
    error = await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_UNREACHABLE")
    assert error.retryable and len(server.requests) == 2
    await assert_no_checkpoint_or_partial(state)


@pytest.mark.parametrize(("budget", "expected"), [
    ("idle", "SOURCE_UNREACHABLE"),
    ("download", "SOURCE_UNREACHABLE"),
    ("task", "TASK_TIMEOUT"),
])
async def test_idle_download_and_remaining_task_budgets(state, servers, budget, expected):
    server = await servers()
    allow(state, server)
    if budget == "idle":
        state.settings.http_idle_seconds = 0.04
    elif budget == "download":
        state.settings.http_download_seconds = 0.08

    async def slow(request):
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for _ in range(20):
                await response.write(b"a")
                await asyncio.sleep(0.15 if budget == "idle" else 0.025)
        except ConnectionResetError:
            pass
        return response

    server.routes["/slow"] = slow
    job, source = await start_job(state, f"{server.url}/slow")
    if budget == "task":
        job["deadline"] = time.time() + 0.08
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), expected)
    await assert_no_checkpoint_or_partial(state)


async def test_environment_proxy_is_not_used(state, servers, monkeypatch):
    server = await servers()
    allow(state, server)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    job, source = await start_job(state, server.url)
    assert (await Downloader(state).acquire(job, "main", source, asyncio.Event())).read_bytes() == MEDIA
    assert len(server.requests) == 1


@pytest.mark.parametrize("mixed_first_response", [False, True])
async def test_connector_dns_results_reject_rebinding_and_mixed_forbidden_addresses(
    state, servers, monkeypatch, mixed_first_response
):
    server = await servers()
    allow(state, server, host="media.test.invalid")
    calls = []

    class RebindingResolver(AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            calls.append(host)
            addresses = ["127.0.0.1", "127.0.0.2"] if mixed_first_response else [
                "127.0.0.1" if len(calls) == 1 else "127.0.0.2"
            ]
            return [ResolveResult(hostname=host, host=address, port=port, family=socket.AF_INET,
                                  proto=socket.IPPROTO_TCP, flags=0) for address in addresses]

        async def close(self):
            pass

    async def redirect(request):
        raise web.HTTPFound("/final")

    monkeypatch.setattr(download_module, "DefaultResolver", RebindingResolver)
    server.routes["/start"] = redirect
    job, source = await start_job(state, f"http://media.test.invalid:{server.port}/start")
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "SOURCE_FORBIDDEN")
    assert len(calls) == (1 if mixed_first_response else 2)
    assert [request["path"] for request in server.requests] == ([] if mixed_first_response else ["/start"])
    await assert_no_checkpoint_or_partial(state)


async def test_checkpoint_eligibility_failure_never_returns_uncommitted_file(state, servers, monkeypatch):
    server = await servers()
    allow(state, server)
    job, source = await start_job(state, server.url)
    original = state.eligible
    checks = 0

    async def lose_eligibility(tx, *args, **kwargs):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise ServiceError("EXECUTION_INTERRUPTED", 409)
        return await original(tx, *args, **kwargs)

    monkeypatch.setattr(state, "eligible", lose_eligibility)
    await failure(Downloader(state).acquire(job, "main", source, asyncio.Event()), "EXECUTION_INTERRUPTED")
    await assert_no_checkpoint_or_partial(state)
    monkeypatch.setattr(state, "eligible", original)
    assert (await Downloader(state).acquire(job, "main", source, asyncio.Event())).read_bytes() == MEDIA
    assert len(server.requests) == 2


async def test_cancellation_waits_for_real_file_write_and_removes_partial(state, servers, monkeypatch):
    server = await servers()
    allow(state, server)
    job, source = await start_job(state, server.url)
    started, released, stopped = threading.Event(), threading.Event(), threading.Event()
    original = download_module._Target.write

    def blocked(self, data, event):
        started.set()
        try:
            if not released.wait(5):
                raise RuntimeError("test write gate timed out")
            return original(self, data, event)
        finally:
            stopped.set()

    monkeypatch.setattr(download_module._Target, "write", blocked)
    cancel = asyncio.Event()
    task = asyncio.create_task(Downloader(state).acquire(job, "main", source, cancel))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        cancel.set()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not stopped.is_set()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert stopped.is_set()
        await assert_no_checkpoint_or_partial(state)
    finally:
        released.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_actual_http_downloads_share_the_whole_work_budget(state, servers):
    server = await servers()
    allow(state, server)
    state.settings.worker_concurrency = 2
    state.settings.max_work_bytes = 100 * 1024
    existing = state.settings.work_dir / "another-owned-work-file.bin"
    existing.write_bytes(b"e" * (20 * 1024))
    both_requested = asyncio.Event()
    data = b"v" * (80 * 1024)

    async def synchronized(request):
        if len(server.requests) == 2:
            both_requested.set()
        await asyncio.wait_for(both_requested.wait(), 2)
        return web.Response(body=data)

    server.routes["/first"] = server.routes["/second"] = synchronized
    first, first_source = await start_job(state, f"{server.url}/first")
    second, second_source = await start_job(state, f"{server.url}/second")
    downloader = Downloader(state)
    outcomes = await asyncio.gather(
        downloader.acquire(first, "main", first_source, asyncio.Event()),
        downloader.acquire(second, "main", second_source, asyncio.Event()),
        return_exceptions=True,
    )
    paths = [result for result in outcomes if isinstance(result, Path)]
    errors = [result for result in outcomes if isinstance(result, ServiceError)]
    assert len(paths) == len(errors) == 1
    assert errors[0].code == "OUTPUT_LIMIT"
    assert paths[0].read_bytes() == data
    assert existing.read_bytes() == b"e" * (20 * 1024)
    assert len(server.requests) == 2
    assert list(state.settings.work_dir.rglob("*.part")) == []
    assert sum(path.stat().st_size for path in state.settings.work_dir.rglob("*") if path.is_file()) <= state.settings.max_work_bytes
