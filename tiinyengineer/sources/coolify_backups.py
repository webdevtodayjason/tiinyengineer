"""Read-only Coolify database backup freshness checks."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import statistics
from typing import Any
from urllib import error, parse, request
from xml.etree import ElementTree


SETTINGS = [
    {"key": "coolify_base_url", "label": "Coolify URL", "secret": False,
     "help": "The trusted Coolify address, ending at the instance root."},
    {"key": "coolify_api_token", "label": "Coolify read-only API token", "secret": True,
     "help": "A token limited to reading databases and backup history."},
    {"key": "coolify_s3_endpoint", "label": "Backup destination endpoint", "secret": False,
     "help": "The HTTPS S3-compatible endpoint used by Coolify backups."},
    {"key": "coolify_s3_region", "label": "Backup destination region", "secret": False,
     "help": "The signing region for the backup destination."},
    {"key": "coolify_s3_bucket", "label": "Backup destination bucket", "secret": False,
     "help": "The bucket that contains the database backups."},
    {"key": "coolify_s3_access_key", "label": "Backup destination read-only access key", "secret": True,
     "help": "A key limited to listing objects in the backup bucket."},
    {"key": "coolify_s3_secret_key", "label": "Backup destination read-only secret key", "secret": True,
     "help": "The secret for the read-only backup destination key."},
    {"key": "coolify_s3_session_token", "label": "Backup destination session token", "secret": True,
     "help": "Optional session token when the read-only destination credentials require one."},
]


SUCCESS_STATES = {"completed", "complete", "finished", "success", "successful", "succeeded"}
ALIASED_PERIODS = {
    "every_minute": 60,
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2678400,
    "yearly": 31536000,
}


def _setting(target: dict[str, Any], key: str) -> str:
    value = target.get(key)
    if value is None:
        value = os.environ.get(key.upper(), "")
    return str(value).strip()


def _api_root(target: dict[str, Any]) -> str:
    base = _setting(target, "coolify_base_url").rstrip("/")
    if not base:
        raise ValueError("Coolify URL is not configured")
    return base if base.endswith("/api/v1") else base + "/api/v1"


def _json_get(url: str, token: str, timeout: float) -> Any:
    if not token:
        raise ValueError("Coolify read-only API token is not configured")
    req = request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "tiinyengineer/0.1",
    })
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _rows(value: Any, key: str | None = None) -> list[dict[str, Any]]:
    if key and isinstance(value, dict):
        value = value.get(key, [])
    elif isinstance(value, dict):
        value = value.get("data", value.get("items", []))
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _api_executions(target: dict[str, Any]) -> list[dict[str, Any]]:
    database_uuid = str(target.get("database_uuid", "")).strip()
    backup_uuid = str(target.get("backup_uuid", "")).strip()
    if not database_uuid or not backup_uuid:
        raise ValueError("database and backup schedule are not configured")
    path = "/databases/{}/backups/{}/executions".format(
        parse.quote(database_uuid, safe=""), parse.quote(backup_uuid, safe=""))
    payload = _json_get(_api_root(target) + path, _setting(target, "coolify_api_token"),
                        float(target.get("timeout", 8)))
    return _rows(payload, "executions")


def _api_schedules(target: dict[str, Any]) -> list[dict[str, Any]]:
    database_uuid = str(target.get("database_uuid", "")).strip()
    if not database_uuid:
        raise ValueError("database is not configured")
    path = "/databases/" + parse.quote(database_uuid, safe="") + "/backups"
    return _rows(_json_get(_api_root(target) + path, _setting(target, "coolify_api_token"),
                           float(target.get("timeout", 8))))


def _signing_key(secret: str, day: str, region: str) -> bytes:
    date_key = hmac.new(("AWS4" + secret).encode(), day.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _signed_s3_request(target: dict[str, Any], continuation: str, now: datetime) -> request.Request:
    endpoint = _setting(target, "coolify_s3_endpoint").rstrip("/")
    bucket = _setting(target, "coolify_s3_bucket")
    region = _setting(target, "coolify_s3_region") or "us-east-1"
    access_key = _setting(target, "coolify_s3_access_key")
    secret_key = _setting(target, "coolify_s3_secret_key")
    if not endpoint or not bucket or not access_key or not secret_key:
        raise ValueError("backup destination read-only access is not configured")

    parts = parse.urlsplit(endpoint)
    path_style = str(target.get("s3_path_style", "true")).lower() not in {"0", "false", "no"}
    if path_style:
        host = parts.netloc
        raw_path = parts.path.rstrip("/") + "/" + bucket
    else:
        host = bucket + "." + parts.netloc
        raw_path = parts.path or "/"
    canonical_uri = parse.quote(raw_path or "/", safe="/-_.~")
    query = {"list-type": "2", "max-keys": "1000"}
    prefix = str(target.get("s3_prefix", ""))
    if prefix:
        query["prefix"] = prefix
    if continuation:
        query["continuation-token"] = continuation
    canonical_query = parse.urlencode(sorted(query.items()), quote_via=parse.quote, safe="-_.~")

    moment = now.astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    day = moment.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    session_token = _setting(target, "coolify_s3_session_token")
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    canonical_request = "\n".join(("GET", canonical_uri, canonical_query, canonical_headers,
                                    signed_headers, payload_hash))
    scope = f"{day}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(("AWS4-HMAC-SHA256", amz_date, scope,
                                 hashlib.sha256(canonical_request.encode()).hexdigest()))
    signature = hmac.new(_signing_key(secret_key, day, region), string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = parse.urlunsplit((parts.scheme or "https", host, canonical_uri, canonical_query, ""))
    return request.Request(url, headers=headers)


def _xml_text(element: ElementTree.Element, name: str) -> str:
    found = element.find(f"{{*}}{name}")
    return found.text.strip() if found is not None and found.text else ""


def _s3_executions(target: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    continuation = ""
    while True:
        req = _signed_s3_request(target, continuation, now)
        with request.urlopen(req, timeout=float(target.get("timeout", 8))) as response:
            root = ElementTree.fromstring(response.read())
        for item in root.findall("{*}Contents"):
            modified = _xml_text(item, "LastModified")
            size = _xml_text(item, "Size")
            if modified:
                rows.append({"filename": _xml_text(item, "Key"), "created_at": modified,
                             "size": int(size or 0), "status": "completed"})
        if _xml_text(root, "IsTruncated").lower() != "true":
            break
        continuation = _xml_text(root, "NextContinuationToken")
        if not continuation:
            break
    return rows


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _period_seconds(frequency: str, explicit: Any = None) -> int:
    if explicit not in (None, ""):
        return max(1, int(explicit))
    normalized = frequency.strip().lower()
    if normalized in ALIASED_PERIODS:
        return ALIASED_PERIODS[normalized]
    fields = normalized.split()
    if len(fields) != 5:
        raise ValueError("set period_seconds for this backup schedule")
    minute, hour, day, month, weekday = fields
    if minute.startswith("*/") and all(field == "*" for field in fields[1:]):
        return int(minute[2:]) * 60
    if hour.startswith("*/") and minute.isdigit() and all(field == "*" for field in fields[2:]):
        return int(hour[2:]) * 3600
    if minute.isdigit() and hour.isdigit() and day == month == weekday == "*":
        return 86400
    if minute.isdigit() and hour.isdigit() and day == month == "*" and weekday != "*":
        return 604800
    if minute.isdigit() and hour.isdigit() and day != "*" and month == weekday == "*":
        return 2678400
    raise ValueError("set period_seconds for this backup schedule")


def _successful(row: dict[str, Any]) -> bool:
    return str(row.get("status", "")).strip().lower() in SUCCESS_STATES


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _evaluate(target: dict[str, Any], executions: list[dict[str, Any]], now: datetime,
              source: str) -> tuple[bool, str, str | None]:
    frequency = str(target.get("frequency") or target.get("schedule") or "").strip()
    period = _period_seconds(frequency, target.get("period_seconds"))
    labelled = []
    for row in executions:
        at = _parse_time(row.get("created_at") or row.get("finished_at"))
        if at:
            labelled.append((at, row))
    labelled.sort(key=lambda pair: pair[0], reverse=True)
    name = str(target.get("database_name") or "database")
    if not labelled:
        return False, f"{frequency or 'scheduled'}; no backup found", f"No backup was found for {name}."

    latest_at, latest = labelled[0]
    good = [(at, row) for at, row in labelled if _successful(row)]
    if not _successful(latest):
        state = str(latest.get("status") or "failed")
        return False, f"{frequency}; last execution {latest_at.isoformat()} ({state})", (
            f"The newest backup execution for {name} did not finish successfully."
        )
    newest_at, newest = good[0]
    size = int(newest.get("size") or 0)
    age_seconds = max(0.0, (now.astimezone(timezone.utc) - newest_at).total_seconds())
    status = f"{frequency}; last good {newest_at.isoformat()}; completed; {_human_size(size)}; via {source}"
    if age_seconds > period * 1.5:
        hours = age_seconds / 3600
        return False, status, f"The newest good backup for {name} is {hours:.1f} hours old."

    previous_sizes = [int(row.get("size") or 0) for _, row in good[1:8] if int(row.get("size") or 0) > 0]
    if size > 0 and previous_sizes:
        usual = statistics.median(previous_sizes)
        if size < usual * 0.5 or size > usual * 2:
            return False, status, (
                f"The newest backup for {name} is {_human_size(size)}; recent backups are usually "
                f"{_human_size(round(usual))}."
            )
    return True, status, None


def check_coolify_backup(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Check one scheduled database backup without changing Coolify or its bucket."""
    now = _parse_time(target.get("now")) or datetime.now(timezone.utc)
    try:
        if target.get("require_backup_schedule"):
            schedules = _api_schedules(target)
            enabled = [row for row in schedules if row.get("enabled", True)]
            name = str(target.get("database_name") or "database")
            if not enabled:
                return False, "no enabled backup schedule", f"No backups are scheduled for {name}."
            frequencies = ", ".join(str(row.get("frequency") or "scheduled") for row in enabled)
            return True, f"enabled backup schedule: {frequencies}", None
        try:
            executions = _api_executions(target)
        except error.HTTPError as exc:
            if exc.code not in {404, 405, 501}:
                raise
            executions = []
        source = "Coolify"
        if not executions:
            executions = _s3_executions(target, now)
            source = "backup destination"
        return _evaluate(target, executions, now, source)
    except (ValueError, error.HTTPError, error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, ElementTree.ParseError) as exc:
        return False, "backup check unavailable", str(exc)


