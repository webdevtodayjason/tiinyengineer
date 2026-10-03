from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import socket
from typing import Any, Callable
from urllib import error, parse, request
from zoneinfo import ZoneInfo


USER_AGENT = "tiinyengineer/0.1"
CENTRAL = ZoneInfo("America/Chicago")

SIGNAL_SETTINGS = [
    {"key": "alerts_enabled", "label": "Alerts view enabled", "secret": False,
     "help": "Enter true to send state changes to an Alerts view endpoint."},
    {"key": "alerts_view_endpoint", "label": "Alerts view endpoint", "secret": False,
     "help": "Optional HTTPS endpoint that accepts alert reports."},
    {"key": "alerts_view_token", "label": "Alerts view token", "secret": True,
     "help": "Optional bearer token for the Alerts view endpoint."},
    {"key": "telegram_bot_token", "label": "Telegram bot token", "secret": True,
     "help": "Token for TiinyEngineer's alert bot."},
    {"key": "telegram_chat_id", "label": "Telegram chat id", "secret": False,
     "help": "Chat that receives internet-only alerts and weekly tests."},
    {"key": "watchtower_beat_endpoint", "label": "Off-site heartbeat endpoint", "secret": False,
     "help": "Optional Watchtower-style heartbeat URL, without its token."},
    {"key": "watchtower_beat_token", "label": "Off-site heartbeat token", "secret": True,
     "help": "Optional token appended to the off-site heartbeat endpoint."},
    {"key": "lan_probe_host", "label": "Local network probe host", "secret": False,
     "help": "Optional local gateway or always-on host used to distinguish network failures."},
]


@dataclass(frozen=True)
class PathState:
    tailnet: bool
    internet: bool
    lan: bool


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (TimeoutError, OSError):
        return False


def endpoint_probe(url: str, timeout: float = 3.0) -> bool:
    """Return whether an HTTP endpoint is reachable, regardless of authorization."""
    req = request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with request.urlopen(req, timeout=timeout):
            return True
    except error.HTTPError:
        return True
    except (error.URLError, TimeoutError, OSError):
        return False


def probe_paths(alerts_endpoint: str, lan_host: str = "",
                http_probe: Callable[[str], bool] = endpoint_probe,
                connect_probe: Callable[[str, int], bool] = tcp_probe) -> PathState:
    parsed = parse.urlparse(alerts_endpoint)
    alerts_host = parsed.hostname or ""
    alerts_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tailnet = bool(alerts_host) and connect_probe(alerts_host, alerts_port) and http_probe(alerts_endpoint)
    telegram = connect_probe("api.telegram.org", 443)
    resolver = connect_probe("1.1.1.1", 53)
    return PathState(tailnet=tailnet, internet=telegram and resolver,
                     lan=bool(lan_host) and connect_probe(lan_host, 80))


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None,
              timeout: float = 5.0) -> tuple[bool, str]:
    all_headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    all_headers.update(headers or {})
    req = request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=all_headers)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            response.read(300)
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (error.URLError, TimeoutError, OSError) as exc:
        return False, type(exc).__name__


