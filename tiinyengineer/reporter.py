from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any

from .signals import SignalRouter, post_json
from .util import parse_time


class Reporter:
    def __init__(self, endpoint: str, token: str, enabled: bool, logger: logging.Logger, store: Any | None = None):
        self.endpoint = endpoint
        self.token = token
        self.enabled = enabled
        self.logger = logger
        self.router = SignalRouter(store, endpoint, token, logger) if store is not None else None
        self.next_tick = 0.0

    @staticmethod
    def alert_text(check: dict[str, Any], state: dict[str, Any], incident: dict[str, Any] | None) -> str:
        where = f" ({check['where_text']})" if check.get("where_text") else ""
        if state["transition"] == "UP" and incident:
            elapsed = max(0, round((parse_time(state["at"]) - parse_time(incident["opened_at"])).total_seconds() / 60))
            line1 = f"UP again after {elapsed} min: {check['display_name']}{where}"
            line2 = f"Recovered at {state['at']}; confirmed by a successful check"
        else:
            line1 = f"DOWN: {check['display_name']}{where}"
            line2 = f"Since {state['at']}; {state['failures']} checks in a row failed"
        line3 = check.get("meaning") or "The service is not answering as expected."
        line4 = f"Do this first: {check.get('first_step') or 'Check the service.'} Owner: {check.get('owner') or 'unassigned'}"
        links = ["Alerts view row"]
        if check.get("console_url"):
            links.append(str(check["console_url"]))
        return "\n".join((line1, line2, line3, line4, " | ".join(links)))

    def _payload(self, check: dict[str, Any], result: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
        payload = {
            "check_id": f"canary-{check['id']}", "project": "tiinyapp-farm", "name": check["display_name"],
            "url": self._target_url(check), "ok": state["status"] != "DOWN", "status": text,
            "latency_ms": result.get("latency_ms", 0), "owner": check.get("owner", "tiinyapp-farm lead"),
            "at": state["at"], "where": check.get("where_text", ""), "meaning": check.get("meaning", ""),
            "first_step": check.get("first_step", ""), "console_url": check.get("console_url", ""),
            "evidence": f"{state['failures']} checks in a row failed" if state["failures"] else "answering on this check",
            "incident": f"host:{check['id']}",
        }
        if result.get("error"):
            payload["error"] = result["error"]
        return payload

    def report(self, check: dict[str, Any], result: dict[str, Any], state: dict[str, Any], incident: dict[str, Any] | None) -> bool:
        if not self.enabled:
            return False
        text = self.alert_text(check, state, incident)
        payload = self._payload(check, result, state, text)
        if self.router is not None:
            results = self.router.route(state.get("incident_id") or f"host:{check['id']}",
                                        check.get("severity", "warning"), text, payload)
            return results.get("alerts", False)
        ok, _detail = post_json(self.endpoint, payload, headers={"Authorization": f"Bearer {self.token}"})
        return ok

    def tick(self) -> None:
        now = time.monotonic()
        if self.enabled and self.router is not None and now >= self.next_tick:
            self.router.tick(datetime.now(timezone.utc))
            self.next_tick = now + 60

    @staticmethod
    def _target_url(check: dict[str, Any]) -> str:
        target = check["target"]
        if check["kind"] == "http":
            return target["url"]
        if check["kind"] == "tcp":
            return f"lan://{target['host']}:{target['port']}"
        if check["kind"] == "beat":
            return f"job://{check['id']}"
        return f"lan://{target['host']}"

    def test(self, check: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        state = {"transition": "DOWN", "status": "DOWN",
                 "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "failures": 1, "incident_id": f"test:{check['id']}"}
        test_check = dict(check)
        test_check["display_name"] = f"TEST {check['display_name']}"
        test_check["meaning"] = "nothing is down"
        text = self.alert_text(test_check, state, None)
        text = "TEST: " + text.split(": ", 1)[-1]
        payload = self._payload(test_check, {"latency_ms": 0}, state, text)
        if self.router is not None:
            paths = self.router.probe()
            if not paths.tailnet:
                return False
            ok, _detail = self.router.send_alerts(payload)
            return ok
        ok, _detail = post_json(self.endpoint, payload, headers={"Authorization": f"Bearer {self.token}"})
        return ok
