"""Read-only RouterOS health, interface, DHCP, and log checks."""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import threading
from typing import Any
from urllib import error, parse, request


SETTINGS = [
    {"key": "mikrotik_rest_url", "label": "RouterOS REST URL", "secret": False,
     "help": "The trusted address of a RouterOS device. Add one check per device."},
    {"key": "mikrotik_rest_username", "label": "RouterOS read-only user", "secret": True,
     "help": "Use a different account limited to read access on each device."},
    {"key": "mikrotik_rest_password", "label": "RouterOS read-only password", "secret": True,
     "help": "The password for that device's read-only account."},
    {"key": "mikrotik_cpu_threshold", "label": "Router CPU warning percent", "secret": False,
     "help": "A poll fails when CPU load reaches this percentage. Default: 85."},
    {"key": "mikrotik_temperature_threshold", "label": "Router temperature warning", "secret": False,
     "help": "A poll fails when any reported temperature reaches this Celsius value. Default: 75."},
    {"key": "mikrotik_dhcp_threshold", "label": "DHCP pool warning percent", "secret": False,
     "help": "A poll fails when active leases use this percentage of the available pool. Default: 90."},
    {"key": "mikrotik_syslog_host", "label": "Syslog listen address", "secret": False,
     "help": "Address for RouterOS devices to send logs to. Default: all local addresses."},
    {"key": "mikrotik_syslog_port", "label": "Syslog UDP port", "secret": False,
     "help": "Unprivileged UDP port for RouterOS logs. Default: 5514."},
]


RESOURCE_PATH = "/system/resource"
HEALTH_PATH = "/system/health"
INTERFACE_PATH = "/interface"
LEASE_PATH = "/ip/dhcp-server/lease"
POOL_PATH = "/ip/pool"
LOG_PATH = "/log"
INTERFACE_FIELDS = (
    "name,type,running,disabled,link-downs,last-link-up-time,last-link-down-time,"
    "rx-error,tx-error,rx-drop,tx-drop"
)
_COUNTER_FIELDS = ("rx-error", "tx-error", "rx-drop", "tx-drop")
_STATE_LOCK = threading.Lock()
_INTERFACE_STATE: dict[str, dict[str, dict[str, int]]] = {}


def _setting(target: dict[str, Any], key: str, default: str = "") -> str:
    value = target.get(key)
    if value is None:
        value = os.environ.get(key.upper(), default)
    return str(value).strip()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None else 0


def _api_root(target: dict[str, Any]) -> str:
    base = _setting(target, "mikrotik_rest_url").rstrip("/")
    if not base:
        raise ValueError("RouterOS REST URL is not configured")
    parts = parse.urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("RouterOS REST URL must be a complete HTTP or HTTPS address")
    return base if base.endswith("/rest") else base + "/rest"


def _get(target: dict[str, Any], path: str, fields: str = "") -> list[dict[str, Any]]:
    username = _setting(target, "mikrotik_rest_username")
    password = _setting(target, "mikrotik_rest_password")
    if not username or not password:
        raise ValueError("RouterOS read-only credentials are not configured")
    query = parse.urlencode({".proplist": fields}, safe=",") if fields else ""
    url = _api_root(target) + path + (("?" + query) if query else "")
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    req = request.Request(url, method="GET", headers={
        "Accept": "application/json",
        "Authorization": f"Basic {encoded}",
        "User-Agent": "tiinyengineer/0.1",
    })
    with request.urlopen(req, timeout=float(target.get("timeout", 8))) as response:
        value = json.loads(response.read().decode("utf-8"))
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"RouterOS returned invalid data for {path}")
    return value


def _temperature(health_rows: list[dict[str, Any]]) -> tuple[float | None, dict[str, float]]:
    readings: dict[str, float] = {}
    for row in health_rows:
        if row.get("name"):
            value = _number(row.get("value"))
            if value is not None and "temperature" in str(row["name"]).lower():
                readings[str(row["name"])] = value
        for key, raw in row.items():
            value = _number(raw)
            if value is not None and "temperature" in str(key).lower():
                readings[str(key)] = value
    return (max(readings.values()) if readings else None), readings


def _pool_size(ranges: str) -> int:
    total = 0
    for item in ranges.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            first, last = (ipaddress.ip_address(value.strip()) for value in item.split("-", 1))
            if first.version != last.version or int(last) < int(first):
                raise ValueError("invalid DHCP pool range")
            total += int(last) - int(first) + 1
        else:
            total += 1
    return total


