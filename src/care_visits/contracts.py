"""探访计划、离线记录和风险升级共享的稳定值。"""

from enum import StrEnum
import re
import secrets


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


# 各风险等级对应的滚动探访频次（天）与升级时限（小时）。
RISK_VISIT_INTERVAL_DAYS = {
    RiskBand.ROUTINE: 7,
    RiskBand.CONCERN: 3,
    RiskBand.URGENT: 1,
}
ESCALATION_DEADLINE_HOURS = {
    RiskBand.CONCERN: 24,
    RiskBand.URGENT: 2,
}


class Role(StrEnum):
    """三级账号角色，决定可见范围与可执行动作。"""

    COUNTY = "county"        # 县级：全县，可关闭/导出/撤回授权
    TOWNSHIP = "township"    # 乡镇：本乡镇，可接手/转派/联系家属
    VILLAGER = "villager"    # 村级：本村，接单与到访上报


class ConsentScope(StrEnum):
    """老人可逐项授予或撤回的授权范围。"""

    VISIT = "visit"          # 上门探访（撤回后不得再排探访、不得再记录到访）
    RISK_CARE = "risk_care"  # 风险处置与升级联系（撤回后线索不再升级，仅留存审计）
    FAMILY_CONTACT = "family_contact"  # 联系家属


class ResidenceStatus(StrEnum):
    """老人居住状态：住院等临时变化会暂停滚动计划。"""

    HOME = "home"
    HOSPITALIZED = "hospitalized"
    MOVED_AWAY = "moved_away"


class PlanStatus(StrEnum):
    PENDING = "pending"      # 待接单
    ASSIGNED = "assigned"    # 已接单，服务人员持有
    DONE = "done"            # 已完成（到访/未遇/拒访/风险发现）
    CANCELLED = "cancelled"  # 因授权撤回/居住变化/重分配作废，仅保留审计
    MISSED = "missed"        # 过了应访日期仍未完成（每日漏访扫描标记）


class RiskStatus(StrEnum):
    OPEN = "open"            # 待处置
    ESCALATED = "escalated"  # 已在升级链中处置
    CLOSED = "closed"        # 已关闭（保留责任链）


class EscalationState(StrEnum):
    PENDING = "pending"      # 等待时限内处置
    TAKEN = "taken"          # 已被有权限人员接手
    TRANSFERRED = "transferred"  # 已转派，等待接手人
    TIMED_OUT = "timed_out"  # 超时自动升级
    CLOSED = "closed"        # 随风险关闭


_TOKEN_RE = re.compile(r"OFF-[A-Z0-9]{8}-[A-Z0-9]{8}")
_TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def validate_offline_token(value: str) -> str:
    """校验村级客户端生成的离线凭证。"""

    if not re.fullmatch(r"OFF-[A-Z0-9]{8}-[A-Z0-9]{8}", value):
        raise ValueError("离线凭证格式不正确")
    return value


def new_offline_token() -> str:
    """生成符合契约的随机离线凭证（村级客户端可独立生成同样格式的串）。"""

    def block() -> str:
        return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(8))

    return f"OFF-{block()}-{block()}"
