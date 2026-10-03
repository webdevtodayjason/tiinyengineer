from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib.resources
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from urllib import error
from urllib.parse import parse_qs, urlparse

from . import sources
from .checks import RUNNERS, discover_tiinys, run_check
from .parity import record as record_parity
from .reporter import Reporter
from .store import Store, judge
from .sources import containers
from .util import h, parse_time, utc_now


MAX_BODY = 256 * 1024
CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}
RUNNERS.update(containers.KINDS)


def setting_definitions() -> list[dict[str, Any]]:
    found = sources.settings()
    if not any(item.get("source") == "containers" for item in found):
        found.extend(dict(item, source="containers") for item in containers.SETTINGS)
    unique = []
    seen = set()
    for item in found:
        if item["key"] not in seen:
            unique.append(item)
            seen.add(item["key"])
    try:
        from .signals import SIGNAL_SETTINGS
    except ImportError:
        return unique
    return unique + [dict(item, source="signals") for item in SIGNAL_SETTINGS]


class Engine:
    def __init__(self, store: Store, reporter: Reporter, data_path: Path, logger: logging.Logger):
        self.store = store
        self.reporter = reporter
        self.data_path = data_path
        self.logger = logger
        self.stop_event = threading.Event()
        self.next_container_discovery = 0.0

    def cycle(self) -> None:
        self.reporter.tick()
        self._discover_containers()
        observed = [run_check(self._configured_check(check)) for check in self.store.due_checks()]
        for item in judge(observed):
            check, result = item["check"], item["result"]
            state = self.store.record_result(check["id"], "poll", result["ok"], result, result["at"])
            if state["transition"] and check["alert"] and self.reporter.enabled:
                incident = self.store.incident(state["incident_id"]) if state["incident_id"] else None
                sent = self.reporter.report(check, result, state, incident)
                if incident:
                    self._record_signal(incident["id"], sent, "state transition")
        for check in self.store.find_missed_beats():
            result = {"status": "missed its expected run", "latency_ms": 0, "error": "no beat within period plus grace"}
            state = self.store.record_result(check["id"], "beat", False, result)
            if state["transition"] and check["alert"] and self.reporter.enabled:
                incident = self.store.incident(state["incident_id"])
                sent = self.reporter.report(check, result, state, incident)
                self._record_signal(incident["id"], sent, "missed beat")

    def _configured_check(self, check: dict[str, Any]) -> dict[str, Any]:
        if check["kind"] != "docker":
            return check
        configured = dict(check)
        target = dict(check["target"])
        for key in ("coolify_base_url", "coolify_api_token", "container_socket_path"):
            value = self.store.setting(key)
            if value:
                target.setdefault("socket_path" if key == "container_socket_path" else key, value)
        if target.get("mode") == "report":
            target.setdefault("reports_dir", str(self.data_path))
        configured["target"] = target
        return configured

    def _discover_containers(self) -> None:
        now = time.monotonic()
        if now < self.next_container_discovery:
            return
        self.next_container_discovery = now + 60
        enabled = self.store.setting("container_local_enabled").casefold() in {"1", "true", "yes"}
        if enabled:
            config = {
                "container_local_name": self.store.setting("container_local_name"),
                "container_socket_path": self.store.setting("container_socket_path") or containers.DEFAULT_SOCKET_PATH,
            }
            try:
                for check in containers.discover_local_checks(config):
                    if not self.store.check(check["id"]):
                        self.store.save_check(check)
            except (OSError, TimeoutError, ValueError, json.JSONDecodeError):
                self.logger.warning("local Docker discovery was unavailable")
        base = self.store.setting("coolify_base_url")
        token = self.store.setting("coolify_api_token")
        host = self.store.setting("coolify_container_host")
        if base and token and host:
            config = {"host": host, "coolify_base_url": base, "coolify_api_token": token,
                      "reports_dir": str(self.data_path)}
            try:
                for check in containers.discover_checks(config):
                    self.store.save_check(check)
            except (error.HTTPError, error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                self.logger.warning("Coolify container discovery was unavailable")

    def _record_signal(self, incident_id: str, sent: bool, detail: str) -> None:
        self.store.record_signal(incident_id, "alerts", sent, detail)

    def run(self) -> None:
        next_export = 0.0
        next_parity = 0.0
        parity_path = os.environ.get("CANARY_STATE_PATH")
        while not self.stop_event.is_set():
            try:
                self.cycle()
                if parity_path and time.monotonic() >= next_parity:
                    record_parity(self.store, Path(parity_path), self.data_path / "canary-parity.jsonl")
                    next_parity = time.monotonic() + 60
                if time.monotonic() >= next_export:
                    self.store.export_ledger(self.data_path / "ledger.jsonl")
                    now = datetime.now(timezone.utc)
                    self.store.rollup_old_events((now - timedelta(days=30)).isoformat(timespec="seconds"),
                                                 (now - timedelta(days=365)).isoformat(timespec="seconds"))
                    next_export = time.monotonic() + 86400
            except Exception:
                self.logger.exception("scheduler cycle failed")
            self.stop_event.wait(1.0)


class App:
    def __init__(self, store: Store, reporter: Reporter):
        self.store = store
        self.reporter = reporter
        self.data_path = Path(store.path).parent if store.path != ":memory:" else Path.cwd()
        self.secret_path = self.data_path / "source-settings.json"

    def handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TiinyEngineer/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                logging.getLogger("tiinyengineer.http").info(fmt, *args)

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                pages = {"/", "/now", "/registry", "/rules", "/reporting", "/assignment", "/timeline", "/history", "/settings"}
                if parsed.path in pages:
                    self._asset("index.html")
                elif parsed.path.startswith("/assets/"):
                    self._asset(parsed.path.removeprefix("/assets/"))
                elif parsed.path == "/api/status":
                    self._json({"ok": True, "version": "0.1.0", "at": utc_now(), "checks": app.checks_feed()})
                elif parsed.path == "/api/checks":
                    self._json({"checks": app.checks_feed()})
                elif parsed.path == "/api/incidents":
                    self._json({"incidents": app.incidents_feed()})
                elif parsed.path == "/api/ledger":
                    self._json({"ledger": app.ledger_feed()})
                elif parsed.path == "/api/history":
                    self._json(app.history_feed())
                elif parsed.path == "/api/settings":
                    self._json({"settings": app.settings_feed()})
                elif parsed.path == "/api/devices/find":
                    self._json({"devices": discover_tiinys()})
                elif parsed.path.startswith("/beat/"):
                    app.handle_beat(self, parsed)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path.startswith("/beat/"):
                    app.handle_beat(self, parsed)
                elif parsed.path == "/api/containers/report":
                    app.handle_container_report(self)
                elif parsed.path in {"/api/checks", "/settings/check"}:
                    app.save_check(self, json_api=parsed.path == "/api/checks")
                elif parsed.path == "/api/settings":
                    app.save_settings(self)
                elif parsed.path.startswith("/settings/token/"):
                    check_id = parsed.path.removeprefix("/settings/token/")
                    if not app.store.check(check_id):
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        token = app.store.rotate_token(check_id)
                        self._html(app.token_page(check_id, token))
                elif parsed.path.startswith("/settings/test-alert/"):
                    check = app.store.check(parsed.path.removeprefix("/settings/test-alert/"))
                    if not check:
                        self.send_error(HTTPStatus.NOT_FOUND)
                    else:
                        ok = app.reporter.test(check)
                        self._json({"ok": ok, "message": "TEST sent to one channel." if ok else "The alert endpoint did not accept the TEST."})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def _headers(self, status: int, content_type: str, length: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'")
                self.end_headers()

            def _json(self, value: Any, status: int = 200) -> None:
                body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                self._headers(status, "application/json", len(body))
                self.wfile.write(body)

            def _html(self, text: str, status: int = 200) -> None:
                body = text.encode("utf-8")
                self._headers(status, "text/html; charset=utf-8", len(body))
                self.wfile.write(body)

            def _asset(self, name: str) -> None:
                if "/" in name or "\\" in name or name.startswith("."):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    body = importlib.resources.files("tiinyengineer.ui").joinpath(name).read_bytes()
                except (FileNotFoundError, ModuleNotFoundError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_type = "text/html; charset=utf-8" if name.endswith(".html") else STATIC_TYPES.get(Path(name).suffix, "application/octet-stream")
                self._headers(HTTPStatus.OK, content_type, len(body))
                self.wfile.write(body)

        return Handler

    def _read_body(self, handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any], bool]:
        length = int(handler.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("request is too large")
        raw = handler.rfile.read(length)
        is_json = handler.headers.get_content_type() == "application/json"
        if is_json:
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError("a JSON object is required")
            return value, True
        form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in form.items()}, False

    def handle_beat(self, handler: BaseHTTPRequestHandler, parsed) -> None:
        check_id = parsed.path.removeprefix("/beat/")
        query = parse_qs(parsed.query)
        auth = handler.headers.get("Authorization", "")
        token = query.get("token", [auth.removeprefix("Bearer ")])[0]
        ok = query.get("ok", ["1"])[0].lower() in {"1", "true", "yes"}
        try:
            duration = int(query["ms"][0]) if "ms" in query else None
        except ValueError:
            handler.send_error(HTTPStatus.BAD_REQUEST, "ms must be a whole number")
            return
        reason = query.get("reason", [""])[0]
        if not self.store.record_beat(check_id, token, ok, duration, reason):
            handler.send_error(HTTPStatus.UNAUTHORIZED)
            return
        body = b"ok\n"
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def handle_container_report(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            report, is_json = self._read_body(handler)
            if not is_json:
                raise ValueError("JSON is required")
            host = containers.safe_host(str(report.get("host", "")))
            configured_hosts = {
                containers.safe_host(item) for item in self.store.setting("container_reporter_hosts").split(",") if item.strip()
            }
            if host not in configured_hosts:
                raise PermissionError("host is not configured")
            raw_tokens = json.loads(self.store.setting("container_reporter_tokens", "{}"))
            if not isinstance(raw_tokens, dict):
                raise ValueError("container reporter tokens must be a JSON object")
            configured_tokens = {containers.safe_host(str(key)): str(value) for key, value in raw_tokens.items()}
            expected = str(configured_tokens.get(host, ""))
            authorization = handler.headers.get("Authorization", "")
            supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                raise PermissionError("reporter token was not accepted")
            containers.save_report(self.data_path, report)
            existing = {check["id"] for check in self.store.checks("docker")}
            for check in containers.checks_from_rows(host, report["containers"], "report", str(self.data_path)):
                if check["id"] not in existing:
                    self.store.save_check(check)
        except PermissionError:
            handler.send_error(HTTPStatus.UNAUTHORIZED)
            return
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            handler._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        handler._json({"ok": True, "host": host}, HTTPStatus.ACCEPTED)

    @staticmethod
    def _place(check: dict[str, Any]) -> str:
        where = check.get("where_text", "").lower()
        if "home office" in where:
            return "home"
        if any(word in where for word in ("rack", "server room")):
            return "rack"
        if "office" in where:
            return "office"
        if any(word in where for word in ("internet", "cloud", "worker", "site")):
            return "cloud"
        return "home"

    def checks_feed(self) -> list[dict[str, Any]]:
        states = {row["id"]: row for row in self.store.latest()}
        result = []
        for check in self.store.checks():
            state = states.get(check["id"], {})
            last = state.get("last_beat_at") if check["kind"] == "beat" else state.get("last_checked_at")
            result.append({**check, "status": state.get("status", "UNKNOWN"), "last_at": last,
                           "incident_id": state.get("open_incident_id"), "place": self._place(check)})
        return result

    def _five_lines(self, incident: dict[str, Any], check: dict[str, Any]) -> list[str]:
        links = ["Alerts view row"]
        if check.get("console_url"):
            links.append("console or KVM")
        first_step = (check.get("first_step") or "check the last known good state").strip()
        if not first_step.endswith((".", "!", "?")):
            first_step += "."
        return [
            f"DOWN: {check['display_name']} ({check.get('where_text') or 'registered location'})",
            f"Since {incident['opened_at']}. Very sure: {check['misses_before_real']} checks missed in a row.",
            check.get("meaning") or "The check stopped answering and needs an owner to look at it.",
            f"Do this first: {first_step} Owner: {check.get('owner') or 'unassigned'}.",
            "Links: " + ", ".join(links),
        ]

    def incidents_feed(self) -> list[dict[str, Any]]:
        checks = {check["id"]: check for check in self.store.checks()}
        result = []
        for incident in self.store.incidents(open_only=True):
            check = checks.get(incident["check_id"])
            if check and check["alert"]:
                result.append({**incident, "check_name": check["display_name"], "five_lines": self._five_lines(incident, check),
                               "console_url": check.get("console_url", "")})
        return result

    def ledger_feed(self) -> list[dict[str, Any]]:
        result = []
        for row in self.store.ledger():
            try:
                body = json.loads(row["body"])
            except (TypeError, json.JSONDecodeError):
                body = {"summary": str(row["body"])}
            result.append({**row, "body": body})
        return result

    def history_feed(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(hours=35)
        hours = [(start + timedelta(hours=i)).isoformat(timespec="seconds") for i in range(36)]
        checks = self.checks_feed()
        by_check = {check["id"]: ["none"] * 36 for check in checks}
        with closing(self.store._connect()) as db:
            rows = db.execute("SELECT check_id,at,ok FROM events WHERE at >= ? ORDER BY at", (start.isoformat(),)).fetchall()
            rolls = db.execute("SELECT check_id,hour,failures FROM event_rollups WHERE hour >= ? ORDER BY hour", (start.isoformat(),)).fetchall()
        for row in rolls:
            when = parse_time(row["hour"]).replace(minute=0, second=0, microsecond=0)
            index = int((when - start).total_seconds() // 3600)
            if row["check_id"] in by_check and 0 <= index < 36:
                by_check[row["check_id"]][index] = "miss" if row["failures"] else "up"
        for row in rows:
            when = parse_time(row["at"]).replace(minute=0, second=0, microsecond=0)
            index = int((when - start).total_seconds() // 3600)
            if row["check_id"] in by_check and 0 <= index < 36:
                old = by_check[row["check_id"]][index]
                by_check[row["check_id"]][index] = "miss" if not row["ok"] else ("up" if old == "none" else old)
        return {"hours": hours, "checks": [{**check, "history": by_check[check["id"]]} for check in checks]}

    def settings_feed(self) -> list[dict[str, Any]]:
        secret_values = self._read_secret_settings()
        result = []
        for item in setting_definitions():
            key = str(item["key"])
            secret = bool(item.get("secret"))
            result.append({**item, "secret": secret, "value": "" if secret else self.store.setting(key),
                           "configured": key in secret_values if secret else bool(self.store.setting(key))})
        return result

    def _read_secret_settings(self) -> dict[str, str]:
        try:
            value = json.loads(self.secret_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def _write_secret_settings(self, value: dict[str, str]) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        temporary = self.secret_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.secret_path)
        os.chmod(self.secret_path, 0o600)

    def save_settings(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            values, _ = self._read_body(handler)
            definitions = {str(item["key"]): item for item in setting_definitions()}
            secret_values = self._read_secret_settings()
            for key, raw in values.items():
                if key not in definitions:
                    raise ValueError(f"unknown setting: {key}")
                value = str(raw)
                if definitions[key].get("secret"):
                    if value:
                        secret_values[key] = value
                        self.store.set_setting(key, value)
                else:
                    self.store.set_setting(key, value)
            self._write_secret_settings(secret_values)
            if self.store.path != ":memory:":
                os.chmod(self.store.path, 0o600)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            handler._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        handler._json({"ok": True, "settings": self.settings_feed()})

    @staticmethod
    def _target_from_text(kind: str, raw: str) -> dict[str, Any]:
        value = raw.strip()
        if kind == "http":
            parsed = urlparse(value if "://" in value else "https://" + value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("enter a complete web address or host name")
            return {"url": parsed.geturl(), "expected_status": 200, "timeout": 5}
        if kind == "tcp":
            host, separator, port = value.rpartition(":")
            if not separator or not host or not port.isdigit():
                raise ValueError("enter a host and port, for example server.example:443")
            return {"host": host, "port": int(port), "timeout": 5}
        if kind == "ping":
            if not value or any(char.isspace() for char in value):
                raise ValueError("enter a host name or address")
            return {"host": value, "timeout": 5}
        if kind == "beat":
            return {"job": value or "scheduled-job"}
        raise ValueError("this check type needs its structured target fields")

    def save_check(self, handler: BaseHTTPRequestHandler, json_api: bool = False) -> None:
        try:
            form, is_json = self._read_body(handler)
            if json_api and not is_json:
                raise ValueError("JSON is required")
            check_id = str(form.get("id", "")).strip().lower()
            kind = str(form.get("kind", "")).strip()
            if not CHECK_ID.fullmatch(check_id):
                raise ValueError("id must use lowercase letters, numbers and hyphens")
            target_value = form.get("target", {})
            if isinstance(target_value, str):
                try:
                    target = json.loads(target_value)
                    if not isinstance(target, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    target = self._target_from_text(kind, target_value)
            elif isinstance(target_value, dict):
                target = target_value
            else:
                raise ValueError("target must be an object or address")
            check = {
                "id": check_id, "kind": kind, "target": target,
                "display_name": str(form.get("display_name") or form.get("name") or check_id),
                "where_text": str(form.get("where_text") or form.get("where") or "Registered from the dashboard"),
                "meaning": str(form.get("meaning") or "This check stopped answering."),
                "first_step": str(form.get("first_step") or "Check the service and its last known good state."),
                "owner": str(form.get("owner") or "unassigned"), "console_url": str(form.get("console_url") or ""),
                "period_s": int(form.get("period_s", 60)), "grace_s": int(form.get("grace_s", 90)),
                "misses_before_real": int(form.get("misses_before_real", 3)),
                "severity": str(form.get("severity", "warning")),
                "alert": bool(form.get("alert", True)) and str(form.get("severity", "warning")) != "presence",
                "failure_class": str(form.get("failure_class") or "configured_check"),
                "playbook": str(form.get("playbook") or form.get("first_step") or "Check the last known good state."),
                "quiet_hours": str(form.get("quiet_hours") or ""), "maintenance": str(form.get("maintenance") or ""),
                "cooldown_s": int(form.get("cooldown_s", 600)),
            }
            if kind not in {*RUNNERS, "beat"}:
                raise ValueError("unsupported kind")
            if check["severity"] not in {"critical", "warning", "info", "presence"}:
                raise ValueError("unsupported severity")
            if min(check["period_s"], check["misses_before_real"]) < 1 or min(check["grace_s"], check["cooldown_s"]) < 0:
                raise ValueError("timing values must be positive")
            self.store.save_check(check)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            if json_api:
                handler._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                handler.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if json_api:
            saved = next(item for item in self.checks_feed() if item["id"] == check["id"])
            handler._json({"ok": True, "check": saved}, HTTPStatus.CREATED)
        else:
            handler.send_response(HTTPStatus.SEE_OTHER)
            handler.send_header("Location", "/settings")
            handler.end_headers()

    @staticmethod
    def _layout(title: str, content: str) -> str:
        return f"<!doctype html><html lang=en><meta charset=utf-8><title>{h(title)}</title><body><h1>{h(title)}</h1>{content}</body></html>"

    def token_page(self, check_id: str, token: str) -> str:
        return self._layout("New beat token", f"<p>Copy this token now. Rotating it stopped the old token.</p><p><code>{h(token)}</code></p><p>Beat URL: <code>/beat/{h(check_id)}?token=TOKEN&amp;ok=1&amp;ms=123</code></p>")


def serve(store: Store, reporter: Reporter, data_path: Path, host: str, port: int, logger: logging.Logger) -> None:
    engine = Engine(store, reporter, data_path, logger)
    scheduler = threading.Thread(target=engine.run, name="scheduler", daemon=True)
    scheduler.start()
    server = ThreadingHTTPServer((host, port), App(store, reporter).handler())
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        engine.stop_event.set()
        server.server_close()
        scheduler.join(timeout=3)
