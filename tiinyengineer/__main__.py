from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import tempfile

from .checks import discover_tiinys
from .reporter import Reporter
from .server import Engine, serve
from .store import Store
from .syslog_listener import SyslogListener
from .util import data_dir, load_env


def selfcheck() -> int:
    with tempfile.TemporaryDirectory(prefix="te-selfcheck-") as directory:
        store = Store(Path(directory) / "selfcheck.db")
        store.seed([{"address": "10.0.0.20", "name": "Example Tiiny"}])
        if len(store.checks()) != 2:
            return 1
        store.record_result("tiinyapp-farm-health", "poll", True, {"status": "selfcheck", "latency_ms": 0})
        store.export_ledger(Path(directory) / "ledger.jsonl")
        store.close()
    print("TiinyEngineer selfcheck: ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TiinyEngineer operations watcher")
    parser.add_argument("--host", default=os.environ.get("TIINYENGINEER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TIINYENGINEER_PORT", "8787")))
    parser.add_argument("--once", action="store_true", help="run one scheduler cycle and exit")
    parser.add_argument("--selfcheck", action="store_true", help="validate the package without network access")
    parser.add_argument("--env", type=Path)
    args = parser.parse_args()
    if args.selfcheck:
        return selfcheck()
    root = data_dir()
    if args.env:
        load_env(args.env)
    else:
        load_env(root / ".env")
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("tiinyengineer")
    store = Store(root / "tiinyengineer.db")
    store.seed(discover_tiinys())
    enabled_value = store.setting("alerts_enabled", os.environ.get("ALERTS_VIEW_ENABLED", "false"))
    enabled = enabled_value.lower() in {"1", "true", "yes"}
    token = store.setting("alerts_view_token", os.environ.get("ALERTS_VIEW_TOKEN", ""))
    if enabled and not token:
        logger.error("Alerts reporting is enabled but its token is missing")
        return 2
    endpoint = store.setting("alerts_view_endpoint", os.environ.get("ALERTS_VIEW_ENDPOINT", ""))
    if enabled and not endpoint:
        logger.error("Alerts reporting is enabled but its endpoint is missing")
        return 2
    reporter = Reporter(endpoint, token, enabled, logger, store)
    if args.once:
        Engine(store, reporter, root, logger).cycle()
        store.export_ledger(root / "ledger.jsonl")
        return 0
    syslog_host = store.setting("mikrotik_syslog_host", "0.0.0.0")
    syslog_port = int(store.setting("mikrotik_syslog_port", "5514"))
    try:
        listener = SyslogListener(store, syslog_host, syslog_port, logger)
        listener.start()
    except (OSError, ValueError) as exc:
        logger.error("RouterOS syslog listener could not start: %s", exc)
        listener = None
    try:
        serve(store, reporter, root, args.host, args.port, logger)
    finally:
        if listener:
            listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
