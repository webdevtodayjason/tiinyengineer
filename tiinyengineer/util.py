from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def data_dir() -> Path:
    configured = os.environ.get("FARM_DATA_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".tiinyengineer"


def json_text(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def load_env(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