def send_telegram(token: str, chat_id: str, text: str,
                  sender: Callable[..., tuple[bool, str]] = post_json) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "not configured"
    return sender(f"https://api.telegram.org/bot{token}/sendMessage",
                  {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def send_watchtower(endpoint: str, token: str) -> tuple[bool, str]:
    if not endpoint or not token:
        return False, "not configured"
    url = f"{endpoint.rstrip('/')}/{parse.quote(token, safe='')}"
    req = request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with request.urlopen(req, timeout=5) as response:
            body = response.read(100).decode("utf-8", "replace").strip()
            return response.status == 200 and body == "ok", f"HTTP {response.status} {body}"
    except error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (error.URLError, TimeoutError, OSError) as exc:
        return False, type(exc).__name__


def monday_test_due(now: datetime, last_test: str) -> bool:
    local = now.astimezone(CENTRAL)
    return local.weekday() == 0 and local.hour >= 10 and last_test != local.date().isoformat()


def test_text(path: str, now: datetime) -> str:
    when = now.astimezone(CENTRAL).isoformat(timespec="minutes")
    return "\n".join((
        f"TEST: TiinyEngineer {path} signal path",
        f"Path test at {when}; this channel answered",
        "nothing is down",
        "Do this first: No action. Owner: tiinyapp-farm lead",
        "Alerts view row",
    ))


class SignalRouter:
    def __init__(self, store: Any, alerts_endpoint: str, alerts_token: str, logger: logging.Logger,
                 path_probe: Callable[..., PathState] = probe_paths,
                 json_sender: Callable[..., tuple[bool, str]] = post_json,
                 telegram_sender: Callable[..., tuple[bool, str]] = send_telegram,
                 watchtower_sender: Callable[..., tuple[bool, str]] = send_watchtower):
        self.store = store
        self.alerts_endpoint = alerts_endpoint
        self.alerts_token = alerts_token
        self.logger = logger
        self.path_probe = path_probe
        self.json_sender = json_sender
        self.telegram_sender = telegram_sender
        self.watchtower_sender = watchtower_sender
        self.paths = PathState(False, False, False)

    def setting(self, key: str, default: str = "") -> str:
        env_keys = {
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_chat_id": "TELEGRAM_CHAT_ID",
            "watchtower_beat_endpoint": "WATCHTOWER_BEAT_ENDPOINT",
            "watchtower_beat_token": "WATCHTOWER_BEAT_TOKEN",
            "lan_probe_host": "LAN_PROBE_HOST",
        }
        fallback = os.environ.get(env_keys.get(key, ""), default)
        return self.store.setting(key, fallback)

    def probe(self) -> PathState:
        self.paths = self.path_probe(self.alerts_endpoint, self.setting("lan_probe_host"))
        return self.paths

    def send_alerts(self, payload: dict[str, Any]) -> tuple[bool, str]:
        headers = {"Authorization": f"Bearer {self.alerts_token}"}
        return self.json_sender(self.alerts_endpoint, payload, headers=headers)

    def route(self, incident_id: str, severity: str, text: str,
              alerts_payload: dict[str, Any]) -> dict[str, bool]:
        paths = self.probe()
        results: dict[str, bool] = {}
        if paths.tailnet:
            ok, detail = self.send_alerts(alerts_payload)
            results["alerts"] = ok
            self.store.record_signal(incident_id, "alerts", ok, detail)
        if paths.internet:
            ok, detail = self.telegram_sender(self.setting("telegram_bot_token"),
                                               self.setting("telegram_chat_id"), text)
            results["telegram"] = ok
            self.store.record_signal(incident_id, "telegram", ok, detail)
        if severity == "critical":
            self.logger.info("critical signal attempted on all available paths: %s", ",".join(results) or "none")
        return results

    def tick(self, now: datetime) -> None:
        paths = self.probe()
        if paths.internet:
            ok, detail = self.watchtower_sender(self.setting("watchtower_beat_endpoint"),
                                                 self.setting("watchtower_beat_token"))
            self.store.record_signal(None, "watchtower", ok, detail)
        local_date = now.astimezone(CENTRAL).date().isoformat()
        tested = False
        if paths.tailnet and monday_test_due(now, self.setting("last_signal_path_test_alerts")):
            payload = {"check_id": "tiinyengineer-path-test", "project": "tiinyapp-farm",
                       "name": "TiinyEngineer Alerts path test", "ok": True,
                       "status": test_text("Alerts view", now), "incident": "path-test:alerts"}
            ok, detail = self.send_alerts(payload)
            self._record_path_test("alerts", ok, detail)
            self.store.set_setting("last_signal_path_test_alerts", local_date)
            tested = True
        if paths.internet and monday_test_due(now, self.setting("last_signal_path_test_telegram")):
            ok, detail = self.telegram_sender(self.setting("telegram_bot_token"),
                                               self.setting("telegram_chat_id"), test_text("Telegram", now))
            self._record_path_test("telegram", ok, detail)
            self.store.set_setting("last_signal_path_test_telegram", local_date)
            tested = True
        if tested:
            self.logger.info("weekly signal path test attempted")

    def _record_path_test(self, path: str, ok: bool, detail: str) -> None:
        if hasattr(self.store, "record_path_test"):
            self.store.record_path_test(path, ok, detail)
        else:
            self.store.record_signal(None, path, ok, f"path test: {detail}")