def _app_name(database: dict[str, Any]) -> str:
    for key in ("application_name", "project_name", "environment_name"):
        if database.get(key):
            return str(database[key])
    environment = database.get("environment")
    if isinstance(environment, dict):
        project = environment.get("project")
        if isinstance(project, dict) and project.get("name"):
            return str(project["name"])
        if environment.get("name"):
            return str(environment["name"])
    return "Coolify"


def discover_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one dashboard-ready check for every Coolify backup schedule."""
    token = _setting(config, "coolify_api_token")
    timeout = float(config.get("timeout", 8))
    databases = _rows(_json_get(_api_root(config) + "/databases", token, timeout))
    checks = []
    for database in databases:
        database_uuid = str(database.get("uuid") or "")
        if not database_uuid:
            continue
        schedules = _rows(_json_get(
            _api_root(config) + "/databases/" + parse.quote(database_uuid, safe="") + "/backups",
            token, timeout))
        if not schedules:
            database_name = str(database.get("name") or database_uuid)
            app_name = _app_name(database)
            check_id = "coolify-backup-schedule-" + re.sub(
                r"[^a-z0-9]+", "-", database_uuid.lower()).strip("-")
            checks.append({
                "id": check_id,
                "kind": "coolify_backup",
                "target": {"database_uuid": database_uuid, "database_name": database_name,
                           "application_name": app_name, "require_backup_schedule": True},
                "display_name": f"No backups for {database_name}",
                "where_text": f"{database_name} in {app_name}",
                "meaning": "A disk loss would lose all of it.",
                "first_step": "Add a scheduled backup in Coolify.",
                "owner": "operations",
                "console_url": _setting(config, "coolify_base_url"),
                "period_s": 3600,
                "grace_s": 0,
                "misses_before_real": 1,
                "severity": "warning",
                "alert": True,
                "failure_class": "backup_missing",
                "playbook": "Add a scheduled backup, then confirm its first good destination object.",
            })
            continue
        for schedule in schedules:
            backup_uuid = str(schedule.get("uuid") or "")
            frequency = str(schedule.get("frequency") or "")
            if not backup_uuid or not frequency:
                continue
            database_name = str(database.get("name") or database_uuid)
            app_name = _app_name(database)
            check_id = "coolify-backup-" + re.sub(r"[^a-z0-9]+", "-", f"{database_uuid}-{backup_uuid}".lower()).strip("-")
            target = {
                "database_uuid": database_uuid,
                "backup_uuid": backup_uuid,
                "database_name": database_name,
                "application_name": app_name,
                "frequency": frequency,
            }
            for key in ("period_seconds", "s3_prefix", "s3_path_style"):
                if key in config:
                    target[key] = config[key]
            checks.append({
                "id": check_id,
                "kind": "coolify_backup",
                "target": target,
                "display_name": f"Coolify backup for {database_name}",
                "where_text": f"{database_name} in {app_name}",
                "meaning": "Restores would lose data newer than the last good backup.",
                "first_step": "Check the newest backup in Coolify and the backup destination.",
                "owner": "operations",
                "console_url": _setting(config, "coolify_base_url"),
                "period_s": min(_period_seconds(frequency, config.get("period_seconds")), 3600),
                "grace_s": 0,
                "misses_before_real": 1,
                "severity": "critical",
                "alert": True,
                "failure_class": "backup_stale",
                "playbook": "Check the latest execution and destination object, then correct the backup job.",
            })
    return checks


KINDS = {"coolify_backup": check_coolify_backup}
