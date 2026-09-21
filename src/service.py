"""积水隐患巡查服务。

职责：
- 接收网格员上报（地点、时间、现场描述、图片摘要、严重级别）；
- 同一地点短时重复上报自动聚合为一个事件，每条原始证据完整保留；
- 坐标精度不足或描述缺失的事件进入「待补充」，补齐前不能核实/派发；
- 事件支持核实、转派、合并、关闭、重新打开，全程记录操作者与依据；
- 上报编号 / 操作编号幂等，重复提交返回首次结果；
- 调度端查询未处理隐患、当前责任部门与处理时限；
- 全部状态落 SQLite，重启后聚合关系与时间线可追溯。
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import (
    DispatchItem,
    DomainError,
    Event,
    EventDetail,
    EventLog,
    EventNotFoundError,
    EventStatus,
    IncompleteEventError,
    InvalidStateTransitionError,
    LogAction,
    OPEN_STATUSES,
    Report,
    Severity,
    SubmitResult,
    TransitionResult,
    event_from_dict,
    event_to_dict,
    parse_dt,
)
from .storage import SQLiteStorage

#: 聚合半径（米）：同一地点的判定阈值
DEFAULT_AGGREGATION_RADIUS_M = 50.0
#: 聚合时间窗：距事件最近一次上报在此窗口内视为短时重复
DEFAULT_AGGREGATION_WINDOW = timedelta(minutes=30)
#: 坐标精度阈值（米）：精度缺失或差于该值视为精度不足
DEFAULT_MIN_ACCURACY_M = 50.0

_SYSTEM = "system"


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两坐标间球面距离（米）。"""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class HazardService:
    """隐患巡查领域服务。"""

    def __init__(
        self,
        db_path: str = "hazard.db",
        *,
        aggregation_radius_m: float = DEFAULT_AGGREGATION_RADIUS_M,
        aggregation_window: timedelta = DEFAULT_AGGREGATION_WINDOW,
        min_accuracy_m: float = DEFAULT_MIN_ACCURACY_M,
        now=None,
    ):
        self.storage = SQLiteStorage(db_path)
        self.aggregation_radius_m = aggregation_radius_m
        self.aggregation_window = aggregation_window
        self.min_accuracy_m = min_accuracy_m
        # 时间源可注入，便于测试与回放
        self._now = now or (lambda: datetime.now(timezone.utc))

    def shutdown(self) -> None:
        """释放存储连接（进程退出/重建实例前调用）。"""
        self.storage.close()

    # ------------------------------------------------------------------
    # 上报受理与聚合
    # ------------------------------------------------------------------

    def submit_report(
        self,
        *,
        report_id: str,
        reporter_id: str,
        latitude: Optional[float],
        longitude: Optional[float],
        accuracy_m: Optional[float] = None,
        reported_at=None,
        description: str = "",
        image_digest: Optional[str] = None,
        severity=Severity.MODERATE,
    ) -> SubmitResult:
        """受理一条网格员上报。

        report_id 为幂等键：重复提交返回首次受理结果，不产生重复证据。
        """
        report_id = (report_id or "").strip()
        if not report_id:
            raise DomainError("report_id（上报编号）不能为空")
        if not (reporter_id or "").strip():
            raise DomainError("reporter_id（网格员编号）不能为空")

        existing = self.storage.get_report(report_id)
        if existing is not None:
            return SubmitResult(
                report=existing,
                event=self._require_event(existing.event_id),
                deduplicated=True,
            )

        received_at = self._now()
        reported_at = parse_dt(reported_at) if reported_at is not None else received_at
        severity = Severity(int(severity))
        description = (description or "").strip()
        precise = (
            latitude is not None
            and longitude is not None
            and accuracy_m is not None
            and float(accuracy_m) <= self.min_accuracy_m
        )

        # 精度达标才参与自动聚合，避免漂移坐标把不同隐患错误并案
        event = (
            self._find_aggregate_target(latitude, longitude, reported_at)
            if precise
            else None
        )

        if event is None:
            status = (
                EventStatus.PENDING
                if (precise and description)
                else EventStatus.PENDING_SUPPLEMENT
            )
            event = Event(
                event_id=self._new_event_id(),
                status=status,
                severity=severity,
                latitude=latitude if precise else None,
                longitude=longitude if precise else None,
                department=None,
                merged_into=None,
                created_at=received_at,
                updated_at=received_at,
                last_report_at=reported_at,
            )
            self.storage.insert_event(event)
            self._log(
                event.event_id, LogAction.CREATE,
                operator=reporter_id, reason="网格员上报，新建事件",
                details={"report_id": report_id, "initial_status": status.value},
            )
        else:
            event.severity = max(event.severity, severity)
            if event.latitude is None and precise:
                event.latitude, event.longitude = latitude, longitude
            event.last_report_at = max(event.last_report_at, reported_at)
            event.updated_at = received_at
            self.storage.update_event(event)
            self._log(
                event.event_id, LogAction.ATTACH,
                operator=reporter_id, reason="同一地点短时重复上报，自动聚合",
                details={"report_id": report_id},
            )

        report = Report(
            report_id=report_id,
            event_id=event.event_id,
            reporter_id=reporter_id.strip(),
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
            reported_at=reported_at,
            received_at=received_at,
            description=description,
            image_digest=image_digest,
            severity=severity,
        )
        self.storage.insert_report(report)

        # 补齐检查：待补充事件在资料齐全后自动转入待核实
        if event.status == EventStatus.PENDING_SUPPLEMENT and self._is_complete(event.event_id):
            event.status = EventStatus.PENDING
            event.updated_at = received_at
            self.storage.update_event(event)
            self._log(
                event.event_id, LogAction.SUPPLEMENTED,
                operator=_SYSTEM, reason="坐标精度与现场描述已补齐，转入待核实",
                details={"report_id": report_id},
            )

        return SubmitResult(report=report, event=event, deduplicated=False)

    def _find_aggregate_target(
        self, latitude: float, longitude: float, reported_at: datetime
    ) -> Optional[Event]:
        """在聚合半径与时间窗内寻找最近的未闭环事件。"""
        if latitude is None or longitude is None:
            return None
        best: Optional[Event] = None
        best_dist = math.inf
        window_s = self.aggregation_window.total_seconds()
        for cand in self.storage.list_events(statuses=OPEN_STATUSES):
            if cand.latitude is None:
                continue
            if abs((reported_at - cand.last_report_at).total_seconds()) > window_s:
                continue
            dist = haversine_m(latitude, longitude, cand.latitude, cand.longitude)
            if dist <= self.aggregation_radius_m and dist < best_dist:
                best, best_dist = cand, dist
        return best

    def _is_complete(self, event_id: str) -> bool:
        """资料齐全 = 已有精度达标的坐标 + 至少一条非空现场描述。"""
        event = self.storage.get_event(event_id)
        if event is None or event.latitude is None:
            return False
        return any(r.description.strip() for r in self.storage.list_reports(event_id))

    def _new_event_id(self) -> str:
        today = self._now().strftime("%Y%m%d")
        prefix = f"EVT-{today}-"
        seq = self.storage.count_events_with_prefix(prefix) + 1
        return f"{prefix}{seq:04d}"

    # ------------------------------------------------------------------
    # 状态机：核实 / 转派 / 合并 / 关闭 / 重新打开
    # ------------------------------------------------------------------

    def verify(self, event_id: str, *, operator: str, reason: str,
               op_id: Optional[str] = None) -> TransitionResult:
        """核实事件：待核实 -> 已核实。"""
        def apply(event):
            if event.status == EventStatus.PENDING_SUPPLEMENT:
                raise IncompleteEventError(
                    f"{event_id} 处于待补充状态，坐标或描述未补齐，不能核实")
            self._require_status(event, (EventStatus.PENDING,), "核实")
            event.status = EventStatus.VERIFIED
            return {}

        return self._transition(event_id, LogAction.VERIFY, operator, reason, op_id, apply)

    def reassign(self, event_id: str, *, department: str, operator: str,
                 reason: str, op_id: Optional[str] = None) -> TransitionResult:
        """转派责任部门：待核实/已核实/处理中 -> 处理中。"""
        department = (department or "").strip()
        if not department:
            raise DomainError("department（责任部门）不能为空")

        def apply(event):
            if event.status == EventStatus.PENDING_SUPPLEMENT:
                raise IncompleteEventError(
                    f"{event_id} 处于待补充状态，不能直接派发")
            self._require_status(
                event,
                (EventStatus.PENDING, EventStatus.VERIFIED, EventStatus.DISPATCHED),
                "转派",
            )
            event.department = department
            event.status = EventStatus.DISPATCHED
            return {"department": department}

        return self._transition(event_id, LogAction.REASSIGN, operator, reason, op_id, apply)

    def merge(self, source_event_id: str, target_event_id: str, *,
              operator: str, reason: str,
              op_id: Optional[str] = None) -> TransitionResult:
        """合并：source 并入 target。原始证据保留在各自事件上，可追溯。"""
        if source_event_id == target_event_id:
            raise DomainError("源事件与目标事件不能相同")
        replay = self._replay(op_id)
        if replay is not None:
            return replay
        self._require_actor(operator, reason)
        source = self._require_event(source_event_id)
        target = self._require_event(target_event_id)
        self._require_status(source, OPEN_STATUSES, "合并")
        self._require_status(target, OPEN_STATUSES, "作为合并目标")

        now = self._now()
        source.status = EventStatus.MERGED
        source.merged_into = target.event_id
        source.updated_at = now
        target.severity = max(target.severity, source.severity)
        target.updated_at = now
        self.storage.update_event(source)
        self.storage.update_event(target)
        self._log(source.event_id, LogAction.MERGE, operator, reason,
                  {"merged_into": target.event_id})
        self._log(target.event_id, LogAction.MERGE, operator, reason,
                  {"absorbed_from": source.event_id})

        result = TransitionResult(event=source, op_id=self._op_id(op_id),
                                  target_event=target)
        self._record_operation(result.op_id, source.event_id, LogAction.MERGE, result)
        return result

    def close(self, event_id: str, *, operator: str, reason: str,
              op_id: Optional[str] = None) -> TransitionResult:
        """关闭事件：任一未闭环状态 -> 已关闭。"""
        def apply(event):
            self._require_status(event, OPEN_STATUSES, "关闭")
            event.status = EventStatus.CLOSED
            return {}

        return self._transition(event_id, LogAction.CLOSE, operator, reason, op_id, apply)

    def reopen(self, event_id: str, *, operator: str, reason: str,
               op_id: Optional[str] = None) -> TransitionResult:
        """重新打开：已关闭 -> 待核实。"""
        def apply(event):
            self._require_status(event, (EventStatus.CLOSED,), "重新打开")
            event.status = EventStatus.PENDING
            return {}

        return self._transition(event_id, LogAction.REOPEN, operator, reason, op_id, apply)

    # ------------------------------------------------------------------
    # 调度端查询
    # ------------------------------------------------------------------

    def list_unhandled(self, *, include_pending_supplement: bool = True) -> list:
        """未处理隐患列表：含当前责任部门与处理时限，按严重级别和时限排序。"""
        now = self._now()
        items = []
        for event in self.storage.list_events(statuses=OPEN_STATUSES):
            if event.status == EventStatus.PENDING_SUPPLEMENT and not include_pending_supplement:
                continue
            items.append(DispatchItem(
                event_id=event.event_id,
                status=event.status,
                severity=event.severity,
                department=event.department,
                deadline=event.deadline,
                is_overdue=now > event.deadline,
                dispatchable=event.dispatchable,
                report_count=self.storage.count_reports(event.event_id),
                latitude=event.latitude,
                longitude=event.longitude,
                created_at=event.created_at,
                last_report_at=event.last_report_at,
            ))
        items.sort(key=lambda i: (-int(i.severity), i.deadline, i.event_id))
        return items

    def get_event_detail(self, event_id: str) -> EventDetail:
        """事件完整档案：本体 + 全部原始证据 + 完整时间线。"""
        event = self._require_event(event_id)
        return EventDetail(
            event=event,
            reports=self.storage.list_reports(event_id),
            timeline=self.storage.list_logs(event_id),
        )

    # ------------------------------------------------------------------
    # 内部：状态变更骨架（校验 -> 变更 -> 留痕 -> 幂等记录）
    # ------------------------------------------------------------------

    def _transition(self, event_id, action, operator, reason, op_id, apply):
        replay = self._replay(op_id)
        if replay is not None:
            return replay
        self._require_actor(operator, reason)
        event = self._require_event(event_id)
        extra = apply(event)  # 状态校验与变更，不合法时抛领域错误
        event.updated_at = self._now()
        self.storage.update_event(event)
        self._log(event.event_id, action, operator, reason,
                  {"status": event.status.value, **extra})
        result = TransitionResult(event=event, op_id=self._op_id(op_id))
        self._record_operation(result.op_id, event.event_id, action, result)
        return result

    def _replay(self, op_id) -> Optional[TransitionResult]:
        """相同操作编号重复提交时，返回首次执行的结果快照。"""
        if not op_id:
            return None
        record = self.storage.get_operation(op_id)
        if record is None:
            return None
        target = record.get("target_event")
        return TransitionResult(
            event=event_from_dict(record["event"]),
            op_id=op_id,
            deduplicated=True,
            target_event=event_from_dict(target) if target else None,
        )

    def _record_operation(self, op_id, event_id, action, result: TransitionResult) -> None:
        payload = {"event": event_to_dict(result.event)}
        if result.target_event is not None:
            payload["target_event"] = event_to_dict(result.target_event)
        self.storage.insert_operation(
            op_id, event_id, action.value, payload, self._now())

    @staticmethod
    def _op_id(op_id: Optional[str]) -> str:
        return op_id or f"OP-{uuid.uuid4().hex[:12]}"

    def _log(self, event_id, action, operator, reason, details=None) -> None:
        self.storage.insert_log(EventLog(
            log_id=f"LOG-{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            action=action,
            operator=operator,
            reason=reason,
            details=details or {},
            created_at=self._now(),
        ))

    def _require_event(self, event_id: str) -> Event:
        event = self.storage.get_event(event_id)
        if event is None:
            raise EventNotFoundError(f"事件不存在: {event_id}")
        return event

    @staticmethod
    def _require_status(event: Event, allowed, action_name: str) -> None:
        if event.status not in allowed:
            allowed_text = "/".join(s.value for s in allowed)
            raise InvalidStateTransitionError(
                f"事件 {event.event_id} 当前状态为「{event.status.value}」，"
                f"仅「{allowed_text}」状态可执行{action_name}")

    @staticmethod
    def _require_actor(operator: str, reason: str) -> None:
        if not (operator or "").strip():
            raise DomainError("必须提供操作者")
        if not (reason or "").strip():
            raise DomainError("必须提供操作依据")


#: 兼容既有入口命名
Service = HazardService
