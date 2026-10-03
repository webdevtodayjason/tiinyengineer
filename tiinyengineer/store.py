from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sqlite3
from typing import Any, Iterable

from .defaults import generic_checks
from .util import json_text, parse_time, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS checks (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, display_name TEXT NOT NULL,
 where_text TEXT NOT NULL DEFAULT '', meaning TEXT NOT NULL DEFAULT '', first_step TEXT NOT NULL DEFAULT '',
 owner TEXT NOT NULL DEFAULT '', console_url TEXT NOT NULL DEFAULT '', period_s INTEGER NOT NULL,
 grace_s INTEGER NOT NULL, misses_before_real INTEGER NOT NULL DEFAULT 3,
 severity TEXT NOT NULL DEFAULT 'warning', alert INTEGER NOT NULL DEFAULT 1,
 failure_class TEXT NOT NULL DEFAULT '', playbook TEXT NOT NULL DEFAULT '', quiet_hours TEXT NOT NULL DEFAULT '',
 maintenance TEXT NOT NULL DEFAULT '', cooldown_s INTEGER NOT NULL DEFAULT 600,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, check_id TEXT NOT NULL REFERENCES checks(id),
 source TEXT NOT NULL, ok INTEGER NOT NULL, value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_check_at ON events(check_id, at DESC);
CREATE TABLE IF NOT EXISTS event_rollups (
 check_id TEXT NOT NULL REFERENCES checks(id), hour TEXT NOT NULL, observations INTEGER NOT NULL,
 successes INTEGER NOT NULL, failures INTEGER NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(check_id, hour)
);
CREATE TABLE IF NOT EXISTS incidents (
 id TEXT PRIMARY KEY, check_id TEXT NOT NULL REFERENCES checks(id), opened_at TEXT NOT NULL,
 state TEXT NOT NULL, class TEXT NOT NULL, owner TEXT NOT NULL, severity TEXT NOT NULL,
 plan TEXT NOT NULL DEFAULT '', handoff_msg_id TEXT, external_item TEXT, closed_at TEXT, close_reason TEXT
);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents(check_id, state);
CREATE TABLE IF NOT EXISTS verdicts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL REFERENCES incidents(id), at TEXT NOT NULL,
 by_actor TEXT NOT NULL, label TEXT NOT NULL, class TEXT NOT NULL, owner TEXT NOT NULL,
 severity TEXT NOT NULL, confidence REAL NOT NULL, reason TEXT NOT NULL, shadow INTEGER NOT NULL DEFAULT 0,
 judged_right INTEGER
);
CREATE TABLE IF NOT EXISTS ledger (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, incident_id TEXT,
 kind TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TABLE IF NOT EXISTS signals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT, path TEXT NOT NULL, at TEXT NOT NULL,
 ok INTEGER NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS beat_tokens (
 check_id TEXT PRIMARY KEY REFERENCES checks(id), token TEXT NOT NULL UNIQUE, last_beat_at TEXT,
 last_ok INTEGER, duration_ms INTEGER, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS check_state (
 check_id TEXT PRIMARY KEY REFERENCES checks(id), status TEXT NOT NULL DEFAULT 'UNKNOWN',
 consecutive_failures INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT, last_transition_at TEXT,
 open_incident_id TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._keeper = self._connect()
        self._keeper.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def close(self) -> None:
        self._keeper.close()

    def seed(self, devices: list[dict[str, Any]] | None = None) -> bool:
        """Seed a new registry once, without changing any existing installation."""
        now = utc_now()
        with closing(self._connect()) as db:
            if db.execute("SELECT 1 FROM checks LIMIT 1").fetchone():
                return False
            for check in generic_checks(devices):
                self._insert_check(db, check, now)
        return True

    @staticmethod
    def _insert_check(db: sqlite3.Connection, check: dict[str, Any], now: str) -> None:
        db.execute(
            """INSERT OR IGNORE INTO checks
            (id,kind,target,display_name,where_text,meaning,first_step,owner,console_url,period_s,grace_s,
             misses_before_real,severity,alert,failure_class,playbook,quiet_hours,maintenance,cooldown_s,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (check["id"], check["kind"], json_text(check["target"]), check["display_name"],
             check.get("where_text", ""), check.get("meaning", ""), check.get("first_step", ""),
             check.get("owner", ""), check.get("console_url", ""), int(check.get("period_s", 60)),
             int(check.get("grace_s", 90)), int(check.get("misses_before_real", 3)),
             check.get("severity", "warning"), int(check.get("alert", True)), check.get("failure_class", ""),
             check.get("playbook", ""), check.get("quiet_hours", ""), check.get("maintenance", ""),
             int(check.get("cooldown_s", 600)), now, now),
        )
        db.execute("INSERT OR IGNORE INTO check_state(check_id) VALUES(?)", (check["id"],))

    def checks(self, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM checks"
        params: tuple[Any, ...] = ()
        if kind:
            query += " WHERE kind=?"
            params = (kind,)
        query += " ORDER BY id"
        with closing(self._connect()) as db:
            rows = db.execute(query, params).fetchall()
        return [self._check_dict(row) for row in rows]

    def check(self, check_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
        return self._check_dict(row) if row else None

    @staticmethod
    def _check_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["target"] = json.loads(result["target"])
        result["alert"] = bool(result["alert"])
        return result

    def save_check(self, check: dict[str, Any]) -> None:
        now = utc_now()
        with closing(self._connect()) as db:
            existing = db.execute("SELECT created_at FROM checks WHERE id=?", (check["id"],)).fetchone()
            if not existing:
                self._insert_check(db, check, now)
            else:
                values = (
                    check["kind"], json_text(check["target"]), check["display_name"], check.get("where_text", ""),
                    check.get("meaning", ""), check.get("first_step", ""), check.get("owner", ""),
                    check.get("console_url", ""), int(check.get("period_s", 60)), int(check.get("grace_s", 90)),
                    int(check.get("misses_before_real", 3)), check.get("severity", "warning"),
                    int(check.get("alert", True)), check.get("failure_class", ""), check.get("playbook", ""),
                    check.get("quiet_hours", ""), check.get("maintenance", ""), int(check.get("cooldown_s", 600)),
                    now, check["id"],
                )
                db.execute("""UPDATE checks SET kind=?,target=?,display_name=?,where_text=?,meaning=?,first_step=?,owner=?,
                           console_url=?,period_s=?,grace_s=?,misses_before_real=?,severity=?,alert=?,failure_class=?,
                           playbook=?,quiet_hours=?,maintenance=?,cooldown_s=?,updated_at=? WHERE id=?""", values)
            if check["kind"] == "beat":
                db.execute("INSERT OR IGNORE INTO beat_tokens(check_id, token) VALUES(?, ?)", (check["id"], secrets.token_urlsafe(24)))

    def token_for(self, check_id: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT token FROM beat_tokens WHERE check_id=?", (check_id,)).fetchone()
        return row[0] if row else None

    def rotate_token(self, check_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with closing(self._connect()) as db:
            db.execute("UPDATE beat_tokens SET token=? WHERE check_id=?", (token, check_id))
        return token

    def record_beat(self, check_id: str, token: str, ok: bool, duration_ms: int | None, reason: str) -> bool:
        now = utc_now()
        with closing(self._connect()) as db:
            row = db.execute("SELECT token FROM beat_tokens WHERE check_id=?", (check_id,)).fetchone()
            if not row or not secrets.compare_digest(row[0], token):
                return False
            db.execute("UPDATE beat_tokens SET last_beat_at=?,last_ok=?,duration_ms=?,reason=? WHERE check_id=?",
                       (now, int(ok), duration_ms, reason[:300], check_id))
        self.record_result(check_id, "beat", ok, {"status": "ran" if ok else "reported failure", "duration_ms": duration_ms, "error": reason or None}, now)
        return True

    def beat_rows(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT c.*, b.last_beat_at,b.last_ok,b.duration_ms,b.reason,s.status,s.open_incident_id
                               FROM checks c JOIN beat_tokens b ON b.check_id=c.id
                               JOIN check_state s ON s.check_id=c.id ORDER BY c.display_name""").fetchall()
        return [dict(row) for row in rows]

    def record_result(self, check_id: str, source: str, ok: bool, value: dict[str, Any], at: str | None = None) -> dict[str, Any]:
        at = at or utc_now()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            check = db.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
            if not check:
                db.execute("ROLLBACK")
                raise KeyError(check_id)
            state = db.execute("SELECT * FROM check_state WHERE check_id=?", (check_id,)).fetchone()
            failures = 0 if ok else int(state["consecutive_failures"]) + 1
            new_status = "UP" if ok else ("DOWN" if failures >= check["misses_before_real"] else "DEGRADED")
            transition = None
            if new_status == "DOWN" and state["status"] != "DOWN":
                transition = "DOWN"
            elif new_status == "UP" and state["status"] == "DOWN":
                transition = "UP"
            db.execute("INSERT INTO events(at,check_id,source,ok,value) VALUES(?,?,?,?,?)",
                       (at, check_id, source, int(ok), json_text(value)))
            incident_id = state["open_incident_id"]
            if transition == "DOWN":
                incident_id = self._new_incident_id(db, at)
                db.execute("""INSERT INTO incidents(id,check_id,opened_at,state,class,owner,severity,plan)
                           VALUES(?,?,?,'open',?,?,?,?)""",
                           (incident_id, check_id, at, check["failure_class"], check["owner"], check["severity"], check["playbook"]))
                db.execute("""INSERT INTO verdicts(incident_id,at,by_actor,label,class,owner,severity,confidence,reason,shadow)
                           VALUES(?,?,'rule','real',?,?,?,1.0,?,0)""",
                           (incident_id, at, check["failure_class"], check["owner"], check["severity"],
                            f"{failures} consecutive checks failed"))
                self._ledger(db, incident_id, "observed", "engine", {"check_id": check_id, "value": value})
                self._ledger(db, incident_id, "triaged", "rule", {"label": "real", "reason": f"{failures} consecutive checks failed"})
            elif transition == "UP" and incident_id:
                opened = db.execute("SELECT opened_at FROM incidents WHERE id=?", (incident_id,)).fetchone()
                minutes = max(0, round((parse_time(at) - parse_time(opened[0])).total_seconds() / 60))
                db.execute("UPDATE incidents SET state='closed',closed_at=?,close_reason=? WHERE id=?",
                           (at, f"UP again after {minutes} min", incident_id))
                self._ledger(db, incident_id, "closed", "engine", {"reason": f"UP again after {minutes} min"})
            db.execute("""UPDATE check_state SET status=?,consecutive_failures=?,last_checked_at=?,
                       last_transition_at=CASE WHEN ? IS NULL THEN last_transition_at ELSE ? END,
                       open_incident_id=? WHERE check_id=?""",
                       (new_status, failures, at, transition, at, None if transition == "UP" else incident_id, check_id))
            db.execute("COMMIT")
        return {"status": new_status, "transition": transition, "incident_id": incident_id, "failures": failures, "at": at}

    @staticmethod
    def _new_incident_id(db: sqlite3.Connection, at: str) -> str:
        date = parse_time(at).strftime("%Y%m%d")
        row = db.execute("SELECT COUNT(*) FROM incidents WHERE id LIKE ?", (f"TE-{date}-%",)).fetchone()
        return f"TE-{date}-{int(row[0]) + 1:03d}"

    @staticmethod
    def _ledger(db: sqlite3.Connection, incident_id: str | None, kind: str, actor: str, body: dict[str, Any]) -> None:
        db.execute("INSERT INTO ledger(at,incident_id,kind,actor,body) VALUES(?,?,?,?,?)",
                   (utc_now(), incident_id, kind, actor, json_text(body)))

    def due_checks(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT c.*,s.last_checked_at FROM checks c JOIN check_state s ON s.check_id=c.id
                               WHERE c.kind != 'beat' ORDER BY c.id""").fetchall()
        result = []
        for row in rows:
            if not row["last_checked_at"] or (now - parse_time(row["last_checked_at"])).total_seconds() >= row["period_s"]:
                result.append(self._check_dict(row))
        return result

    def find_missed_beats(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        missed = []
        for row in self.beat_rows():
            baseline = row["last_beat_at"] or row["created_at"]
            age = (now - parse_time(baseline)).total_seconds()
            if age > row["period_s"] + row["grace_s"] and row["status"] != "DOWN":
                missed.append(row)
        return missed

    def state_for(self, check_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM check_state WHERE check_id=?", (check_id,)).fetchone()
        return dict(row) if row else {}

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def incidents(self, open_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM incidents"
        if open_only:
            query += " WHERE state NOT IN ('closed','dismissed')"
        query += " ORDER BY opened_at DESC"
        with closing(self._connect()) as db:
            rows = db.execute(query).fetchall()
        return [dict(row) for row in rows]

    def ledger(self, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM ledger ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute("""SELECT c.id,c.display_name,c.kind,c.owner,c.period_s,c.grace_s,s.status,
                               s.last_checked_at,s.open_incident_id,b.last_beat_at
                               FROM checks c JOIN check_state s ON s.check_id=c.id
                               LEFT JOIN beat_tokens b ON b.check_id=c.id ORDER BY c.display_name""").fetchall()
        return [dict(row) for row in rows]

    def setting(self, key: str, default: str = "") -> str:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with closing(self._connect()) as db:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def record_signal(self, incident_id: str, path: str, ok: bool, detail: str) -> None:
        with closing(self._connect()) as db:
            db.execute("INSERT INTO signals(incident_id,path,at,ok,detail) VALUES(?,?,?,?,?)",
                       (incident_id, path, utc_now(), int(ok), detail))
            self._ledger(db, incident_id, "signalled", "engine", {"path": path, "ok": ok})

    def record_path_test(self, path: str, ok: bool, detail: str) -> None:
        with closing(self._connect()) as db:
            db.execute("INSERT INTO signals(incident_id,path,at,ok,detail) VALUES(NULL,?,?,?,?)",
                       (path, utc_now(), int(ok), detail))
            self._ledger(db, None, "path_test", "engine", {"path": path, "ok": ok, "detail": detail})

    def export_ledger(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in reversed(self.ledger(1_000_000)):
                handle.write(json_text(row) + "\n")

    def rollup_old_events(self, cutoff: str, rollup_cutoff: str) -> None:
        """Keep raw observations for 30 days and hourly counts for one year."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT check_id,substr(at,1,13) || ':00:00+00:00' AS hour,
                               COUNT(*) AS observations,SUM(ok) AS successes,SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS failures
                               FROM events WHERE at < ? GROUP BY check_id,hour""", (cutoff,)).fetchall()
            for row in rows:
                db.execute("""INSERT INTO event_rollups(check_id,hour,observations,successes,failures,value)
                           VALUES(?,?,?,?,?,?) ON CONFLICT(check_id,hour) DO UPDATE SET
                           observations=excluded.observations,successes=excluded.successes,
                           failures=excluded.failures,value=excluded.value""",
                           (row["check_id"], row["hour"], row["observations"], row["successes"], row["failures"], "{}"))
            db.execute("DELETE FROM events WHERE at < ?", (cutoff,))
            db.execute("DELETE FROM event_rollups WHERE hour < ?", (rollup_cutoff,))
            db.execute("COMMIT")


def judge(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable policy hook. Stage 1 passes observations through unchanged."""
    return results
