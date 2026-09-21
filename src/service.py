"""积水隐患巡查领域服务。

只用 Python 标准库（sqlite3）实现，面向网格员上报与调度端查询：

* 接收地点、时间、现场描述、图片摘要、严重级别等上报信息；
* 将同一地点、短时间内的重复上报聚合为一个事件，每条原始上报（证据）独立留存；
* 坐标精度不足或描述缺失的事件进入「待补充」状态，禁止直接派发；
* 支持核实、派发/转派、合并、关闭、重新打开，所有状态变化记录操作者与依据；
* 上报编号 / 事件编号幂等；
* 聚合关系与完整时间线持久化在 SQLite 中，服务重启后仍可追溯。
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

__all__ = [
    "Service",
    "ServiceError",
    "NotFoundError",
    "ValidationError",
    "InvalidTransition",
    "STATUS_LABELS",
    "SEVERITY_LABELS",
]

# ---- 状态与级别常量 ---------------------------------------------------------

PENDING_SUPPLEMENT = "PENDING_SUPPLEMENT"  # 待补充
PENDING = "PENDING"                        # 待处理（已立案、未派发）
VERIFIED = "VERIFIED"                      # 已核实
DISPATCHED = "DISPATCHED"                  # 已派发 / 处理中
CLOSED = "CLOSED"                          # 已关闭
MERGED = "MERGED"                          # 已合并（被并入其他事件）

OPEN_STATUSES = (PENDING_SUPPLEMENT, PENDING, VERIFIED, DISPATCHED)

STATUS_LABELS = {
    PENDING_SUPPLEMENT: "待补充",
    PENDING: "待处理",
    VERIFIED: "已核实",
    DISPATCHED: "已派发",
    CLOSED: "已关闭",
    MERGED: "已合并",
}

LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
CRITICAL = "CRITICAL"
SEVERITY_ORDER = {LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4}
SEVERITY_LABELS = {
    LOW: "低",
    MEDIUM: "中",
    HIGH: "高",
    CRITICAL: "紧急",
}
_LABEL_TO_SEVERITY = {v: k for k, v in SEVERITY_LABELS.items()}
_LABEL_TO_SEVERITY.update({k: k for k in SEVERITY_ORDER})

# 各严重级别的处理时限（秒）：紧急 2h、高 4h、中 12h、低 24h
DEFAULT_SLA_SECONDS = {
    CRITICAL: 2 * 3600,
    HIGH: 4 * 3600,
    MEDIUM: 12 * 3600,
    LOW: 24 * 3600,
}


class ServiceError(Exception):
    """业务错误基类。"""


class NotFoundError(ServiceError):
    """事件或上报不存在。"""


class ValidationError(ServiceError):
    """入参不合法。"""


class InvalidTransition(ServiceError):
    """状态流转不被允许（例如向「待补充」事件直接派发）。"""


# ---- 纯函数工具 -------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(value: Any) -> datetime:
    """把 ISO 字符串 / datetime / epoch 秒统一为带时区的 datetime。"""
    if value is None:
        return _now()
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return _now()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValidationError(f"无法解析的时间值: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


_PUNCT_RE = re.compile(r"[\s，,。.、＃#\-—_（）()【】\[\]]+")


def _normalize_location(name: Optional[str]) -> str:
    """地点名称归一化：去空白与常见标点、转小写，用于同名地点聚合。"""
    if not name:
        return ""
    return _PUNCT_RE.sub("", name.strip().lower())


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _new_id(prefix: str) -> str:
    return f"{prefix}{_now().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:8]}"


# ---- 主服务 -----------------------------------------------------------------


class Service:
    """积水隐患巡查领域服务。

    Parameters
    ----------
    db_path:
        SQLite 文件路径；默认 ``:memory:``（进程内、重启不保留）。
        生产部署传入文件路径即可在服务重启后恢复全部事件、证据与时间线。
    aggregation_window_seconds:
        同地点上报聚合的时间窗，默认 2 小时。
    aggregation_radius_meters:
        坐标聚合半径，默认 50 米。
    max_coord_accuracy_meters:
        坐标精度上限（米），上报的 ``coord_accuracy`` 超过该值视为精度不足。
    sla_seconds:
        按严重级别覆盖默认处理时限。
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        aggregation_window_seconds: float = 2 * 3600,
        aggregation_radius_meters: float = 50.0,
        max_coord_accuracy_meters: float = 100.0,
        sla_seconds: Optional[dict] = None,
    ) -> None:
        self.db_path = db_path
        self.window = float(aggregation_window_seconds)
        self.radius = float(aggregation_radius_meters)
        self.max_accuracy = float(max_coord_accuracy_meters)
        self.sla = dict(DEFAULT_SLA_SECONDS)
        if sla_seconds:
            unknown = set(sla_seconds) - set(DEFAULT_SLA_SECONDS)
            if unknown:
                raise ValidationError(f"未知严重级别: {sorted(unknown)}")
            self.sla.update(sla_seconds)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()
        self.ready = True

    # -- 基础 ---------------------------------------------------------------

    def shutdown(self) -> None:
        """关闭数据库连接。业务上的关闭事件请用 :meth:`close_event`。"""
        with self._lock:
            self._conn.close()
            self.ready = False

    def __enter__(self) -> "Service":
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id         TEXT PRIMARY KEY,
                    location_key     TEXT NOT NULL DEFAULT '',
                    location_name    TEXT,
                    lat              REAL,
                    lng              REAL,
                    severity         TEXT NOT NULL,
                    status           TEXT NOT NULL,
                    responsible_dept TEXT,
                    dispatched_at    TEXT,
                    deadline         TEXT,
                    first_report_at  TEXT NOT NULL,
                    last_report_at   TEXT NOT NULL,
                    created_at       TEXT NOT NULL,
                    updated_at       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    report_id           TEXT PRIMARY KEY,
                    event_id            TEXT NOT NULL REFERENCES events(event_id),
                    reporter            TEXT,
                    location_name       TEXT,
                    location_key        TEXT NOT NULL DEFAULT '',
                    lat                 REAL,
                    lng                 REAL,
                    coord_accuracy      REAL,
                    coords_complete     INTEGER NOT NULL,
                    description         TEXT,
                    description_complete INTEGER NOT NULL,
                    image_summary       TEXT,
                    severity            TEXT NOT NULL,
                    reported_at         TEXT NOT NULL,
                    received_at         TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id    TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    from_status TEXT,
                    to_status   TEXT,
                    operator    TEXT NOT NULL,
                    basis       TEXT,
                    detail      TEXT,
                    created_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS merges (
                    source_event_id TEXT PRIMARY KEY REFERENCES events(event_id),
                    target_event_id TEXT NOT NULL REFERENCES events(event_id),
                    operator        TEXT NOT NULL,
                    basis           TEXT,
                    created_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_event ON reports(event_id);
                CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
                CREATE INDEX IF NOT EXISTS idx_events_loc ON events(location_key);
                CREATE INDEX IF NOT EXISTS idx_timeline_event ON timeline(event_id);
                """
            )
            self._conn.commit()

    def _timeline(
        self,
        event_id: str,
        action: str,
        operator: str,
        *,
        from_status: Optional[str],
        to_status: Optional[str],
        basis: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO timeline
               (event_id, action, from_status, to_status, operator, basis, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                action,
                from_status,
                to_status,
                operator,
                basis,
                _json_dumps(detail or {}),
                _iso(_now()),
            ),
        )

    # -- 上报与聚合 ----------------------------------------------------------

    def report(
        self,
        *,
        location_name: Optional[str] = None,
        reported_at: Any = None,
        description: Optional[str] = None,
        image_summary: Optional[str] = None,
        severity: str = MEDIUM,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        coord_accuracy: Optional[float] = None,
        reporter: Optional[str] = None,
        report_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> dict:
        """网格员上报一条隐患。

        * 同一地点（坐标在聚合半径内，或归一化后地点名称相同）且在聚合时间窗内的
          活跃事件会被自动聚合，新上报作为独立证据挂在该事件下；
        * ``report_id`` 为客户端幂等键，重复提交返回原事件、不产生重复证据；
        * 显式传入 ``event_id`` 时按事件编号续报（同样幂等）；
        * 坐标精度不足或描述缺失时事件停留在「待补充」，不会被派发。
        """
        severity = _normalize_severity(severity)
        reported_dt = _to_dt(reported_at)

        with self._lock:
            # 1) 上报编号幂等：同一 report_id 永远返回同一结果
            if report_id:
                row = self._conn.execute(
                    "SELECT event_id FROM reports WHERE report_id = ?", (report_id,)
                ).fetchone()
                if row is not None:
                    event_id_existing = self._resolve_target(row["event_id"])
                    return {
                        "created": False,
                        "report_id": report_id,
                        "event_id": event_id_existing,
                        "reason": "duplicate_report_id",
                    }

            coords_ok = self._coords_complete(lat, lng, coord_accuracy)
            desc_ok = bool(description and description.strip())
            loc_key = _normalize_location(location_name)
            report_id = report_id or _new_id("R")
            received = _iso(_now())

            # 2) 显式按事件编号续报
            target: Optional[sqlite3.Row] = None
            if event_id:
                target = self._get_event_row(event_id)
                if target["status"] == CLOSED:
                    raise InvalidTransition(
                        f"事件 {event_id} 已关闭，不能继续上报，请新建事件"
                    )
                if target["status"] == MERGED:
                    target = self._get_event_row(self._resolve_target(event_id))

            # 3) 自动聚合
            if target is None:
                target = self._find_aggregate(
                    loc_key=loc_key,
                    lat=lat if coords_ok else None,
                    lng=lng if coords_ok else None,
                    reported_dt=reported_dt,
                )

            if target is not None:
                event_id = target["event_id"]
                self._conn.execute(
                    """INSERT INTO reports (report_id, event_id, reporter, location_name,
                       location_key, lat, lng, coord_accuracy, coords_complete,
                       description, description_complete, image_summary, severity,
                       reported_at, received_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        report_id, event_id, reporter, location_name, loc_key,
                        lat, lng, coord_accuracy, int(coords_ok),
                        description, int(desc_ok), image_summary, severity,
                        _iso(reported_dt), received,
                    ),
                )
                new_status = target["status"]
                updates = ["last_report_at = ?", "updated_at = ?"]
                params: list = [_iso(reported_dt), received]
                if SEVERITY_ORDER[severity] > SEVERITY_ORDER[target["severity"]]:
                    updates.append("severity = ?")
                    params.append(severity)
                if target["lat"] is None and coords_ok:
                    updates.extend(["lat = ?", "lng = ?"])
                    params.extend([lat, lng])
                if target["location_name"] is None and location_name:
                    updates.append("location_name = ?")
                    params.append(location_name)
                # 新证据补齐了信息：待补充 -> 待处理
                supplement_fields = []
                if new_status == PENDING_SUPPLEMENT and (coords_ok or desc_ok):
                    missing = self._missing_fields(event_id, extra_coords=coords_ok,
                                                   extra_desc=desc_ok)
                    if not missing:
                        new_status = PENDING
                        updates.append("status = ?")
                        params.append(new_status)
                        supplement_fields = ["由新上报补齐信息"]
                params.append(event_id)
                self._conn.execute(
                    f"UPDATE events SET {', '.join(updates)} WHERE event_id = ?", params
                )
                self._timeline(
                    event_id, "REPORT_AGGREGATED", reporter or "网格员",
                    from_status=target["status"], to_status=new_status,
                    basis=f"上报编号 {report_id}",
                    detail={
                        "report_id": report_id,
                        "severity": severity,
                        "location_name": location_name,
                        "reported_at": _iso(reported_dt),
                        "supplement": supplement_fields,
                    },
                )
                self._conn.commit()
                return {
                    "created": False,
                    "aggregated": True,
                    "report_id": report_id,
                    "event_id": event_id,
                    "status": new_status,
                }

            # 4) 新建事件
            event_id = event_id or _new_id("EV")
            status = PENDING if (coords_ok and desc_ok) else PENDING_SUPPLEMENT
            now_iso = _iso(_now())
            self._conn.execute(
                """INSERT INTO events (event_id, location_key, location_name, lat, lng,
                   severity, status, first_report_at, last_report_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id, loc_key, location_name,
                    lat if coords_ok else None, lng if coords_ok else None,
                    severity, status, _iso(reported_dt), _iso(reported_dt),
                    now_iso, now_iso,
                ),
            )
            self._conn.execute(
                """INSERT INTO reports (report_id, event_id, reporter, location_name,
                   location_key, lat, lng, coord_accuracy, coords_complete,
                   description, description_complete, image_summary, severity,
                   reported_at, received_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report_id, event_id, reporter, location_name, loc_key,
                    lat, lng, coord_accuracy, int(coords_ok),
                    description, int(desc_ok), image_summary, severity,
                    _iso(reported_dt), received,
                ),
            )
            self._timeline(
                event_id, "CREATED", reporter or "网格员",
                from_status=None, to_status=status,
                basis=f"首条上报 {report_id}",
                detail={
                    "report_id": report_id,
                    "severity": severity,
                    "missing_fields": [] if status == PENDING
                    else self._missing_fields(event_id),
                },
            )
            self._conn.commit()
            return {
                "created": True,
                "aggregated": False,
                "report_id": report_id,
                "event_id": event_id,
                "status": status,
            }

    def supplement(
        self,
        event_id: str,
        operator: str,
        basis: str,
        *,
        description: Optional[str] = None,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        coord_accuracy: Optional[float] = None,
        image_summary: Optional[str] = None,
    ) -> dict:
        """补充信息（现场描述 / 精确坐标 / 图片摘要）。

        仅 ``待补充`` 事件可补充；信息补齐后自动转为 ``待处理``。
        补充内容写入最近一条不完整的原始上报，原值在时间线中留痕。
        """
        if not basis or not str(basis).strip():
            raise ValidationError("补充信息必须填写依据")
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] != PENDING_SUPPLEMENT:
                raise InvalidTransition(
                    f"事件当前为「{STATUS_LABELS.get(event['status'], event['status'])}」，"
                    "无需补充"
                )
            coords_ok = self._coords_complete(lat, lng, coord_accuracy)
            desc_ok = bool(description and description.strip())
            if not any([desc_ok, coords_ok, image_summary and image_summary.strip()]):
                raise ValidationError("至少补充一项有效信息（描述、精确坐标或图片摘要）")

            changes: dict = {}
            row = self._conn.execute(
                """SELECT * FROM reports WHERE event_id = ?
                   AND (coords_complete = 0 OR description_complete = 0)
                   ORDER BY reported_at DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
            if row is None:
                raise InvalidTransition("事件下没有需要补充的原始上报")

            new_desc, new_lat, new_lng, new_acc, new_img = (
                row["description"], row["lat"], row["lng"],
                row["coord_accuracy"], row["image_summary"],
            )
            if desc_ok:
                changes["description"] = {"old": row["description"], "new": description}
                new_desc = description
            if coords_ok:
                changes["coordinates"] = {
                    "old": None if row["lat"] is None else [row["lat"], row["lng"]],
                    "new": [lat, lng],
                    "accuracy_meters": coord_accuracy,
                }
                new_lat, new_lng, new_acc = lat, lng, coord_accuracy
            if image_summary and image_summary.strip():
                new_img = (row["image_summary"] or "") + " | 补充: " + image_summary
                changes["image_summary_appended"] = image_summary

            self._conn.execute(
                """UPDATE reports SET description = ?, lat = ?, lng = ?,
                   coord_accuracy = ?, coords_complete = ?, description_complete = ?,
                   image_summary = ? WHERE report_id = ?""",
                (
                    new_desc, new_lat, new_lng, new_acc,
                    int(coords_ok or bool(row["coords_complete"])),
                    int(desc_ok or bool(row["description_complete"])),
                    new_img, row["report_id"],
                ),
            )
            missing = self._missing_fields(event_id)
            new_status = PENDING if not missing else PENDING_SUPPLEMENT
            updates = ["status = ?", "updated_at = ?"]
            params: list = [new_status, _iso(_now())]
            if self._event_row_lat(event) is None and coords_ok:
                updates[0:0] = ["lat = ?", "lng = ?"]
                params[0:0] = [lat, lng]
            params.append(event_id)
            self._conn.execute(
                f"UPDATE events SET {', '.join(updates)} WHERE event_id = ?", params
            )
            self._timeline(
                event_id, "SUPPLEMENTED", operator,
                from_status=PENDING_SUPPLEMENT, to_status=new_status,
                basis=basis, detail={"changes": changes, "missing_fields": missing},
            )
            self._conn.commit()
            return self.get_event(event_id)

    # -- 状态流转 ------------------------------------------------------------

    def verify(self, event_id: str, operator: str, basis: str,
               *, severity: Optional[str] = None) -> dict:
        """核实事件。仅 ``待处理`` 事件可核实，核实时可修正严重级别。"""
        self._require_basis(operator, basis)
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] == PENDING_SUPPLEMENT:
                raise InvalidTransition("待补充事件信息不完整，不能核实，请先补充")
            if event["status"] != PENDING:
                raise InvalidTransition(
                    f"「{STATUS_LABELS.get(event['status'], event['status'])}」"
                    "状态的事件不能核实"
                )
            detail = {}
            updates = ["status = ?", "updated_at = ?"]
            params: list = [VERIFIED, _iso(_now())]
            if severity is not None:
                sev = _normalize_severity(severity)
                updates.insert(0, "severity = ?")
                params.insert(0, sev)
                detail["severity"] = sev
            params.append(event_id)
            self._conn.execute(
                f"UPDATE events SET {', '.join(updates)} WHERE event_id = ?", params
            )
            self._timeline(event_id, "VERIFIED", operator,
                           from_status=PENDING, to_status=VERIFIED,
                           basis=basis, detail=detail)
            self._conn.commit()
            return self.get_event(event_id)

    def dispatch(self, event_id: str, operator: str, basis: str,
                 responsible_dept: str) -> dict:
        """派发事件到责任部门并按严重级别设定处理时限。

        ``待补充`` / 已关闭 / 已合并事件禁止派发。
        """
        self._require_basis(operator, basis)
        if not responsible_dept or not str(responsible_dept).strip():
            raise ValidationError("必须指定责任部门")
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] == PENDING_SUPPLEMENT:
                raise InvalidTransition("待补充事件不能直接派发，请先补充信息")
            if event["status"] not in (PENDING, VERIFIED):
                raise InvalidTransition(
                    f"「{STATUS_LABELS.get(event['status'], event['status'])}」"
                    "状态的事件不能派发"
                )
            now = _now()
            # 处理时限自首次上报起算（响应时限口径），补发/积压事件派发即显示已超时
            deadline = _iso(
                datetime.fromtimestamp(
                    _to_dt(event["first_report_at"]).timestamp()
                    + self.sla[event["severity"]],
                    tz=timezone.utc,
                )
            )
            self._conn.execute(
                """UPDATE events SET status = ?, responsible_dept = ?,
                   dispatched_at = ?, deadline = ?, updated_at = ? WHERE event_id = ?""",
                (DISPATCHED, responsible_dept, _iso(now), deadline, _iso(now), event_id),
            )
            self._timeline(
                event_id, "DISPATCHED", operator,
                from_status=event["status"], to_status=DISPATCHED, basis=basis,
                detail={
                    "responsible_dept": responsible_dept,
                    "sla_seconds": self.sla[event["severity"]],
                    "deadline": deadline,
                },
            )
            self._conn.commit()
            return self.get_event(event_id)

    def transfer(self, event_id: str, operator: str, basis: str,
                 new_dept: str) -> dict:
        """转派到其他责任部门。处理时限沿用首次派发时的截止时间，不重新计时。"""
        self._require_basis(operator, basis)
        if not new_dept or not str(new_dept).strip():
            raise ValidationError("必须指定转入的责任部门")
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] != DISPATCHED:
                raise InvalidTransition("只有已派发事件可以转派")
            if new_dept == event["responsible_dept"]:
                raise InvalidTransition(f"事件本就由「{new_dept}」负责，无需转派")
            self._conn.execute(
                "UPDATE events SET responsible_dept = ?, updated_at = ? WHERE event_id = ?",
                (new_dept, _iso(_now()), event_id),
            )
            self._timeline(
                event_id, "TRANSFERRED", operator,
                from_status=DISPATCHED, to_status=DISPATCHED, basis=basis,
                detail={
                    "from_dept": event["responsible_dept"],
                    "to_dept": new_dept,
                    "deadline_unchanged": event["deadline"],
                },
            )
            self._conn.commit()
            return self.get_event(event_id)

    def merge(self, source_event_id: str, target_event_id: str,
              operator: str, basis: str) -> dict:
        """把 source 事件合并进 target 事件。

        * source 下的全部原始证据保持原归属不变，通过 ``merges`` 关系挂到 target
          的证据链上，聚合关系可追溯；
        * source 进入 ``已合并`` 终态，双方时间线各自留痕。
        """
        self._require_basis(operator, basis)
        if source_event_id == target_event_id:
            raise ValidationError("不能将事件合并到自身")
        with self._lock:
            source = self._get_event_row(source_event_id)
            target = self._get_event_row(target_event_id)
            for label, ev in (("被合并", source), ("合并目标", target)):
                if ev["status"] not in OPEN_STATUSES:
                    raise InvalidTransition(
                        f"{label}事件 {ev['event_id']} 为"
                        f"「{STATUS_LABELS.get(ev['status'], ev['status'])}」，不能合并"
                    )
            dup = self._conn.execute(
                "SELECT 1 FROM merges WHERE source_event_id = ?",
                (source_event_id,),
            ).fetchone()
            if dup is not None:
                raise InvalidTransition(f"事件 {source_event_id} 已被合并")

            now_iso = _iso(_now())
            # 目标事件取更高严重级别，并补齐缺失的地点/坐标
            severity = max(
                (source["severity"], target["severity"]),
                key=lambda s: SEVERITY_ORDER[s],
            )
            self._conn.execute(
                """UPDATE events SET severity = ?,
                   lat = COALESCE(lat, ?), lng = COALESCE(lng, ?),
                   location_name = COALESCE(location_name, ?),
                   last_report_at = ?, updated_at = ? WHERE event_id = ?""",
                (
                    severity, source["lat"], source["lng"], source["location_name"],
                    max(source["last_report_at"], target["last_report_at"]),
                    now_iso, target_event_id,
                ),
            )
            self._conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE event_id = ?",
                (MERGED, now_iso, source_event_id),
            )
            self._conn.execute(
                """INSERT INTO merges (source_event_id, target_event_id,
                   operator, basis, created_at) VALUES (?, ?, ?, ?, ?)""",
                (source_event_id, target_event_id, operator, basis, now_iso),
            )
            self._timeline(
                source_event_id, "MERGED", operator,
                from_status=source["status"], to_status=MERGED, basis=basis,
                detail={"target_event_id": target_event_id},
            )
            self._timeline(
                target_event_id, "MERGE_RECEIVED", operator,
                from_status=target["status"], to_status=target["status"],
                basis=basis,
                detail={
                    "source_event_id": source_event_id,
                    "evidence_added": self._conn.execute(
                        "SELECT COUNT(*) c FROM reports WHERE event_id = ?",
                        (source_event_id,),
                    ).fetchone()["c"],
                },
            )
            self._conn.commit()
            return self.get_event(target_event_id)

    def close_event(self, event_id: str, operator: str, basis: str,
                    *, outcome: Optional[str] = None) -> dict:
        """关闭事件（须填写依据，如现场处置结果）。"""
        self._require_basis(operator, basis)
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] not in OPEN_STATUSES:
                raise InvalidTransition(
                    f"「{STATUS_LABELS.get(event['status'], event['status'])}」"
                    "状态的事件不能关闭"
                )
            self._conn.execute(
                "UPDATE events SET status = ?, updated_at = ? WHERE event_id = ?",
                (CLOSED, _iso(_now()), event_id),
            )
            self._timeline(
                event_id, "CLOSED", operator,
                from_status=event["status"], to_status=CLOSED, basis=basis,
                detail={"outcome": outcome},
            )
            self._conn.commit()
            return self.get_event(event_id)

    def reopen(self, event_id: str, operator: str, basis: str) -> dict:
        """重新打开已关闭事件。

        回到 ``待处理`` 状态重新出现在未处理清单中；原责任部门与截止时间在时间线
        中留痕，需重新核实、派发。
        """
        self._require_basis(operator, basis)
        with self._lock:
            event = self._get_event_row(event_id)
            if event["status"] != CLOSED:
                raise InvalidTransition("只有已关闭事件可以重新打开")
            self._conn.execute(
                """UPDATE events SET status = ?, responsible_dept = NULL,
                   dispatched_at = NULL, deadline = NULL, updated_at = ?
                   WHERE event_id = ?""",
                (PENDING, _iso(_now()), event_id),
            )
            self._timeline(
                event_id, "REOPENED", operator,
                from_status=CLOSED, to_status=PENDING, basis=basis,
                detail={
                    "previous_responsible_dept": event["responsible_dept"],
                    "previous_deadline": event["deadline"],
                },
            )
            self._conn.commit()
            return self.get_event(event_id)

    # -- 查询（调度端） -------------------------------------------------------

    def list_unhandled(self, *, include_verified: bool = True) -> list[dict]:
        """未处理隐患清单：未关闭、未合并、未完成派发的事件。

        返回按严重级别（紧急优先）、首次上报时间排序的事件视图，含责任部门、
        处理时限和是否可派发标记。``待补充`` 事件带 ``missing_fields``。
        """
        statuses = [PENDING_SUPPLEMENT, PENDING]
        if include_verified:
            statuses.append(VERIFIED)
        placeholders = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"""SELECT * FROM events WHERE status IN ({placeholders})
                ORDER BY CASE severity
                    WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                    WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                first_report_at""",
            statuses,
        ).fetchall()
        now = _now()
        result = []
        for row in rows:
            item = self._event_view(row, now)
            item["dispatchable"] = row["status"] in (PENDING, VERIFIED)
            if row["status"] == PENDING_SUPPLEMENT:
                item["missing_fields"] = self._missing_fields(row["event_id"])
            result.append(item)
        return result

    def dispatch_board(self, *, include_closed: bool = False) -> list[dict]:
        """调度看板：已派发事件的当前责任部门与处理时限（含是否超时）。"""
        sql = "SELECT * FROM events WHERE status = ?"
        params: list = [DISPATCHED]
        if include_closed:
            sql = "SELECT * FROM events WHERE status IN (?, ?) ORDER BY deadline"
            params = [DISPATCHED, CLOSED]
        rows = self._conn.execute(sql + " ORDER BY deadline", params).fetchall()
        return [self._event_view(row, _now()) for row in rows]

    def get_event(self, event_id: str) -> dict:
        """事件详情：状态、责任部门、时限、证据数量、合并关系、缺失信息。"""
        with self._lock:
            row = self._get_event_row(event_id)
            view = self._event_view(row, _now())
            view["evidence"] = self.list_evidence(event_id)
            view["timeline"] = self.event_timeline(event_id, include_merged=True)
            view["merged_from"] = self._merged_sources(event_id)
            target = self._resolve_target(event_id)
            if target != event_id:
                view["merged_into"] = target
            if row["status"] == PENDING_SUPPLEMENT:
                view["missing_fields"] = self._missing_fields(event_id)
            return view

    def list_evidence(self, event_id: str) -> list[dict]:
        """列出事件的全部原始上报证据（含被合并事件带来的证据），按上报时间排序。"""
        self._get_event_row(event_id)
        rows = self._conn.execute(
            """WITH RECURSIVE chain(eid) AS (
                   SELECT ?
                   UNION ALL
                   SELECT m.source_event_id FROM merges m
                   JOIN chain ON m.target_event_id = chain.eid
               )
               SELECT r.* FROM reports r JOIN chain ON r.event_id = chain.eid
               ORDER BY r.reported_at, r.received_at""",
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def event_timeline(self, event_id: str, *, include_merged: bool = True) -> list[dict]:
        """完整状态变化时间线（操作者、依据、变更前后状态、明细）。"""
        self._get_event_row(event_id)
        if include_merged:
            rows = self._conn.execute(
                """WITH RECURSIVE chain(eid) AS (
                       SELECT ?
                       UNION ALL
                       SELECT m.source_event_id FROM merges m
                       JOIN chain ON m.target_event_id = chain.eid
                   )
                   SELECT t.* FROM timeline t JOIN chain ON t.event_id = chain.eid
                   ORDER BY t.id""",
                (event_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM timeline WHERE event_id = ? ORDER BY id", (event_id,)
            ).fetchall()
        return [
            {
                "seq": r["id"],
                "event_id": r["event_id"],
                "action": r["action"],
                "from_status": r["from_status"],
                "to_status": r["to_status"],
                "operator": r["operator"],
                "basis": r["basis"],
                "detail": _json_loads(r["detail"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -- 内部辅助 ------------------------------------------------------------

    def _get_event_row(self, event_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        return row

    def _resolve_target(self, event_id: str) -> str:
        """沿 merges 链找到事件的最终归属事件。"""
        current = event_id
        for _ in range(1000):
            row = self._conn.execute(
                "SELECT target_event_id FROM merges WHERE source_event_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                return current
            current = row["target_event_id"]

    def _merged_sources(self, event_id: str) -> list[str]:
        return [
            r["source_event_id"]
            for r in self._conn.execute(
                """WITH RECURSIVE chain(eid) AS (
                       SELECT ?
                       UNION ALL
                       SELECT m.source_event_id FROM merges m
                       JOIN chain ON m.target_event_id = chain.eid
                   )
                   SELECT eid AS source_event_id FROM chain WHERE eid != ?""",
                (event_id, event_id),
            ).fetchall()
        ]

    def _find_aggregate(
        self,
        *,
        loc_key: str,
        lat: Optional[float],
        lng: Optional[float],
        reported_dt: datetime,
    ) -> Optional[sqlite3.Row]:
        """在活跃事件中寻找同地点、时间窗内的聚合目标。"""
        placeholders = ",".join("?" * len(OPEN_STATUSES))
        candidates = self._conn.execute(
            f"SELECT * FROM events WHERE status IN ({placeholders})",
            OPEN_STATUSES,
        ).fetchall()
        best: Optional[sqlite3.Row] = None
        for ev in candidates:
            last = _to_dt(ev["last_report_at"])
            if abs((reported_dt - last).total_seconds()) > self.window:
                continue
            same_name = bool(loc_key) and ev["location_key"] == loc_key
            near = False
            if lat is not None and ev["lat"] is not None:
                near = _haversine_m(lat, lng, ev["lat"], ev["lng"]) <= self.radius
            if same_name or near:
                if best is None or ev["last_report_at"] > best["last_report_at"]:
                    best = ev
        return best

    def _coords_complete(
        self,
        lat: Optional[float],
        lng: Optional[float],
        accuracy: Optional[float],
    ) -> bool:
        if lat is None or lng is None:
            return False
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            raise ValidationError("坐标必须是数字")
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
            raise ValidationError(f"坐标超出合法范围: {lat_f}, {lng_f}")
        if accuracy is not None and float(accuracy) > self.max_accuracy:
            return False
        return True

    def _missing_fields(
        self,
        event_id: str,
        *,
        extra_coords: bool = False,
        extra_desc: bool = False,
    ) -> list[str]:
        rows = self._conn.execute(
            """SELECT COALESCE(MAX(coords_complete), 0) c,
                      COALESCE(MAX(description_complete), 0) d
               FROM reports WHERE event_id = ?""",
            (event_id,),
        ).fetchone()
        missing = []
        if not (rows["c"] or extra_coords):
            missing.append("coordinates")
        if not (rows["d"] or extra_desc):
            missing.append("description")
        return missing

    @staticmethod
    def _event_row_lat(event: sqlite3.Row) -> Optional[float]:
        return event["lat"]

    def _event_view(self, ev: sqlite3.Row, now: datetime) -> dict:
        deadline_dt = _to_dt(ev["deadline"]) if ev["deadline"] else None
        seconds_left = (
            (deadline_dt - now).total_seconds() if deadline_dt is not None else None
        )
        count = self._conn.execute(
            """WITH RECURSIVE chain(eid) AS (
                   SELECT ?
                   UNION ALL
                   SELECT m.source_event_id FROM merges m
                   JOIN chain ON m.target_event_id = chain.eid
               )
               SELECT COUNT(*) c FROM reports r JOIN chain ON r.event_id = chain.eid""",
            (ev["event_id"],),
        ).fetchone()["c"]
        return {
            "event_id": ev["event_id"],
            "status": ev["status"],
            "status_label": STATUS_LABELS[ev["status"]],
            "severity": ev["severity"],
            "severity_label": SEVERITY_LABELS[ev["severity"]],
            "location_name": ev["location_name"],
            "lat": ev["lat"],
            "lng": ev["lng"],
            "responsible_dept": ev["responsible_dept"],
            "dispatched_at": ev["dispatched_at"],
            "deadline": ev["deadline"],
            "seconds_left": seconds_left,
            "overdue": seconds_left is not None and seconds_left < 0,
            "first_report_at": ev["first_report_at"],
            "last_report_at": ev["last_report_at"],
            "evidence_count": count,
            "updated_at": ev["updated_at"],
        }

    @staticmethod
    def _require_basis(operator: str, basis: str) -> None:
        if not operator or not str(operator).strip():
            raise ValidationError("必须记录操作者")
        if not basis or not str(basis).strip():
            raise ValidationError("状态变化必须记录依据")


def _normalize_severity(value: str) -> str:
    if value is None:
        raise ValidationError("缺少严重级别")
    sev = _LABEL_TO_SEVERITY.get(str(value).strip())
    if sev is None:
        raise ValidationError(
            f"未知严重级别: {value!r}，可选: {sorted(SEVERITY_LABELS)}"
        )
    return sev


# 小型 JSON 封装，集中处理中文不转义
import json  # noqa: E402


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _json_loads(text: Optional[str]) -> Any:
    if not text:
        return {}
    return json.loads(text)
