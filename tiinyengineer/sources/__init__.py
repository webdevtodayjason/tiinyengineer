"""Extra check sources, one module each, registered in one place.

A source module in this package exports:
  KINDS:    {"<kind>": fn(target: dict) -> (ok: bool, status: str, error: str | None)}
  SETTINGS: [{"key": str, "label": str, "secret": bool, "help": str}]  (rendered by the settings page)

Adding a module needs its name on the import line and in ``_MODULES`` below.
"""
from __future__ import annotations

from typing import Any, Callable

from . import coolify_backups, mikrotik, tailscale


_MODULES = (coolify_backups, mikrotik, tailscale)


def kinds() -> dict[str, Callable[[dict[str, Any]], tuple[bool, str, str | None]]]:
    found: dict[str, Callable[[dict[str, Any]], tuple[bool, str, str | None]]] = {}
    for module in _MODULES:
        found.update(getattr(module, "KINDS", {}))
    return found


def settings() -> list[dict[str, Any]]:
    return [dict(item, source=module.__name__.rsplit(".", 1)[1]) for module in _MODULES for item in getattr(module, "SETTINGS", [])]
