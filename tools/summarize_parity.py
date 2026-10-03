#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path


def summarize(path: Path, since: str, hours: int) -> tuple[str, int]:
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    end = start + timedelta(hours=hours)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        at = datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
        if start <= at <= end:
            rows.append(row)
    if not rows or datetime.fromisoformat(rows[-1]["at"]) < end:
        raise ValueError(f"the {hours}-hour window is not complete")
    names = {item["check_id"] for row in rows for item in row["checks"]}
    if len(names) != 10 or any(len(row["checks"]) != 10 for row in rows):
        raise ValueError("the comparison does not contain exactly 10 shared checks")
    differences = [(row["at"], item) for row in rows for item in row["checks"] if not item["agree"]]
    lines = ["# Canary shadow comparison", "", f"Window: {since} through {end.isoformat(timespec='seconds')}",
             f"Samples: {len(rows)}", "Shared checks: 10", f"Differences: {len(differences)}", ""]
    if differences:
        lines += ["## Differences", ""]
        for at, item in differences:
            lines.append(f"- {at}: {item['check_id']}, Canary {item['canary']}, TiinyEngineer {item['tiinyengineer']}")
        return "\n".join(lines) + "\n", 1
    lines += ["Result: all 10 shared checks agreed for the complete window.", ""]
    return "\n".join(lines), 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report, status = summarize(args.samples, args.since, args.hours)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Parity report not written: {exc}")
        return 2
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote {args.output}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
