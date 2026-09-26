"""协调器使用的角色、授权范围与任务状态。"""

from enum import StrEnum


class Role(StrEnum):
    """系统内可识别的操作角色。"""

    COUNTY = "county"
    TOWNSHIP = "township"
    VILLAGE = "village"


class Scope(StrEnum):
    """老人可授予或撤回的数据访问范围。"""

    PROFILE = "profile"
    HEALTH = "health"
    FAMILY = "family"


class TaskStatus(StrEnum):
    """探访任务的生命周期状态。"""

    PLANNED = "planned"
    CLAIMED = "claimed"
    DONE = "done"
    CANCELLED = "cancelled"


class AlertStatus(StrEnum):
    """风险线索的生命周期状态。"""

    OPEN = "open"
    ACKED = "acked"
    CLOSED = "closed"


class AlertAction(StrEnum):
    """责任链中允许出现的处置动作。"""

    RAISE = "raise"
    ESCALATE = "escalate"
    ACK = "ack"
    REASSIGN = "reassign"
    CONTACT_FAMILY = "contact_family"
    CLOSE = "close"
