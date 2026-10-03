"""UDP syslog intake that appends RouterOS lines to the incident ledger."""
from __future__ import annotations

from contextlib import closing
import logging
import socket
import threading
from typing import Any

from .store import Store
from .util import json_text, utc_now


class SyslogListener:
    def __init__(self, store: Store, host: str = "0.0.0.0", port: int = 5514,
                 logger: logging.Logger | None = None):
        self.store = store
        self.logger = logger or logging.getLogger("tiinyengineer.syslog")
        self.stop_event = threading.Event()
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(0.25)
        self.socket.bind((host, port))
        self.address = self.socket.getsockname()
        self.thread: threading.Thread | None = None

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self.run, name="syslog-listener", daemon=True)
        self.thread.start()
        return self.thread

    def run(self) -> None:
        self.logger.info("RouterOS syslog listener ready on UDP %s", self.address[1])
        while not self.stop_event.is_set():
            try:
                packet, peer = self.socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                self.logger.exception("RouterOS syslog listener failed")
                break
            text = packet.decode("utf-8", "replace")
            for line in text.splitlines() or [text]:
                if line:
                    self.record(line[:8192], peer)

    def record(self, line: str, peer: tuple[Any, ...]) -> None:
        body = {"source": "syslog", "sender": str(peer[0]), "message": line}
        with closing(self.store._connect()) as db:
            db.execute(
                "INSERT INTO ledger(at,incident_id,kind,actor,body) VALUES(?,NULL,'observed','syslog',?)",
                (utc_now(), json_text(body)),
            )

    def close(self) -> None:
        self.stop_event.set()
        self.socket.close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
