"""Container health from Docker reports and the read-only Coolify API."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import socket
from typing import Any
from urllib import error, parse, request


DEFAULT_SOCKET_PATH = "/var/run/docker.sock"
DEPLOYING = {"deploying", "in_progress", "queued", "restarting", "starting"}

SETTINGS = [
    {
        "key": "container_local_enabled",
        "label": "Watch containers on this host",
        "secret": False,
        "help": "Set to true to discover and watch running containers through the local Docker socket.",
    },
    {
        "key": "container_local_name",
        "label": "This Docker host name",
        "secret": False,
        "help": "The plain host name shown in container alerts.",
    },
    {
        "key": "container_socket_path",
        "label": "Local Docker socket",
        "secret": False,
        "help": "The default works on a standard Docker Engine installation.",
    },
    {
        "key": "container_reporter_hosts",
        "label": "Container reporter hosts",
        "secret": False,
        "help": "Comma-separated Docker host names allowed to report.",
    },
    {
        "key": "container_reporter_tokens",
        "label": "Container reporter tokens",
        "secret": True,
        "help": "JSON object mapping each allowed host name to its private reporter token.",
    },
    {
        "key": "coolify_base_url",
        "label": "Coolify URL",
        "secret": False,
        "help": "The trusted Coolify address, ending at the instance root.",
    },
    {
        "key": "coolify_api_token",
        "label": "Coolify read-only API token",
        "secret": True,
        "help": "A read-only token used to see app, service, and deployment state.",
    },
    {
        "key": "coolify_container_host",
        "label": "Coolify Docker host",
        "secret": False,
        "help": "The reporter host name where Coolify runs its app and service containers.",
    },
]


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection carried by a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("local-docker.sock", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


def _docker_get(socket_path: str, path: str, timeout: float = 5.0) -> bytes:
    connection = UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("GET", path, headers={"Host": "localhost"})
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"Docker API returned HTTP {response.status}")
        return body
    finally:
        connection.close()


def docker_containers(socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = 5.0) -> list[dict[str, Any]]:
    value = json.loads(_docker_get(socket_path, "/containers/json?all=1", timeout))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError("Docker API returned an invalid container list")
    return value


def _decode_logs(body: bytes) -> str:
    """Decode Docker's multiplexed log stream, or a plain fixture response."""
    chunks: list[bytes] = []
    offset = 0
    while offset + 8 <= len(body) and body[offset] in (0, 1, 2) and body[offset + 1:offset + 4] == b"\0\0\0":
        size = int.from_bytes(body[offset + 4:offset + 8], "big")
        end = offset + 8 + size
        if end > len(body):
            chunks = []
            break
        chunks.append(body[offset + 8:end])
        offset = end
    raw = b"".join(chunks) if chunks and offset == len(body) else body
    lines = raw.decode("utf-8", "replace").splitlines()[-50:]
    return "\n".join(line[-1000:] for line in lines)


def container_logs(container_id: str, socket_path: str = DEFAULT_SOCKET_PATH, timeout: float = 5.0) -> str:
    quoted = parse.quote(container_id, safe="")
    body = _docker_get(socket_path, f"/containers/{quoted}/logs?stdout=1&stderr=1&tail=50", timeout)
    return _decode_logs(body)


def _names(row: dict[str, Any]) -> set[str]:
    names = {str(row.get("Id", "")), str(row.get("Id", ""))[:12]}
    for name in row.get("Names", []):
        if isinstance(name, str):
            names.add(name.lstrip("/"))
    for key in ("Name", "name"):
        if row.get(key):
            names.add(str(row[key]).lstrip("/"))
    return {name for name in names if name}


def _find(rows: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    for row in rows:
        names = _names(row)
        container_id = str(row.get("Id", ""))
        if wanted in names or (len(wanted) >= 12 and container_id.startswith(wanted)):
            return row
    return None


def _health(row: dict[str, Any]) -> tuple[bool, str]:
    state = str(row.get("State") or row.get("state") or "unknown").casefold()
    status = str(row.get("Status") or row.get("status") or state)
    health = str(row.get("Health") or row.get("health") or "").casefold()
    running = state == "running"
    healthy = health not in {"unhealthy", "starting"} and "(unhealthy)" not in status.casefold()
    return running and healthy, status


def _log_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(line) for line in value][-50:]
    return str(value or "").splitlines()[-50:]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _report_path(target: dict[str, Any]) -> Path:
    root = Path(str(target.get("reports_dir") or os.environ.get("FARM_DATA_DIR") or "."))
    host = safe_host(str(target["host"]))
    return root / "container-reports" / f"{host}.json"


