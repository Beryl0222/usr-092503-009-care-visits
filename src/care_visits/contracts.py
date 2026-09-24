"""探访计划、离线记录和风险升级共享的稳定值。"""

from enum import StrEnum
import re


class VisitOutcome(StrEnum):
    """线下探访可能记录的结果。"""

    COMPLETED = "completed"
    NOT_FOUND = "not_found"
    DECLINED = "declined"
    RISK_FOUND = "risk_found"


class RiskBand(StrEnum):
    """风险线索的处置等级。"""

    ROUTINE = "routine"
    CONCERN = "concern"
    URGENT = "urgent"


def validate_offline_token(value: str) -> str:
    """校验村级客户端生成的离线凭证。"""

    if not re.fullmatch(r"OFF-[A-Z0-9]{8}-[A-Z0-9]{8}", value):
        raise ValueError("离线凭证格式不正确")
    return value
