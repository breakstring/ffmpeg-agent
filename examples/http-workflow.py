"""上传、异步处理和校验下载；HTTP 调用仅依赖 Python 标准库。"""

import sys
import time

from client_common import (
    TERMINAL,
    Client,
    ExampleError,
    check_job,
    definition,
    parse_args,
    prepare,
    print_job,
)


def main() -> int:
    args = parse_args("Upload, process and download over HTTP")
    params = prepare(args)
    client = Client(args.config, args.base_url)
    if args.job_id:
        job = client.api("GET", "/v1/jobs/" + args.job_id)
    else:
        print("request_id:", args.request_id, flush=True)
        if args.upload_first:
            uploaded = client.upload(args.source, args.request_id)
            print("file_id:", uploaded["file_id"], flush=True)
            job = client.api("POST", "/v1/jobs", definition(args.operation, {
                "kind": "uploaded_file", "file_id": uploaded["file_id"],
            }, params), {"Idempotency-Key": "example-job-" + args.request_id})
        else:
            job = client.submit_multipart(args.source, args.request_id, definition(args.operation, {
                "kind": "upload", "part": "source",
            }, params))
    print_job(job)
    deadline = time.monotonic() + args.wait_seconds
    while job["status"] not in TERMINAL:
        if time.monotonic() >= deadline:
            raise ExampleError("Wait timed out; resume with --job-id " + job["job_id"]
                               + ". The service task has not been canceled")
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        job = client.api("GET", "/v1/jobs/" + job["job_id"])
    artifact_id = check_job(job)
    artifact = client.api("GET", "/v1/artifacts/" + artifact_id)
    client.save_artifact(artifact, args.destination)
    print("Download verified", flush=True)
    if job["status"] == "partial":
        print("Partial result: successful members saved; see manifest.json", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExampleError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (Exception, KeyboardInterrupt) as error:
        print("Example stopped: " + type(error).__name__
              + "; use the printed job_id to resume if accepted", file=sys.stderr)
        sys.exit(1)
