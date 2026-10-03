from __future__ import annotations

import errno
import json
import socket
import time
from typing import Any, Callable
from urllib import error, request

from . import sources
from .util import utc_now


def check_http(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    expected = int(target.get("expected_status", 200))
    req = request.Request(target["url"], method="GET", headers={"User-Agent": "tiinyengineer/0.1"})
    try:
        with request.urlopen(req, timeout=float(target.get("timeout", 5))) as response:
            status = response.status
        return status == expected, f"HTTP {status}", None if status == expected else f"expected HTTP {expected}"
    except error.HTTPError as exc:
        return False, f"HTTP {exc.code}", f"expected HTTP {expected}"
    except (error.URLError, TimeoutError, OSError) as exc:
        return False, "unreachable", str(exc)


def check_tcp(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    try:
        with socket.create_connection((target["host"], int(target["port"])), timeout=float(target.get("timeout", 5))):
            return True, "connected", None
    except (TimeoutError, OSError) as exc:
        return False, "connection failed", str(exc)


def arp_has(host: str, path: str = "/proc/net/arp") -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()[1:]
    except (FileNotFoundError, OSError):
        return False
    for line in lines:
        fields = line.split()
        if len(fields) >= 6 and fields[0] == host and fields[2] != "0x0":
            return True
    return False


def check_ping(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    host = target["host"]
    if arp_has(host, target.get("arp_path", "/proc/net/arp")):
        return True, "present in the local address table", None
    errors = []
    timeout = float(target.get("timeout", 5))
    ports = target.get("ports", [443, 80, 22])
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(max(0.1, timeout / max(1, len(ports))))
        try:
            result = sock.connect_ex((host, int(port)))
            if result in (0, errno.ECONNREFUSED):
                return True, f"host answered on TCP {port}", None
            errors.append(f"{port}: {errno.errorcode.get(result, result)}")
        except (OSError, TimeoutError) as exc:
            errors.append(f"{port}: {type(exc).__name__}")
        finally:
            sock.close()
    return False, "unreachable", "; ".join(errors)[-300:]


RUNNERS: dict[str, Callable[[dict[str, Any]], tuple[bool, str, str | None]]] = {
    "http": check_http,
    "tcp": check_tcp,
    "ping": check_ping,
}
RUNNERS.update(sources.kinds())


def run_check(check: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    ok, status, problem = RUNNERS[check["kind"]](check["target"])
    return {
        "check": check,
        "result": {
            "ok": ok, "status": status, "latency_ms": round((time.monotonic() - started) * 1000),
            "error": problem, "at": utc_now(),
        },
    }


def discover_tiinys(timeout: float = 0.35) -> list[dict[str, Any]]:
    """Find devices through the documented UDP discovery response."""
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.bind(("", 0))
        sock.sendto(b"GADGET_DISCOVER_V1", ("255.255.255.255", 39217))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                body, peer = sock.recvfrom(65535)
                value = json.loads(body.decode("utf-8"))
                if isinstance(value, dict):
                    value["address"] = peer[0]
                    found[peer[0]] = value
            except socket.timeout:
                break
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                continue
    except OSError:
        return []
    finally:
        sock.close()
    return list(found.values())
