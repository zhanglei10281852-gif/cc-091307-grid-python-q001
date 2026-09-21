"""SQLite 持久化层。

事件、原始上报、时间线与幂等操作记录全部落库，
服务重启后聚合关系与完整时间线仍可追溯。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional, Sequence

from .models import (
    Event,
    EventLog,
    EventStatus,
    LogAction,
    Report,
    Severity,
    event_from_dict,
    event_to_dict,
    parse_dt,
    to_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    severity       INTEGER NOT NULL,
    latitude       REAL,
    longitude      REAL,
    department     TEXT,
    merged_into    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    last_report_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_id    TEXT PRIMARY KEY,          -- 上报编号，幂等键
    event_id     TEXT NOT NULL REFERENCES events(event_id),
    reporter_id  TEXT NOT NULL,
    latitude     REAL,
    longitude    REAL,
    accuracy_m   REAL,
    reported_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    description  TEXT,
    image_digest TEXT,
    severity     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_event ON reports(event_id);

CREATE TABLE IF NOT EXISTS event_logs (
    log_id     TEXT PRIMARY KEY,
    event_id   TEXT NOT NULL REFERENCES events(event_id),
    action     TEXT NOT NULL,
    operator   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_event ON event_logs(event_id);

CREATE TABLE IF NOT EXISTS operations (
    op_id       TEXT PRIMARY KEY,           -- 操作编号，状态变更幂等键
    event_id    TEXT NOT NULL,
    action      TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


class SQLiteStorage:
    """线程安全的 SQLite 存储。db_path 传 ":memory:" 可用于测试。"""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def insert_event(self, event: Event) -> None:
        d = event_to_dict(event)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO events
                   (event_id, status, severity, latitude, longitude, department,
                    merged_into, created_at, updated_at, last_report_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    d["event_id"], d["status"], d["severity"], d["latitude"],
                    d["longitude"], d["department"], d["merged_into"],
                    d["created_at"], d["updated_at"], d["last_report_at"],
                ),
            )

    def update_event(self, event: Event) -> None:
        d = event_to_dict(event)
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE events SET status=?, severity=?, latitude=?, longitude=?,
                   department=?, merged_into=?, updated_at=?, last_report_at=?
                   WHERE event_id=?""",
                (
                    d["status"], d["severity"], d["latitude"], d["longitude"],
                    d["department"], d["merged_into"], d["updated_at"],
                    d["last_report_at"], d["event_id"],
                ),
            )

    def get_event(self, event_id: str) -> Optional[Event]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._row_to_event(row) if row else None

    def list_events(self, statuses: Optional[Sequence[EventStatus]] = None) -> list:
        sql = "SELECT * FROM events"
        params: tuple = ()
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            sql += f" WHERE status IN ({placeholders})"
            params = tuple(s.value for s in statuses)
        sql += " ORDER BY created_at, event_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_event(r) for r in rows]

    def count_events_with_prefix(self, prefix: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE event_id LIKE ?",
                (prefix + "%",),
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return event_from_dict(dict(row))

    # ------------------------------------------------------------------
    # 原始上报（证据）
    # ------------------------------------------------------------------

    def insert_report(self, report: Report) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO reports
                   (report_id, event_id, reporter_id, latitude, longitude,
                    accuracy_m, reported_at, received_at, description,
                    image_digest, severity)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report.report_id, report.event_id, report.reporter_id,
                    report.latitude, report.longitude, report.accuracy_m,
                    to_iso(report.reported_at), to_iso(report.received_at),
                    report.description, report.image_digest, int(report.severity),
                ),
            )

    def get_report(self, report_id: str) -> Optional[Report]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reports WHERE report_id=?", (report_id,)
            ).fetchone()
        return self._row_to_report(row) if row else None

    def list_reports(self, event_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports WHERE event_id=? ORDER BY rowid",
                (event_id,),
            ).fetchall()
        return [self._row_to_report(r) for r in rows]

    def count_reports(self, event_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM reports WHERE event_id=?", (event_id,)
            ).fetchone()
        return int(row["n"])

    @staticmethod
    def _row_to_report(row: sqlite3.Row) -> Report:
        return Report(
            report_id=row["report_id"],
            event_id=row["event_id"],
            reporter_id=row["reporter_id"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            accuracy_m=row["accuracy_m"],
            reported_at=parse_dt(row["reported_at"]),
            received_at=parse_dt(row["received_at"]),
            description=row["description"] or "",
            image_digest=row["image_digest"],
            severity=Severity(int(row["severity"])),
        )

    # ------------------------------------------------------------------
    # 时间线
    # ------------------------------------------------------------------

    def insert_log(self, log: EventLog) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO event_logs
                   (log_id, event_id, action, operator, reason, details, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    log.log_id, log.event_id, log.action.value, log.operator,
                    log.reason, json.dumps(log.details, ensure_ascii=False),
                    to_iso(log.created_at),
                ),
            )

    def list_logs(self, event_id: str) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_logs WHERE event_id=? ORDER BY rowid",
                (event_id,),
            ).fetchall()
        return [
            EventLog(
                log_id=r["log_id"],
                event_id=r["event_id"],
                action=LogAction(r["action"]),
                operator=r["operator"],
                reason=r["reason"],
                details=json.loads(r["details"]),
                created_at=parse_dt(r["created_at"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 幂等操作记录
    # ------------------------------------------------------------------

    def insert_operation(self, op_id: str, event_id: str, action: str,
                         result: dict, created_at) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO operations (op_id, event_id, action, result_json, created_at)
                   VALUES (?,?,?,?,?)""",
                (op_id, event_id, action,
                 json.dumps(result, ensure_ascii=False), to_iso(created_at)),
            )

    def get_operation(self, op_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM operations WHERE op_id=?", (op_id,)
            ).fetchone()
        return json.loads(row["result_json"]) if row else None
