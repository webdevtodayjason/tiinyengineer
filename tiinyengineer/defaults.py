from __future__ import annotations

from typing import Any


FARM_HEALTH_CHECK: dict[str, Any] = {
    "id": "tiinyapp-farm-health",
    "kind": "http",
    "target": {"url": "https://tiinyapp.farm/api/health", "expected_status": 200, "timeout": 5},
    "display_name": "Tiiny app farm health",
    "where_text": "On the internet",
    "meaning": "The app catalog or its storage may be unavailable.",
    "first_step": "Check the farm health page and try again.",
    "owner": "app owner",
    "console_url": "https://tiinyapp.farm",
    "period_s": 60,
    "grace_s": 90,
    "misses_before_real": 3,
    "severity": "warning",
    "alert": True,
    "failure_class": "http_unhealthy",
    "playbook": "Check the service health page and its latest status.",
}


def generic_checks(devices: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build portable first-run checks from locally discovered Tiiny devices."""
    checks = [dict(FARM_HEALTH_CHECK)]
    for index, device in enumerate(devices or [], start=1):
        address = str(device.get("address", "")).strip()
        if not address:
            continue
        name = str(device.get("name") or device.get("model") or "My Tiiny").strip()
        suffix = "" if index == 1 else f" {index}"
        checks.append({
            "id": f"my-tiiny-{index}",
            "kind": "http",
            "target": {"url": f"http://{address}:39218/device.json", "expected_status": 200, "timeout": 5},
            "display_name": f"{name}{suffix}",
            "where_text": "On the local network",
            "meaning": "Your Tiiny stopped answering on its saved network address.",
            "first_step": "Check that the Tiiny is powered and connected, then run device discovery again.",
            "owner": "app owner",
            "console_url": "",
            "period_s": 60,
            "grace_s": 90,
            "misses_before_real": 3,
            "severity": "warning",
            "alert": True,
            "failure_class": "host_unreachable",
            "playbook": "Check power and network state, then confirm the current discovered address.",
        })
    return checks
