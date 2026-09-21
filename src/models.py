"""积水隐患巡查领域模型：状态机、严重级别、处理时限与数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------

class DomainError(Exception):
    """领域错误基类。"""


class EventNotFoundError(DomainError):
    """事件编号不存在。"""


class InvalidStateTransitionError(DomainError):
    """当前状态不允许该操作。"""


class IncompleteEventError(InvalidStateTransitionError):
    """事件处于待补充状态（坐标精度不足或描述缺失），不能核实或派发。"""


# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

class EventStatus(str, Enum):
    """事件状态机。"""

    PENDING_SUPPLEMENT = "待补充"   # 坐标精度不足或描述缺失，不能派发
    PENDING = "待核实"             # 资料齐全，等待核实
    VERIFIED = "已核实"            # 已核实，待派单
    DISPATCHED = "处理中"          # 已转派责任部门
    CLOSED = "已关闭"              # 处置完成或无效上报
    MERGED = "已合并"              # 并入其他事件


#: 未闭环（仍占用调度资源）的状态
OPEN_STATUSES = (
    EventStatus.PENDING_SUPPLEMENT,
    EventStatus.PENDING,
    EventStatus.VERIFIED,
    EventStatus.DISPATCHED,
)


class Severity(IntEnum):
    """严重级别，数值越大越紧急。"""

    MINOR = 1      # 轻微
    MODERATE = 2   # 一般
    MAJOR = 3      # 较重
    SEVERE = 4     # 严重
    CRITICAL = 5   # 紧急


SEVERITY_LABELS = {
    Severity.MINOR: "轻微",
    Severity.MODERATE: "一般",
    Severity.MAJOR: "较重",
    Severity.SEVERE: "严重",
    Severity.CRITICAL: "紧急",
}

#: 处理时限（SLA）：按事件当前最高严重级别，自事件创建时间起算
SLA_BY_SEVERITY = {
    Severity.CRITICAL: timedelta(hours=2),
    Severity.SEVERE: timedelta(hours=4),
    Severity.MAJOR: timedelta(hours=8),
    Severity.MODERATE: timedelta(hours=24),
    Severity.MINOR: timedelta(hours=48),
}


class LogAction(str, Enum):
    """时间线动作类型。"""

    CREATE = "创建事件"
    ATTACH = "聚合上报"
    SUPPLEMENTED = "资料补齐"
    VERIFY = "核实"
    REASSIGN = "转派"
    MERGE = "合并"
    CLOSE = "关闭"
    REOPEN = "重新打开"


# ---------------------------------------------------------------------------
# 时间工具（统一 UTC，落库为 ISO8601 字符串）
# ---------------------------------------------------------------------------

def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value) -> datetime:
    """接受 datetime 或 ISO 字符串；naive 时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Report:
    """原始上报（证据），一旦写入不可变，聚合时完整保留。"""

    report_id: str            # 上报编号（幂等键，由上报端生成）
    event_id: str             # 聚合归属的事件编号
    reporter_id: str          # 网格员编号
    latitude: Optional[float]
    longitude: Optional[float]
    accuracy_m: Optional[float]   # 坐标精度（米），缺失或过大视为精度不足
    reported_at: datetime     # 现场上报时间
    received_at: datetime     # 服务接收时间
    description: str          # 现场描述
    image_digest: Optional[str]   # 图片摘要（哈希/指纹）
    severity: Severity


@dataclass
class Event:
    """隐患事件：同一地点短时重复上报的聚合体。"""

    event_id: str
    status: EventStatus
    severity: Severity            # 所有上报中的最高级别
    latitude: Optional[float]     # 规范坐标（取自首条精度达标的上报）
    longitude: Optional[float]
    department: Optional[str]     # 当前责任部门
    merged_into: Optional[str]    # 已合并时指向目标事件
    created_at: datetime
    updated_at: datetime
    last_report_at: datetime      # 最近一次上报时间（聚合时间窗据此滑动）

    @property
    def deadline(self) -> datetime:
        """处理时限 = 创建时间 + 当前严重级别对应的 SLA。"""
        return self.created_at + SLA_BY_SEVERITY[self.severity]

    @property
    def dispatchable(self) -> bool:
        """待补充事件不能直接派发。"""
        return self.status in OPEN_STATUSES and self.status != EventStatus.PENDING_SUPPLEMENT


@dataclass
class EventLog:
    """时间线条目：每次状态变化都记录操作者与依据。"""

    log_id: str
    event_id: str
    action: LogAction
    operator: str      # 操作者（网格员/调度员/system）
    reason: str        # 操作依据
    details: dict      # 附加上下文（JSON）
    created_at: datetime


@dataclass
class SubmitResult:
    """上报受理结果。deduplicated=True 表示命中幂等，返回既有记录。"""

    report: Report
    event: Event
    deduplicated: bool = False


@dataclass
class TransitionResult:
    """状态变更结果。deduplicated=True 表示相同操作编号重复提交，返回首次结果。"""

    event: Event
    op_id: str
    deduplicated: bool = False
    target_event: Optional[Event] = None   # 仅合并操作使用


@dataclass
class DispatchItem:
    """调度端视图：未处理隐患及其责任部门与处理时限。"""

    event_id: str
    status: EventStatus
    severity: Severity
    department: Optional[str]
    deadline: datetime
    is_overdue: bool
    dispatchable: bool
    report_count: int
    latitude: Optional[float]
    longitude: Optional[float]
    created_at: datetime
    last_report_at: datetime


@dataclass
class EventDetail:
    """事件完整档案：本体 + 全部原始证据 + 完整时间线。"""

    event: Event
    reports: list = field(default_factory=list)
    timeline: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 序列化（供幂等结果快照与存储层使用）
# ---------------------------------------------------------------------------

def event_to_dict(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "status": event.status.value,
        "severity": int(event.severity),
        "latitude": event.latitude,
        "longitude": event.longitude,
        "department": event.department,
        "merged_into": event.merged_into,
        "created_at": to_iso(event.created_at),
        "updated_at": to_iso(event.updated_at),
        "last_report_at": to_iso(event.last_report_at),
    }


def event_from_dict(data: dict) -> Event:
    return Event(
        event_id=data["event_id"],
        status=EventStatus(data["status"]),
        severity=Severity(int(data["severity"])),
        latitude=data["latitude"],
        longitude=data["longitude"],
        department=data["department"],
        merged_into=data["merged_into"],
        created_at=parse_dt(data["created_at"]),
        updated_at=parse_dt(data["updated_at"]),
        last_report_at=parse_dt(data["last_report_at"]),
    )