def safe_host(host: str) -> str:
    value = re.sub(r"[^a-z0-9.-]+", "-", host.strip().casefold()).strip("-.")
    if not value or len(value) > 80:
        raise ValueError("host name is invalid")
    return value


def save_report(data_path: Path, report: dict[str, Any]) -> Path:
    host = safe_host(str(report.get("host", "")))
    rows = report.get("containers")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("containers must be a list of objects")
    reported_at = _parse_time(report.get("reported_at"))
    if reported_at is None:
        raise ValueError("reported_at must be an ISO timestamp")
    normalized = {"host": host, "reported_at": reported_at.isoformat(timespec="seconds"), "containers": rows}
    directory = data_path / "container-reports"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{host}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(normalized, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    return path


def _load_report(target: dict[str, Any]) -> list[dict[str, Any]]:
    path = _report_path(target)
    value = json.loads(path.read_text(encoding="utf-8"))
    reported_at = _parse_time(value.get("reported_at")) if isinstance(value, dict) else None
    if reported_at is None:
        raise ValueError("container report has no valid timestamp")
    now = target.get("_now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    age = (now.astimezone(timezone.utc) - reported_at).total_seconds()
    if age < -300:
        raise ValueError("container report timestamp is too far in the future")
    if age > float(target.get("max_report_age", 150)):
        raise TimeoutError(f"last container report is {int(age)} seconds old")
    rows = value.get("containers")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("container report has an invalid container list")
    return rows


def _setting(target: dict[str, Any], key: str) -> str:
    return str(target.get(key) or os.environ.get(key.upper(), "")).strip()


def _check_id(host: str, container: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", f"container-{host}-{container}".casefold()).strip("-")
    return value[:80].rstrip("-")


def _container_name(row: dict[str, Any]) -> str:
    named = sorted(name for name in _names(row) if name not in {str(row.get("Id", "")), str(row.get("Id", ""))[:12]})
    return named[0] if named else str(row.get("Id", ""))[:12]


def checks_from_rows(host: str, rows: list[dict[str, Any]], mode: str,
                     reports_dir: str = "", socket_path: str = DEFAULT_SOCKET_PATH) -> list[dict[str, Any]]:
    """Return checks for containers that are running when first discovered."""
    found = []
    for row in rows:
        if str(row.get("State") or row.get("state") or "").casefold() != "running":
            continue
        name = _container_name(row)
        if not name:
            continue
        target = {"mode": mode, "host": host, "container": name}
        if mode == "report":
            if reports_dir:
                target["reports_dir"] = reports_dir
        else:
            target["socket_path"] = socket_path
        found.append({
            "id": _check_id(host, name), "kind": "docker", "target": target,
            "display_name": f"{name} container", "where_text": f"{host}, Docker",
            "meaning": "The container stopped after it had been running.",
            "first_step": "Check the attached log lines and the host's last known good state.",
            "owner": "operations", "console_url": "", "period_s": 60, "grace_s": 0,
            "misses_before_real": 2, "severity": "warning", "alert": True,
            "failure_class": "container_stopped",
            "playbook": "Read the last 50 log lines, attempt at most one restart, then verify two green checks.",
            "cooldown_s": 600,
        })
    return found


def discover_local_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    socket_path = str(config.get("container_socket_path") or DEFAULT_SOCKET_PATH)
    host = str(config.get("container_local_name") or socket.gethostname())
    rows = docker_containers(socket_path, float(config.get("timeout", 5)))
    return checks_from_rows(host, rows, "local", socket_path=socket_path)


def _coolify_state(target: dict[str, Any]) -> str | None:
    resource_uuid = str(target.get("coolify_resource_uuid") or "").strip()
    if not resource_uuid:
        return None
    base = _setting(target, "coolify_base_url").rstrip("/")
    token = _setting(target, "coolify_api_token")
    resource_type = str(target.get("coolify_resource_type") or "application").casefold()
    segment = "services" if resource_type == "service" else "applications"
    req = request.Request(
        f"{base}/api/v1/{segment}/{parse.quote(resource_uuid, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "tiinyengineer/0.1"},
    )
    with request.urlopen(req, timeout=float(target.get("timeout", 5))) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("Coolify returned an invalid resource document")
    for key in ("deployment_status", "latest_deployment_status", "status"):
        state = str(value.get(key) or "").casefold().replace(" ", "_").replace("-", "_")
        if state in DEPLOYING or any(word in state for word in ("deploying", "in_progress", "queued")):
            return state
    return None


def check_container(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Check one container, deferring while its Coolify resource is deploying."""
    name = str(target["container"])
    try:
        deploying = _coolify_state(target)
        if deploying:
            return True, f"deployment in progress for {name}; check skipped", None
        if str(target.get("mode", "local")) == "report":
            rows = _load_report(target)
            source = str(target.get("host") or "reported host")
        else:
            rows = docker_containers(str(target.get("socket_path", DEFAULT_SOCKET_PATH)), float(target.get("timeout", 5)))
            source = "this host"
        row = _find(rows, name)
        if row is None:
            return False, f"{name} is missing on {source}", "The container is not in Docker's container list."
        ok, status = _health(row)
        if ok:
            return True, f"{name} is {status} on {source}", None
        logs = _log_lines(row.get("logs"))
        if not logs and str(target.get("mode", "local")) != "report":
            logs = container_logs(str(row.get("Id") or name), str(target.get("socket_path", DEFAULT_SOCKET_PATH)),
                                  float(target.get("timeout", 5))).splitlines()[-50:]
        evidence = "last 50 log lines:\n" + "\n".join(logs) if logs else "No container log lines were returned."
        return False, f"{name} is {status} on {source}", evidence
    except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"could not verify {name}", str(exc)


def _coolify_rows(config: dict[str, Any], segment: str) -> list[dict[str, Any]]:
    base = _setting(config, "coolify_base_url").rstrip("/")
    token = _setting(config, "coolify_api_token")
    req = request.Request(
        f"{base}/api/v1/{segment}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "tiinyengineer/0.1"},
    )
    with request.urlopen(req, timeout=float(config.get("timeout", 5))) as response:
        value = json.load(response)
    rows = value.get("data", value) if isinstance(value, dict) else value
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Coolify returned an invalid resource list")
    return rows


def discover_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build dashboard-ready checks for Coolify applications and services."""
    found: list[dict[str, Any]] = []
    host = str(config.get("host") or "container-host")
    for segment, resource_type in (("applications", "application"), ("services", "service")):
        for row in _coolify_rows(config, segment):
            resource_uuid = str(row.get("uuid") or row.get("id") or "")
            name = str(row.get("name") or resource_uuid)
            container = str(row.get("container_name") or name)
            check_id = _check_id(host, container)
            target = {
                "mode": "report", "host": host, "container": container,
                "coolify_resource_uuid": resource_uuid, "coolify_resource_type": resource_type,
                "coolify_base_url": _setting(config, "coolify_base_url"),
            }
            if config.get("reports_dir"):
                target["reports_dir"] = str(config["reports_dir"])
            found.append({
                "id": check_id, "kind": "docker", "target": target,
                "display_name": f"{name} container", "where_text": f"{host}, Coolify",
                "meaning": "The service is not running after its deployment finished.",
                "first_step": "Check the attached log lines and the latest Coolify deployment.",
                "owner": "operations", "console_url": _setting(config, "coolify_base_url"),
                "period_s": 60, "grace_s": 0, "misses_before_real": 2,
                "severity": "critical", "alert": True, "failure_class": "container_stopped",
                "playbook": "Read the last 50 log lines, attempt at most one restart, then verify two green checks.",
                "cooldown_s": 600,
            })
    return found


KINDS = {"docker": check_container}
