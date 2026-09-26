"""养老探访协作：领域契约、可控时钟、持久化与协调器。"""

from .clock import Clock, FakeClock, SystemClock
from .contracts import (
    ConsentScope,
    EscalationState,
    PlanStatus,
    ResidenceStatus,
    RiskBand,
    RiskStatus,
    Role,
    VisitOutcome,
    new_offline_token,
    validate_offline_token,
)
from .app import App
from .store import Storage

__all__ = [
    "App", "Storage", "Clock", "SystemClock", "FakeClock",
    "VisitOutcome", "RiskBand", "Role", "ConsentScope",
    "ResidenceStatus", "PlanStatus", "RiskStatus", "EscalationState",
    "validate_offline_token", "new_offline_token",
]