def _dhcp_use(pools: list[dict[str, Any]], leases: list[dict[str, Any]]) -> tuple[int, int, float | None]:
    capacity = sum(_pool_size(str(row.get("ranges", ""))) for row in pools if row.get("ranges"))
    active = sum(
        1 for row in leases
        if str(row.get("status", "")).lower() in {"bound", "offered", "waiting"}
        and str(row.get("disabled", "false")).lower() != "true"
    )
    return active, capacity, (active * 100.0 / capacity if capacity else None)


def _interface_counters(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return {
        str(row["name"]): {
            "link-downs": _integer(row.get("link-downs")),
            **{field: _integer(row.get(field)) for field in _COUNTER_FIELDS},
        }
        for row in rows if row.get("name")
    }


def _interface_changes(target: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    current = _interface_counters(rows)
    configured = target.get("previous_interfaces")
    key = str(target.get("device_id") or _setting(target, "mikrotik_rest_url"))
    with _STATE_LOCK:
        previous = configured if isinstance(configured, dict) else _INTERFACE_STATE.get(key, {})
        _INTERFACE_STATE[key] = current
    flaps: list[str] = []
    errors: list[str] = []
    for name, counters in current.items():
        old = previous.get(name, {}) if isinstance(previous, dict) else {}
        if old and counters["link-downs"] > _integer(old.get("link-downs")):
            flaps.append(name)
        delta = sum(max(0, counters[field] - _integer(old.get(field))) for field in _COUNTER_FIELDS)
        if old and delta:
            errors.append(f"{name} +{delta}")
    return flaps, errors


def check_mikrotik(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Poll every required read-only endpoint and evaluate operational thresholds."""
    try:
        resource = (_get(target, RESOURCE_PATH) or [{}])[0]
        health_rows = _get(target, HEALTH_PATH)
        interfaces = _get(target, INTERFACE_PATH, INTERFACE_FIELDS)
        leases = _get(target, LEASE_PATH)
        pools = _get(target, POOL_PATH)
        logs = _get(target, LOG_PATH, ".id,time,topics,message")
        cpu = _number(resource.get("cpu-load"))
        temperature, _readings = _temperature(health_rows)
        active, capacity, dhcp_percent = _dhcp_use(pools, leases)
        flaps, interface_errors = _interface_changes(target, interfaces)
    except (ValueError, OSError, TimeoutError, error.URLError, error.HTTPError,
            json.JSONDecodeError) as exc:
        return False, "RouterOS REST poll failed", str(exc)

    up = sum(1 for row in interfaces if str(row.get("running", "false")).lower() == "true")
    error_total = sum(_integer(row.get(field)) for row in interfaces for field in _COUNTER_FIELDS)
    link_down_total = sum(_integer(row.get("link-downs")) for row in interfaces)
    latest_log = str(logs[-1].get("message", ""))[:120] if logs else "none"
    status_parts = [
        f"CPU {cpu:.0f}%" if cpu is not None else "CPU unavailable",
        f"temperature {temperature:.0f} C" if temperature is not None else "temperature unavailable",
        f"DHCP {dhcp_percent:.0f}% ({active}/{capacity})" if dhcp_percent is not None else f"DHCP {active} leases",
        f"{up}/{len(interfaces)} interfaces up",
        f"interface errors {error_total}",
        f"link downs {link_down_total}",
        f"{len(logs)} log lines; latest log: {latest_log}",
    ]
    problems: list[str] = []
    try:
        cpu_limit = float(_setting(target, "mikrotik_cpu_threshold", "85"))
        temperature_limit = float(_setting(target, "mikrotik_temperature_threshold", "75"))
        dhcp_limit = float(_setting(target, "mikrotik_dhcp_threshold", "90"))
    except ValueError as exc:
        return False, "; ".join(status_parts), f"RouterOS threshold is not a number: {exc}"
    if cpu is not None and cpu >= cpu_limit:
        problems.append(f"CPU is {cpu:.0f}% (warning at {cpu_limit:.0f}%)")
    if temperature is not None and temperature >= temperature_limit:
        problems.append(f"temperature is {temperature:.0f} C (warning at {temperature_limit:.0f} C)")
    if dhcp_percent is not None and dhcp_percent >= dhcp_limit:
        problems.append(f"DHCP pool is {dhcp_percent:.0f}% used (warning at {dhcp_limit:.0f}%)")
    if flaps:
        problems.append("port flap: " + ", ".join(sorted(flaps)))
    if interface_errors:
        problems.append("new interface errors: " + ", ".join(sorted(interface_errors)))
    return not problems, "; ".join(status_parts), "; ".join(problems) or None


KINDS = {"mikrotik": check_mikrotik}
