from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .store import Store
from .util import json_text, utc_now


def compare(store: Store, canary_state_path: Path) -> dict[str, Any] | None:
    try:
        canary = json.loads(canary_state_path.read_text(encoding="utf-8")).get("checks", {})
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    shared = [check for check in store.checks() if check["kind"] != "beat"]
    comparisons = []
    for check in shared:
        old = canary.get(check["id"], {})
        new = store.state_for(check["id"])
        comparisons.append({"check_id": check["id"], "canary": old.get("status", "UNKNOWN"),
                            "tiinyengineer": new.get("status", "UNKNOWN"),
                            "agree": old.get("status", "UNKNOWN") == new.get("status", "UNKNOWN")})
    return {"at": utc_now(), "all_agree": all(row["agree"] for row in comparisons), "checks": comparisons}


def record(store: Store, canary_state_path: Path, output: Path) -> None:
    result = compare(store, canary_state_path)
    if result is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json_text(result) + "\n")
