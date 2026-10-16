"""Upload with HTTP, process with remote MCP, and download a verified result."""

import argparse
import asyncio
import logging
import sys

import httpx2
from client_common import (
    TERMINAL,
    Client,
    ExampleError,
    check_job,
    parse_args,
    prepare,
    print_job,
)
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def call(session: ClientSession, name: str, arguments: dict) -> dict:
    result = await session.call_tool(name, arguments)
    if result.is_error or not isinstance(result.structured_content, dict):
        raise ExampleError(f"MCP tool {name} failed. Check the request and service status.")
    return result.structured_content


async def wait_for_job(session: ClientSession, job: dict, wait_seconds: float) -> dict:
    try:
        async with asyncio.timeout(wait_seconds):
            while job.get("status") not in TERMINAL:
                await asyncio.sleep(1)
                job = await call(session, "get_job", {"job_id": job["job_id"]})
            return job
    except TimeoutError:
        raise ExampleError(
            "Waiting timed out; the server job was not canceled. "
            "Run again with --job-id and the printed job_id to resume."
        ) from None


async def run(args: argparse.Namespace, params: dict, client: Client) -> int:
    arguments = None
    if args.job_id is None:
        print("request_id:", args.request_id, flush=True)
        uploaded = await asyncio.to_thread(client.upload, args.source, args.request_id)
        print("file_id:", uploaded["file_id"], flush=True)
        arguments = {
            "source": {"kind": "uploaded_file", "file_id": uploaded["file_id"]},
            "idempotency_key": "example-job-" + args.request_id,
        }
        if args.params is not None:
            arguments["params"] = params

    async with httpx2.AsyncClient(headers=client.headers, timeout=30) as http:
        async with streamable_http_client(f"{client.base}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=30) as session:
                await session.initialize()
                if arguments is None:
                    job = await call(session, "get_job", {"job_id": args.job_id})
                else:
                    job = await call(session, args.operation, arguments)
                print_job(job)
                job = await wait_for_job(session, job, args.wait_seconds)
                artifact_id = check_job(job)
                artifact = await call(session, "get_artifact", {"artifact_id": artifact_id})
                await asyncio.to_thread(client.save_artifact, artifact, args.destination)
                print("Download verified", flush=True)
                if job["status"] == "partial":
                    print("The job completed partially; successful results were saved.")
                    return 2
    return 0


def controlled_error(error: BaseException) -> ExampleError | None:
    if isinstance(error, ExampleError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            if result := controlled_error(nested):
                return result
    return None


def main() -> int:
    # SDK 异常日志可能带有远端响应正文或请求地址。
    logging.disable(logging.CRITICAL)
    try:
        args = parse_args(
            "Upload a file, process it with remote MCP, and download the result. "
            "MCP always uploads first; --upload-first has no additional effect."
        )
        params = prepare(args)
        client = Client(args.config, args.base_url, env_file=args.env_file)
        return asyncio.run(run(args, params, client))
    except KeyboardInterrupt:
        print(
            "Interrupted. Use --job-id with the printed job_id to resume; "
            "the server job was not canceled.",
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        message = controlled_error(error)
        print(
            f"Error: {message}" if message else
            "MCP workflow failed. Check the service URL, Token and server availability.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
