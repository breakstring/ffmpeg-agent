import os
import sys

import uvicorn

from media_service.api import create_app
from media_service.config import Settings
from media_service.telemetry import configure, server_logging


def main():
    if len(sys.argv) != 1 or os.environ.get("WEB_CONCURRENCY", "1") != "1":
        raise SystemExit(
            "Run one API process; configure MEDIA_WORKER_CONCURRENCY for logical workers"
        )
    os.umask(0o077)
    try:
        settings = Settings.from_file()
    except Exception:
        raise SystemExit("Invalid private service configuration") from None
    configure(settings.log_level)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        reload=False,
        access_log=False,
        log_config=server_logging(),
    )


if __name__ == "__main__":
    main()
