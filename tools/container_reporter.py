#!/usr/bin/env python3
"""Post one Docker host's container state to TiinyEngineer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib import error, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tiinyengineer.sources.containers import DEFAULT_SOCKET_PATH, container_logs, docker_containers


def build_report(host: str, socket_path: str, timeout: float) -> dict[str, object]:
    containers = []
    for row in docker_containers(socket_path, timeout):
        state = str(row.get("State") or "unknown")
        status = str(row.get("Status") or state)
        unhealthy = "unhealthy" in status.casefold()
        item = {
            "Id": str(row.get("Id") or ""),
            "Names": [str(name) for name in row.get("Names", []) if isinstance(name, str)],
            "Image": str(row.get("Image") or ""),
            "State": state,
            "Status": status,
        }
        if state.casefold() != "running" or unhealthy:
            try:
                item["logs"] = container_logs(item["Id"], socket_path, timeout).splitlines()[-50:]
            except (OSError, TimeoutError, ValueError) as exc:
                item["logs"] = [f"Container logs could not be read: {exc}"]
        containers.append(item)
    return {
        "host": host,
        "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "containers": containers,
    }


def post_report(endpoint: str, token: str, report: dict[str, object], timeout: float) -> None:
    body = json.dumps(report, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        endpoint.rstrip("/") + "/api/containers/report",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "tiinyengineer-container-reporter/0.1",
        },
    )
    with request.urlopen(req, timeout=timeout) as response:
        if response.status not in (200, 202):
            raise OSError(f"TiinyEngineer returned HTTP {response.status}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Report local Docker container state to TiinyEngineer")
    parser.add_argument("--endpoint", default=os.environ.get("TIINYENGINEER_URL", ""))
    parser.add_argument("--host", default=os.environ.get("CONTAINER_REPORT_HOST", ""))
    parser.add_argument("--socket", default=os.environ.get("DOCKER_SOCKET_PATH", DEFAULT_SOCKET_PATH))
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    token = os.environ.get("CONTAINER_REPORT_TOKEN", "")
    if not args.endpoint or not args.host or not token:
        parser.error("set TIINYENGINEER_URL, CONTAINER_REPORT_HOST, and CONTAINER_REPORT_TOKEN")
    try:
        post_report(args.endpoint, token, build_report(args.host, args.socket, args.timeout), args.timeout)
    except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"container report failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
