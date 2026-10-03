"""Tailscale peer checks through the local read-only API socket."""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import socket
from typing import Any


DEFAULT_SOCKET_PATH = "/var/run/tailscale/tailscaled.sock"
SETTINGS = [
    {
        "key": "tailscale_socket_path",
        "label": "Tailscale local socket",
        "secret": False,
        "help": "The default works on a standard Tailscale installation.",
    },
    {
        "key": "tailscale_peer_roles",
        "label": "Tailscale peer roles",
        "secret": False,
        "help": "Mark always-on machines as server and laptops, phones, and travelling devices as presence.",
    },
]


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose transport is a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 5.0):
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self.socket_path)
        self.sock = connection


def _status(socket_path: str, timeout: float) -> dict[str, Any]:
    connection = UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request("GET", "/localapi/v0/status")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"local Tailscale API returned HTTP {response.status}")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("local Tailscale API returned an invalid status document")
        return value
    finally:
        connection.close()


def _names(peer: dict[str, Any]) -> set[str]:
    names = set()
    for key in ("HostName", "DNSName"):
        value = peer.get(key)
        if isinstance(value, str) and value:
            names.add(value.rstrip(".").casefold())
            names.add(value.rstrip(".").split(".", 1)[0].casefold())
    return names


def _find_peer(status: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    wanted_name = _canonical_name(wanted)
    candidates = [status.get("Self", {})]
    peers = status.get("Peer", {})
    if isinstance(peers, dict):
        candidates.extend(peers.values())
    for peer in candidates:
        if isinstance(peer, dict):
            candidate_names = {_canonical_name(name) for name in _names(peer)}
            if wanted_name in candidate_names:
                return peer
    return None


def _canonical_name(value: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in value.casefold()).split())


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_text(value: Any, now: datetime) -> str:
    seen = _parse_time(value)
    if seen is None:
        return "last seen time unavailable"
    seconds = max(0, int((now - seen).total_seconds()))
    if seconds < 120:
        return "last seen just now"
    if seconds < 7200:
        return f"last seen {seconds // 60} min ago"
    if seconds < 172800:
        return f"last seen {seconds // 3600} h ago"
    return f"last seen {seconds // 86400} days ago"


def _expiry_text(value: Any, now: datetime) -> tuple[str, bool]:
    expiry = _parse_time(value)
    if expiry is None:
        return "key expiry unavailable", False
    seconds = (expiry - now).total_seconds()
    if seconds <= 0:
        return "key expired", True
    days = int(seconds // 86400)
    unit = "day" if days == 1 else "days"
    if seconds <= 7 * 86400:
        return f"key expires in {days} {unit}", True
    return f"key expires in {days} {unit}", False


def _role(target: dict[str, Any], peer_name: str) -> str:
    configured = str(target.get("role", "")).casefold()
    if configured in {"server", "presence"}:
        return configured
    return "presence"


def _lan_reachable(target: dict[str, Any]) -> bool | None:
    host = target.get("lan_host")
    if not host:
        return None
    from ..checks import check_ping

    lan_target = {
        "host": host,
        "ports": target.get("lan_ports", [443, 80]),
        "timeout": target.get("lan_timeout", target.get("timeout", 5)),
        "arp_path": target.get("arp_path", "/proc/net/arp"),
    }
    return check_ping(lan_target)[0]


def check_peer(target: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Check one named peer and, when configured, its local network presence."""
    name = str(target["peer"]).strip()
    role = _role(target, name)
    now = target.get("_now")
    if not isinstance(now, datetime):
        now = datetime.now(timezone.utc)
    try:
        status = _status(str(target.get("socket_path", DEFAULT_SOCKET_PATH)), float(target.get("timeout", 5)))
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        if role == "presence":
            return True, f"{name} presence could not be read", str(exc)
        return False, "Tailscale status unavailable", str(exc)

    peer = _find_peer(status, name)
    online = bool(peer and peer.get("Online"))
    seen_text = _age_text(peer.get("LastSeen") if peer else None, now)
    expiry_text, expiry_warning = _expiry_text(peer.get("KeyExpiry") if peer else None, now)

    if role == "presence":
        state = "online" if online else "away"
        return True, f"{name} is {state} (presence only); {seen_text}; {expiry_text}", None
    if online and expiry_warning:
        return False, f"Key warning for {name}: {expiry_text}; online; {seen_text}", expiry_text
    if online:
        return True, f"{name} is online; {seen_text}; {expiry_text}", None

    lan = _lan_reachable(target)
    if lan is True:
        return False, f"tailnet problem on {name}, LAN up; {seen_text}; {expiry_text}", "tailnet connection is down"
    if lan is False:
        return False, f"{name} is down; {seen_text}; {expiry_text}", "tailnet and LAN checks both failed"
    return False, f"{name} is off the tailnet; LAN not configured; {seen_text}; {expiry_text}", "tailnet connection is down"


KINDS = {"tailscale_peer": check_peer}
