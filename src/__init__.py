"""积水隐患巡查领域包。"""
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
    Report,
    Severity,
    SubmitResult,
    TransitionResult,
)
from .service import HazardService, Service

__all__ = [
    "HazardService",
    "Service",
    "DomainError",
    "EventNotFoundError",
    "InvalidStateTransitionError",
    "IncompleteEventError",
    "EventStatus",
    "Severity",
    "LogAction",
    "Event",
    "Report",
    "EventLog",
    "EventDetail",
    "DispatchItem",
    "SubmitResult",
    "TransitionResult",
]
