"""服务端协调器：滚动探访计划、离线幂等合并、风险升级、责任链与监管导出。

所有计时均来自注入的 Clock（生产 SystemClock / 测试 FakeClock），所有状态均落 SQLite，
进程重启后升级计时、计划与责任链不丢失。
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .clock import Clock
from .contracts import (
    ESCALATION_DEADLINE_HOURS,
    RISK_VISIT_INTERVAL_DAYS,
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
from .errors import (
    AuthError,
    ConflictError,
    ConsentError,
    NotFoundError,
    PermissionError_,
    ValidationError_,
)
from .store import Storage

# 升级时限（可控时钟计时）：乡镇首段时限取自领域契约；未处置则自动升县级，
# 县级再超时继续催办（每次超时都在责任链中留痕）。
_LEVEL_DEADLINE_HOURS = {
    ("township", RiskBand.URGENT): ESCALATION_DEADLINE_HOURS[RiskBand.URGENT],
    ("township", RiskBand.CONCERN): ESCALATION_DEADLINE_HOURS[RiskBand.CONCERN],
    ("county", RiskBand.URGENT): 2,
    ("county", RiskBand.CONCERN): 12,
}


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError_(f"时间格式不正确: {value!r}，应为 ISO 8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError_(f"日期格式不正确: {value!r}，应为 YYYY-MM-DD") from exc


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class App:
    def __init__(self, store: Storage, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    # ---------------------------------------------------------------- 基础资料

    def create_agency(self, actor: dict, agency_id: str, name: str) -> dict:
        self._require_role(actor, Role.COUNTY)
        now = self.clock.now()
        with self.store.begin() as c:
            try:
                c.execute("INSERT INTO agencies(id, name, created_at) VALUES(?,?,?)",
                          (agency_id, name, _ts(now)))
            except Exception as exc:
                raise ConflictError("机构已存在") from exc
        return {"id": agency_id, "name": name, "active": True}

    def deactivate_agency(self, actor: dict, agency_id: str, reason: str) -> dict:
        """机构退出：机构失效，其全部人员失效，手上的计划取消并进入重分配。"""
        self._require_role(actor, Role.COUNTY)
        now = self.clock.now()
        with self.store.begin() as c:
            row = c.execute("SELECT * FROM agencies WHERE id=?", (agency_id,)).fetchone()
            if row is None:
                raise NotFoundError("机构不存在")
            if not row["active"]:
                raise ConflictError("机构已退出")
            c.execute("UPDATE agencies SET active=0, deactivated_at=? WHERE id=?",
                      (_ts(now), agency_id))
            c.execute("UPDATE workers SET active=0, deactivated_at=? WHERE agency_id=? AND active=1",
                      (_ts(now), agency_id))
            workers = c.execute("SELECT id FROM workers WHERE agency_id=?", (agency_id,)).fetchall()
            cancelled = 0
            for w in workers:
                cancelled += self._cancel_live_plans(
                    c, worker_id=w["id"], reason="agency_exit", at=now, detail=reason)
        self.generate_plans()
        return {"agency_id": agency_id, "cancelled_plans": cancelled}

    def create_worker(
        self, actor: Optional[dict], worker_id: str, name: str, role: str,
        *, town: Optional[str] = None, village: Optional[str] = None,
        agency_id: Optional[str] = None, daily_capacity: int = 6,
        token: Optional[str] = None,
    ) -> dict:
        # actor=None 仅用于引导首个县级账号。
        if actor is not None:
            self._require_role(actor, Role.COUNTY)
        role = self._enum_value(Role, role, "角色")
        if role == Role.VILLAGER and not (town and village):
            raise ValidationError_("村级人员必须指定乡镇与村")
        if role != Role.VILLAGER and not town:
            raise ValidationError_("县乡账号必须指定乡镇（县级填本县代码）")
        if daily_capacity < 0:
            raise ValidationError_("日承载量不能为负")
        token = token or ("TOK-" + new_offline_token()[4:])
        now = self.clock.now()
        with self.store.begin() as c:
            if agency_id is not None and c.execute(
                "SELECT 1 FROM agencies WHERE id=? AND active=1", (agency_id,)
            ).fetchone() is None:
                raise ValidationError_("服务机构不存在或已退出")
            try:
                c.execute(
                    """INSERT INTO workers(id, name, role, town, village, agency_id,
                           daily_capacity, token, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (worker_id, name, role, town, village, agency_id,
                     daily_capacity, token, _ts(now)))
            except Exception as exc:
                raise ConflictError("账号或令牌已存在") from exc
        return {"id": worker_id, "name": name, "role": role, "token": token}

    def deactivate_worker(self, actor: dict, worker_id: str, reason: str) -> dict:
        """人员失效（请假/离岗）：未完成计划取消并重新分配给有容量的人。"""
        self._require_role(actor, Role.COUNTY)
        now = self.clock.now()
        with self.store.begin() as c:
            row = c.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
            if row is None:
                raise NotFoundError("人员不存在")
            if not row["active"]:
                raise ConflictError("人员已失效")
            c.execute("UPDATE workers SET active=0, deactivated_at=? WHERE id=?",
                      (_ts(now), worker_id))
            cancelled = self._cancel_live_plans(
                c, worker_id=worker_id, reason="worker_inactive", at=now, detail=reason)
        self.generate_plans()
        return {"worker_id": worker_id, "cancelled_plans": cancelled}

    def create_elder(
        self, actor: dict, elder_id: str, name: str, town: str, village: str,
        risk_band: str = RiskBand.ROUTINE, *, scopes: Optional[list[str]] = None,
    ) -> dict:
        self._require_role(actor, Role.COUNTY)
        band = self._enum_value(RiskBand, risk_band, "风险等级")
        now = self.clock.now()
        with self.store.begin() as c:
            try:
                c.execute(
                    """INSERT INTO elders(id, name, town, village, risk_band, residence_status, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (elder_id, name, town, village, band, ResidenceStatus.HOME, _ts(now)))
            except Exception as exc:
                raise ConflictError("老人已登记") from exc
            for scope in (scopes or [s.value for s in ConsentScope]):
                self._write_consent(c, elder_id, scope, True, actor["id"], "建档初始授权", now)
        self.generate_plans()
        return {"id": elder_id, "name": name, "risk_band": band}

    def set_risk_band(self, actor: dict, elder_id: str, band: str) -> dict:
        self._require_role(actor, Role.COUNTY)
        band = self._enum_value(RiskBand, band, "风险等级")
        with self.store.begin() as c:
            if c.execute("SELECT 1 FROM elders WHERE id=?", (elder_id,)).fetchone() is None:
                raise NotFoundError("老人不存在")
            c.execute("UPDATE elders SET risk_band=? WHERE id=?", (band, elder_id))
        self.generate_plans()
        return {"elder_id": elder_id, "risk_band": band}

    def set_consent(self, actor: dict, elder_id: str, scope: str, granted: bool,
                    reason: str = "") -> dict:
        """授权变更立即生效；撤回探访授权会取消未完成计划。审计行只追加不删除。"""
        self._require_role(actor, Role.COUNTY)
        scope = self._enum_value(ConsentScope, scope, "授权范围")
        now = self.clock.now()
        with self.store.begin() as c:
            if c.execute("SELECT 1 FROM elders WHERE id=?", (elder_id,)).fetchone() is None:
                raise NotFoundError("老人不存在")
            self._write_consent(c, elder_id, scope, granted, actor["id"],
                                reason or ("授权" if granted else "撤回授权"), now)
            if not granted and scope == ConsentScope.VISIT:
                self._cancel_live_plans(
                    c, elder_id=elder_id, reason="consent_visit_withdrawn", at=now, detail=reason)
        if granted:
            self.generate_plans()
        return {"elder_id": elder_id, "scope": scope, "granted": granted}

    def set_residence(self, actor: dict, elder_id: str, status: str, reason: str = "") -> dict:
        """居住变化：住院/搬走暂停计划，回到居家后下一轮滚动立即补排。"""
        self._require_role(actor, Role.COUNTY)
        status = self._enum_value(ResidenceStatus, status, "居住状态")
        now = self.clock.now()
        with self.store.begin() as c:
            if c.execute("SELECT 1 FROM elders WHERE id=?", (elder_id,)).fetchone() is None:
                raise NotFoundError("老人不存在")
            c.execute("UPDATE elders SET residence_status=? WHERE id=?", (status, elder_id))
            if status != ResidenceStatus.HOME:
                self._cancel_live_plans(
                    c, elder_id=elder_id,
                    reason="hospitalized" if status == ResidenceStatus.HOSPITALIZED else "moved_away",
                    at=now, detail=reason)
        if status == ResidenceStatus.HOME:
            self.generate_plans()
        return {"elder_id": elder_id, "residence_status": status}

    def add_holiday(self, actor: dict, day: str, name: str) -> dict:
        self._require_role(actor, Role.COUNTY)
        d = _day(day)
        with self.store.begin() as c:
            c.execute("INSERT OR REPLACE INTO holidays(day, name) VALUES(?,?)",
                      (d.isoformat(), name))
        return {"day": d.isoformat(), "name": name}

    # ---------------------------------------------------------------- 滚动计划

    def generate_plans(self, on_day: Optional[date] = None) -> list[dict]:
        """为每位应访老人滚动生成当日计划，节假日顺延到下一工作日。

        已存在当日未结束计划、住院/搬走、撤回探访授权者不排。容量按村级人员
        daily_capacity 均衡；本村无容量时尝试本乡其他村，仍无则留空待乡镇派单。
        """
        now = self.clock.now()
        today = on_day or now.date()
        created: list[dict] = []
        with self.store.begin() as c:
            elders = c.execute(
                "SELECT * FROM elders WHERE residence_status=? ORDER BY town, village, id",
                (ResidenceStatus.HOME,),
            ).fetchall()
            for e in elders:
                if not self._consent_granted(c, e["id"], ConsentScope.VISIT, now):
                    continue
                target = today
                while self._is_holiday(c, target):
                    target = target + timedelta(days=1)
                live = c.execute(
                    "SELECT id FROM plans WHERE elder_id=? AND due_date=? AND status IN (?,?)",
                    (e["id"], target.isoformat(), PlanStatus.PENDING, PlanStatus.ASSIGNED),
                ).fetchone()
                if live is not None:
                    continue
                band = RiskBand(e["risk_band"])
                interval = RISK_VISIT_INTERVAL_DAYS[band]
                last = c.execute(
                    """SELECT occurred_at FROM visit_records
                       WHERE elder_id=? AND merged_status IN ('applied','applied_unlinked')
                       ORDER BY occurred_at DESC, id DESC LIMIT 1""",
                    (e["id"],),
                ).fetchone()
                if last is not None:
                    last_day = _parse_dt(last["occurred_at"]).date()
                    if last_day + timedelta(days=interval) > target:
                        continue
                assignee = self._pick_assignee(c, e, target)
                plan_id = _new_id("plan")
                event_id = self._event(
                    c, now, "plan_created", e["id"], plan_id, "scheduled",
                    f"risk={band} interval={interval}d assignee={assignee or 'UNASSIGNED'} target={target}")
                c.execute(
                    """INSERT INTO plans(id, elder_id, due_date, interval_days, status,
                           assignee_id, agency_id, created_at, generation_event_id)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (plan_id, e["id"], target.isoformat(), interval, PlanStatus.PENDING,
                     assignee, self._agency_of(c, assignee), _ts(now), event_id))
                created.append(self._plan_dict(c, plan_id))
        return created

    def list_plans(self, actor: dict, *, status: Optional[str] = None,
                   mine: bool = False) -> list[dict]:
        sql = """SELECT p.* FROM plans p JOIN elders e ON e.id=p.elder_id WHERE 1=1"""
        args: list[Any] = []
        if actor["role"] == Role.VILLAGER:
            sql += " AND p.assignee_id=?"
            args.append(actor["id"])
        elif actor["role"] == Role.TOWNSHIP:
            sql += " AND e.town=?"
            args.append(actor["town"])
        if status:
            sql += " AND p.status=?"
            args.append(status)
        if mine:
            sql += " AND p.assignee_id=?"
            args.append(actor["id"])
        sql += " ORDER BY p.due_date, p.id"
        with self.store.read() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def reassign_plan(self, actor: dict, plan_id: str, to_worker_id: str,
                      reason: str = "") -> dict:
        """乡镇/县级把无人承接或承接人失效的计划改派给他人。"""
        self._require_role(actor, (Role.TOWNSHIP, Role.COUNTY))
        now = self.clock.now()
        with self.store.begin() as c:
            plan = self._get_plan(c, plan_id)
            self._require_town(actor, self._elder_town(c, plan["elder_id"]))
            if plan["status"] not in (PlanStatus.PENDING, PlanStatus.ASSIGNED):
                raise ConflictError("该计划已结束，不能改派")
            target = c.execute(
                "SELECT * FROM workers WHERE id=? AND active=1 AND role=?",
                (to_worker_id, Role.VILLAGER)).fetchone()
            if target is None:
                raise ValidationError_("目标人员不存在、已失效或不是村级人员")
            self._require_town(actor, target["town"])
            old = plan["assignee_id"]
            c.execute("UPDATE plans SET assignee_id=?, agency_id=?, status=? WHERE id=?",
                      (to_worker_id, target["agency_id"], PlanStatus.PENDING, plan_id))
            self._event(c, now, "plan_reassigned", plan["elder_id"], plan_id,
                        "manual_reassign", f"{old} -> {to_worker_id}: {reason}")
            return self._plan_dict(c, plan_id)

    def claim_plan(self, actor: dict, plan_id: str) -> dict:
        """并发接单：BEGIN IMMEDIATE 下条件 UPDATE，全局唯一赢家。"""
        self._require_role(actor, Role.VILLAGER)
        now = self.clock.now()
        with self.store.begin() as c:
            plan = self._get_plan(c, plan_id)
            elder = c.execute("SELECT * FROM elders WHERE id=?", (plan["elder_id"],)).fetchone()
            if actor["town"] != elder["town"]:
                raise PermissionError_("不能接外乡镇的计划（本乡内可由乡镇统筹跨村支援）")
            if not self._consent_granted(c, elder["id"], ConsentScope.VISIT, now):
                raise ConsentError("探访授权已撤回，不能接单")
            if elder["residence_status"] != ResidenceStatus.HOME:
                raise ConflictError("老人当前不在居家状态")
            cur = c.execute(
                """UPDATE plans SET status=?, assignee_id=?, agency_id=?, assigned_at=?
                   WHERE id=? AND status IN (?,?)
                     AND (assignee_id=? OR assignee_id IS NULL)""",
                (PlanStatus.ASSIGNED, actor["id"], actor["agency_id"], _ts(now),
                 plan_id, PlanStatus.PENDING, PlanStatus.ASSIGNED, actor["id"]))
            if cur.rowcount == 0:
                raise ConflictError("计划已被其他人接走或已结束")
            self._event(c, now, "plan_claimed", elder["id"], plan_id, "claimed", actor["id"])
            return self._plan_dict(c, plan_id)

    # ---------------------------------------------------------------- 离线记录

    def submit_visit(
        self, actor: dict, *, token: str, outcome: str, occurred_at: str,
        plan_id: Optional[str] = None, elder_id: Optional[str] = None, note: str = "",
        risk_band: Optional[str] = None, risk_description: str = "",
    ) -> dict:
        """合并一条（可能离线产生的）到访记录。

        - token 全局唯一：重复上报原样返回首次结果，不产生第二条记录、不重复完成任务；
        - 以 occurred_at（实际发生时间）为准落库，恢复连接后乱序送达不改变事实顺序；
        - 发生当时授权已撤回/人员已离场/机构已退出：记录留存为 rejected 审计，
          但不完成计划、不升级。拒绝记录先提交再返回错误，避免被外层回滚；
        - 计划在离线期间被取消/改派时，可只给 elder_id，事实以 unlinked 保留并计入频次。
        """
        self._require_role(actor, Role.VILLAGER)
        try:
            validate_offline_token(token)
        except ValueError as exc:
            raise ValidationError_(str(exc)) from exc
        outcome = self._enum_value(VisitOutcome, outcome, "探访结果")
        happened = _parse_dt(occurred_at)
        now = self.clock.now()
        if happened > now + timedelta(minutes=5):
            raise ValidationError_("到访时间不能晚于当前时间")
        if len(note) > 500:
            raise ValidationError_("备注过长")
        band = self._enum_value(RiskBand, risk_band, "风险等级") if risk_band else None
        if outcome == VisitOutcome.RISK_FOUND and band is None:
            raise ValidationError_("风险发现必须给出风险等级")

        # 先查幂等：重复凭证不做任何写操作。
        with self.store.read() as c:
            existing = c.execute("SELECT * FROM visit_records WHERE token=?", (token,)).fetchone()
        if existing is not None:
            return {"duplicate": True, "merged_status": existing["merged_status"],
                    "visit_id": existing["id"], "plan_id": existing["plan_id"]}

        tx = self.store.begin()
        with tx as c:
            # 双重检查（两个并发请求可能同时通过上面的只读查询）。
            existing2 = c.execute("SELECT * FROM visit_records WHERE token=?", (token,)).fetchone()
            if existing2 is not None:
                return {"duplicate": True, "merged_status": existing2["merged_status"],
                        "visit_id": existing2["id"], "plan_id": existing2["plan_id"]}

            plan = None
            if plan_id:
                plan = self._get_plan(c, plan_id)
                elder = c.execute("SELECT * FROM elders WHERE id=?", (plan["elder_id"],)).fetchone()
                if actor["town"] != elder["town"]:
                    raise PermissionError_("只能上报本乡镇老人的到访")
            else:
                rows = c.execute(
                    """SELECT p.* FROM plans p JOIN elders e ON e.id=p.elder_id
                       WHERE e.village=? AND p.due_date=?
                         AND p.status IN (?,?) AND (p.assignee_id=? OR p.assignee_id IS NULL)
                       ORDER BY p.id""",
                    (actor["village"], happened.date().isoformat(),
                     PlanStatus.PENDING, PlanStatus.ASSIGNED, actor["id"])).fetchall()
                if rows:
                    plan = rows[0]
                    elder = c.execute("SELECT * FROM elders WHERE id=?", (plan["elder_id"],)).fetchone()
                elif elder_id:
                    # 无有效计划（离线期间已重分配/取消）：按老人登记事实。
                    elder = c.execute(
                        "SELECT * FROM elders WHERE id=? AND town=?",
                        (elder_id, actor["town"])).fetchone()
                    if elder is None:
                        raise NotFoundError("老人不存在或不属于本乡镇")
            if elder is None:
                raise NotFoundError("找不到匹配的探访计划或老人（请提供 plan_id 或 elder_id）")

            rejection = None
            if actor["agency_id"] and not self._active_at(c, "agencies", actor["agency_id"], happened):
                rejection = "rejected_agency_exited"
            elif not self._active_at(c, "workers", actor["id"], happened):
                rejection = "rejected_worker_inactive"
            elif not self._consent_granted_at(c, elder["id"], ConsentScope.VISIT, happened):
                rejection = "rejected_consent_withdrawn"

            if rejection:
                cur = c.execute(
                    """INSERT INTO visit_records(token, plan_id, elder_id, worker_id, agency_id,
                           outcome, note, occurred_at, received_at, merged_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (token, plan["id"] if plan is not None else None,
                     elder["id"], actor["id"], actor["agency_id"],
                     outcome, note, _ts(happened), _ts(now), rejection))
                self._event(c, now, "visit_rejected", elder["id"],
                            plan["id"] if plan is not None else None,
                            rejection, f"token={token}")
                # 审计必须落库：先提交，再以 403 告知调用方。
                tx.commit_keep_error()
                raise ConsentError("该记录发生时探访授权或服务资格已不存在，仅留存审计",
                                   code=rejection)

            if plan is not None and plan["status"] in (PlanStatus.PENDING, PlanStatus.ASSIGNED):
                if plan["assignee_id"] not in (actor["id"], None):
                    merged_status = "applied_unlinked"
                else:
                    c.execute(
                        "UPDATE plans SET status=?, completed_at=? WHERE id=? AND status IN (?,?)",
                        (PlanStatus.DONE, _ts(happened), plan["id"],
                         PlanStatus.PENDING, PlanStatus.ASSIGNED))
                    merged_status = "applied"
            elif plan is not None and plan["status"] == PlanStatus.DONE:
                # 计划已完成：到访事实留痕，不重复完成任务。
                merged_status = "recorded_no_plan"
            else:
                # 无计划或计划已因重分配/住院/撤权取消（典型：离线期间被改派）：
                # 事实保留、计入频次，但不复活旧计划。
                merged_status = "applied_unlinked"

            cur = c.execute(
                """INSERT INTO visit_records(token, plan_id, elder_id, worker_id, agency_id,
                       outcome, note, occurred_at, received_at, merged_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (token, plan["id"] if plan is not None else None,
                 elder["id"], actor["id"], actor["agency_id"],
                 outcome, note, _ts(happened), _ts(now), merged_status))
            visit_id = cur.lastrowid
            self._event(c, now, "visit_merged", elder["id"],
                        plan["id"] if plan is not None else None,
                        merged_status, f"token={token} outcome={outcome}")

            risk_id: Optional[str] = None
            if outcome == VisitOutcome.RISK_FOUND:
                risk_id = self._open_risk(
                    c, elder=elder, band=band, description=risk_description or note,
                    actor=actor, happened=happened,
                    plan_id=plan["id"] if plan is not None else None, visit_id=visit_id)

            return {"duplicate": False, "merged_status": merged_status,
                    "visit_id": visit_id,
                    "plan_id": plan["id"] if plan is not None else None,
                    "risk_id": risk_id}

    # ---------------------------------------------------------------- 风险与升级

    def list_risks(self, actor: dict, *, status: Optional[str] = None) -> list[dict]:
        sql = "SELECT r.* FROM risks r JOIN elders e ON e.id=r.elder_id WHERE 1=1"
        args: list[Any] = []
        if actor["role"] == Role.VILLAGER:
            sql += " AND r.created_by=?"
            args.append(actor["id"])
        elif actor["role"] == Role.TOWNSHIP:
            sql += " AND e.town=?"
            args.append(actor["town"])
        if status:
            sql += " AND r.status=?"
            args.append(status)
        sql += " ORDER BY r.created_at"
        with self.store.read() as c:
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
            for r in rows:
                esc = self._latest_escalation_row(c, r["id"])
                r["escalation"] = dict(esc) if esc else None
        return rows

    def get_risk(self, actor: dict, risk_id: str) -> dict:
        with self.store.read() as c:
            risk = c.execute("SELECT r.*, e.town AS elder_town FROM risks r "
                             "JOIN elders e ON e.id=r.elder_id WHERE r.id=?",
                             (risk_id,)).fetchone()
            if risk is None:
                raise NotFoundError("风险线索不存在")
            self._require_town(actor, risk["elder_town"],
                               allow_villager_creator=True, creator=risk["created_by"])
            escs = [dict(r) for r in c.execute(
                "SELECT * FROM escalations WHERE risk_id=? ORDER BY opened_at, rowid", (risk_id,))]
            chain = [dict(r) for r in c.execute(
                "SELECT * FROM chain_events WHERE risk_id=? ORDER BY id", (risk_id,))]
            result = {**dict(risk), "escalations": escs, "chain": chain}
        return result

    def take_risk(self, actor: dict, risk_id: str, note: str = "") -> dict:
        self._require_role(actor, (Role.TOWNSHIP, Role.COUNTY))
        now = self.clock.now()
        with self.store.begin() as c:
            risk, esc = self._risk_and_open_escalation(c, risk_id)
            self._require_town(actor, self._elder_town(c, risk["elder_id"]))
            self._require_level(actor, esc["level"])
            if esc["state"] not in (EscalationState.PENDING, EscalationState.TRANSFERRED,
                                    EscalationState.TIMED_OUT):
                raise ConflictError("该升级事项已被接手或关闭")
            c.execute("UPDATE escalations SET state=?, owner_id=? WHERE id=?",
                      (EscalationState.TAKEN, actor["id"], esc["id"]))
            c.execute("UPDATE risks SET status=? WHERE id=?", (RiskStatus.ESCALATED, risk_id))
            self._chain(c, risk_id, esc["id"], actor["id"], "take", None, actor["id"], note, now)
            return self._risk_snapshot(c, risk_id)

    def transfer_risk(self, actor: dict, risk_id: str, to_worker_id: str,
                      note: str = "") -> dict:
        """转派不改 SLA 截止时间，避免靠转派拖延时限；转县级即升级。"""
        self._require_role(actor, (Role.TOWNSHIP, Role.COUNTY))
        now = self.clock.now()
        with self.store.begin() as c:
            risk, esc = self._risk_and_open_escalation(c, risk_id)
            self._require_town(actor, self._elder_town(c, risk["elder_id"]))
            target = c.execute("SELECT * FROM workers WHERE id=? AND active=1",
                               (to_worker_id,)).fetchone()
            if target is None:
                raise NotFoundError("接手人不存在或已失效")
            if target["role"] not in (Role.TOWNSHIP.value, Role.COUNTY.value):
                raise ValidationError_("只能转派给乡镇或县级人员")
            new_level = esc["level"]
            if target["role"] == Role.COUNTY and esc["level"] == "township":
                new_level = "county"
            elif target["role"] == Role.TOWNSHIP and target["town"] != self._elder_town(c, risk["elder_id"]):
                raise PermissionError_("不能转派给外乡镇人员")
            c.execute("UPDATE escalations SET state=?, owner_id=?, level=? WHERE id=?",
                      (EscalationState.TAKEN, to_worker_id, new_level, esc["id"]))
            c.execute("UPDATE risks SET status=? WHERE id=?", (RiskStatus.ESCALATED, risk_id))
            self._chain(c, risk_id, esc["id"], actor["id"], "transfer",
                        esc["owner_id"], to_worker_id, note, now)
            if new_level != esc["level"]:
                self._event(c, now, "escalation_level_up", risk["elder_id"], None,
                            "transfer_to_county", risk_id)
            return self._risk_snapshot(c, risk_id)

    def contact_family(self, actor: dict, risk_id: str, note: str = "") -> dict:
        self._require_role(actor, (Role.TOWNSHIP, Role.COUNTY))
        now = self.clock.now()
        with self.store.begin() as c:
            risk = c.execute("SELECT * FROM risks WHERE id=?", (risk_id,)).fetchone()
            if risk is None or risk["status"] == RiskStatus.CLOSED:
                raise NotFoundError("风险线索不存在或已关闭")
            self._require_town(actor, self._elder_town(c, risk["elder_id"]))
            if not self._consent_granted(c, risk["elder_id"], ConsentScope.FAMILY_CONTACT, now):
                raise ConsentError("联系家属授权已撤回")
            esc = self._latest_escalation_row(c, risk_id)
            self._chain(c, risk_id, esc["id"] if esc else None, actor["id"],
                        "contact_family", None, None, note, now)
            return self._risk_snapshot(c, risk_id)

    def close_risk(self, actor: dict, risk_id: str, note: str = "") -> dict:
        self._require_role(actor, (Role.TOWNSHIP, Role.COUNTY))
        now = self.clock.now()
        with self.store.begin() as c:
            risk = c.execute("SELECT * FROM risks WHERE id=?", (risk_id,)).fetchone()
            if risk is None:
                raise NotFoundError("风险线索不存在")
            if risk["status"] == RiskStatus.CLOSED:
                raise ConflictError("风险线索已关闭")
            self._require_town(actor, self._elder_town(c, risk["elder_id"]))
            esc = self._latest_escalation_row(c, risk_id)
            if actor["role"] == Role.TOWNSHIP:
                if esc is None or esc["owner_id"] != actor["id"] or esc["state"] != EscalationState.TAKEN:
                    raise PermissionError_("乡镇人员只能关闭自己已接手的线索")
            c.execute("UPDATE risks SET status=?, closed_at=?, closed_by=? WHERE id=?",
                      (RiskStatus.CLOSED, _ts(now), actor["id"], risk_id))
            # 只关闭进行中的那一级；已超时的历史级别保留 timed_out 作为监管证据。
            c.execute(
                "UPDATE escalations SET state=? WHERE risk_id=? AND state IN (?,?,?)",
                (EscalationState.CLOSED, risk_id,
                 EscalationState.PENDING, EscalationState.TRANSFERRED, EscalationState.TAKEN))
            self._chain(c, risk_id, esc["id"] if esc else None, actor["id"],
                        "close", None, None, note, now)
            return self._risk_snapshot(c, risk_id)

    def sweep_due(self) -> list[dict]:
        """检查超过时限未处置的升级事项，自动升级。重启后随时可补跑、不重不漏。"""
        now = self.clock.now()
        fired: list[dict] = []
        with self.store.begin() as c:
            rows = c.execute(
                "SELECT * FROM escalations WHERE state IN (?,?) AND deadline_at<=? ORDER BY deadline_at",
                (EscalationState.PENDING, EscalationState.TRANSFERRED, _ts(now))).fetchall()
            for esc in rows:
                c.execute("UPDATE escalations SET state=?, timed_out_at=? WHERE id=?",
                          (EscalationState.TIMED_OUT, _ts(now), esc["id"]))
                risk = c.execute("SELECT * FROM risks WHERE id=?", (esc["risk_id"],)).fetchone()
                self._chain(c, risk["id"], esc["id"], "system", "timeout", esc["owner_id"], None,
                            f"{esc['level']} 级未在 {esc['deadline_at']} 前处置", now)
                new_level = "county"
                hours = _LEVEL_DEADLINE_HOURS[(new_level, RiskBand(risk["band"]))]
                new_id = self._open_escalation(
                    c, risk, new_level, now, timedelta(hours=hours), cause="timeout")
                fired.append({"risk_id": risk["id"], "from_level": esc["level"],
                              "to_level": new_level, "new_escalation_id": new_id})
        return fired

    # ---------------------------------------------------------------- 漏访与导出

    def run_daily_scan(self, on_day: Optional[date] = None) -> dict:
        """每日滚动：先生成当日计划，再把过期未完成计划标记漏访（审计保留），再催办超时升级。"""
        now = self.clock.now()
        today = on_day or now.date()
        created = self.generate_plans(today)
        missed: list[dict] = []
        with self.store.begin() as c:
            rows = c.execute("SELECT * FROM plans WHERE status IN (?,?) AND due_date<?",
                             (PlanStatus.PENDING, PlanStatus.ASSIGNED, today.isoformat())).fetchall()
            for p in rows:
                c.execute("UPDATE plans SET status=?, missed_at=? WHERE id=?",
                          (PlanStatus.MISSED, _ts(now), p["id"]))
                self._event(c, now, "plan_missed", p["elder_id"], p["id"], "missed",
                            f"due={p['due_date']} 未完成")
                missed.append(p["id"])
        fired = self.sweep_due()
        return {"date": today.isoformat(), "created": len(created),
                "missed": len(missed), "missed_plan_ids": missed, "escalations_fired": fired}

    def regulatory_export(self, actor: dict, start: str, end: str) -> str:
        """监管台账 CSV（UTF-8 BOM，Excel 可直接打开）：含取消/漏访与超时升级全过程。"""
        self._require_role(actor, Role.COUNTY)
        d1, d2 = _day(start), _day(end)
        if d2 < d1:
            raise ValidationError_("结束日期不能早于开始日期")
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "计划ID", "老人ID", "姓名", "乡镇", "村", "风险等级", "应访日期",
            "计划状态", "承接人", "承接机构", "探访结果", "到访发生时间", "上报时间",
            "合并状态", "风险ID", "线索等级", "线索状态", "升级级别",
            "升级状态", "升级截止时间", "超时时间",
        ])
        with self.store.read() as c:
            plans = c.execute(
                """SELECT p.*, e.name AS elder_name, e.town AS t_town, e.village AS t_village,
                          e.risk_band AS e_band, w.id AS worker_id, a.name AS agency_name
                   FROM plans p
                   JOIN elders e ON e.id=p.elder_id
                   LEFT JOIN workers w ON w.id=p.assignee_id
                   LEFT JOIN agencies a ON a.id=p.agency_id
                   WHERE p.due_date BETWEEN ? AND ?
                   ORDER BY p.due_date, p.id""",
                (d1.isoformat(), d2.isoformat())).fetchall()
            for p in plans:
                visits = c.execute("SELECT * FROM visit_records WHERE plan_id=? ORDER BY id",
                                   (p["id"],)).fetchall()
                risk = c.execute("SELECT * FROM risks WHERE source_plan_id=? ORDER BY created_at LIMIT 1",
                                 (p["id"],)).fetchone()
                escs = []
                if risk:
                    escs = c.execute(
                        "SELECT * FROM escalations WHERE risk_id=? ORDER BY opened_at, rowid",
                        (risk["id"],)).fetchall()
                if not visits:
                    writer.writerow(self._csv_row(p, None, risk, escs))
                else:
                    for v in visits:
                        writer.writerow(self._csv_row(p, v, risk, escs))
            extra = c.execute(
                """SELECT v.*, e.name AS elder_name, e.town AS t_town, e.village AS t_village,
                          e.risk_band AS e_band
                   FROM visit_records v JOIN elders e ON e.id=v.elder_id
                   WHERE v.plan_id IS NULL
                     AND substr(v.occurred_at,1,10) BETWEEN ? AND ?
                   ORDER BY v.occurred_at""",
                (d1.isoformat(), d2.isoformat())).fetchall()
            for v in extra:
                risk = c.execute(
                    "SELECT * FROM risks WHERE source_visit_id=? ORDER BY created_at LIMIT 1",
                    (v["id"],)).fetchone()
                writer.writerow([
                    "", v["elder_id"], v["elder_name"], v["t_town"], v["t_village"], v["e_band"],
                    "", "unlinked", v["worker_id"], "", v["outcome"],
                    v["occurred_at"], v["received_at"], v["merged_status"],
                    risk["id"] if risk else "", risk["band"] if risk else "",
                    risk["status"] if risk else "", "", "", "", "",
                ])
        return "﻿" + out.getvalue()

    # ---------------------------------------------------------------- 内部方法

    def authenticate(self, token: str) -> dict:
        if not token:
            raise AuthError("缺少访问令牌")
        with self.store.read() as c:
            row = c.execute("SELECT * FROM workers WHERE token=?", (token,)).fetchone()
        if row is None:
            raise AuthError("令牌无效")
        d = dict(row)
        if not d["active"]:
            raise AuthError("账号已失效")
        return d

    def _open_risk(self, c, *, elder, band: RiskBand, description: str, actor: dict,
                   happened: datetime, plan_id: Optional[str], visit_id: int) -> str:
        risk_id = _new_id("risk")
        c.execute(
            """INSERT INTO risks(id, elder_id, band, description, status, source,
                   created_at, created_by, source_plan_id, source_visit_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (risk_id, elder["id"], band, description, RiskStatus.OPEN, "visit",
             _ts(happened), actor["id"], plan_id, visit_id))
        self._chain(c, risk_id, None, actor["id"], "open", None, None,
                    f"到访发现: {description}", happened)
        if band in (RiskBand.CONCERN, RiskBand.URGENT):
            # 撤回风险处置授权：线索只登记留痕，不启动升级计时。
            if self._consent_granted_at(c, elder["id"], ConsentScope.RISK_CARE, happened):
                hours = _LEVEL_DEADLINE_HOURS[("township", band)]
                risk = c.execute("SELECT * FROM risks WHERE id=?", (risk_id,)).fetchone()
                self._open_escalation(c, risk, "township", happened, timedelta(hours=hours), cause="open")
            else:
                self._event(c, happened, "escalation_skipped_consent", elder["id"], None,
                            "risk_care_consent_withdrawn", risk_id)
        return risk_id

    def _open_escalation(self, c, risk, level: str, now: datetime, deadline_delta: timedelta,
                         *, cause: str) -> str:
        esc_id = _new_id("esc")
        c.execute(
            "INSERT INTO escalations(id, risk_id, level, state, deadline_at, opened_at) VALUES(?,?,?,?,?,?)",
            (esc_id, risk["id"], level, EscalationState.PENDING,
             _ts(now + deadline_delta), _ts(now)))
        self._chain(c, risk["id"], esc_id, "system",
                    "escalate" if cause == "timeout" else "open_escalation",
                    None, None, f"{level} 级处置开始，截止 {_ts(now + deadline_delta)}", now)
        return esc_id

    def _risk_and_open_escalation(self, c, risk_id: str):
        risk = c.execute("SELECT * FROM risks WHERE id=?", (risk_id,)).fetchone()
        if risk is None:
            raise NotFoundError("风险线索不存在")
        if risk["status"] == RiskStatus.CLOSED:
            raise ConflictError("风险线索已关闭")
        esc = self._latest_escalation_row(c, risk_id)
        if esc is None:
            raise ConflictError("该线索没有进行中的升级事项")
        return risk, esc

    @staticmethod
    def _latest_escalation_row(c, risk_id: str):
        return c.execute(
            "SELECT * FROM escalations WHERE risk_id=? ORDER BY opened_at DESC, rowid DESC LIMIT 1",
            (risk_id,)).fetchone()

    def _risk_snapshot(self, c, risk_id: str) -> dict:
        risk = dict(c.execute("SELECT * FROM risks WHERE id=?", (risk_id,)).fetchone())
        esc = self._latest_escalation_row(c, risk_id)
        risk["escalation"] = dict(esc) if esc else None
        return risk

    def _chain(self, c, risk_id, escalation_id, actor_id, action,
               from_owner, to_owner, detail, at: datetime) -> None:
        c.execute(
            """INSERT INTO chain_events(risk_id, escalation_id, actor_id, action,
                   from_owner_id, to_owner_id, detail, at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (risk_id, escalation_id, actor_id, action, from_owner, to_owner, detail, _ts(at)))

    def _event(self, c, at: datetime, etype: str, elder_id, plan_id, reason: str,
               detail: str = "") -> int:
        cur = c.execute(
            "INSERT INTO plan_events(at, type, elder_id, plan_id, reason, detail) VALUES(?,?,?,?,?,?)",
            (_ts(at), etype, elder_id, plan_id, reason, detail))
        return int(cur.lastrowid)

    def _cancel_live_plans(self, c, *, reason: str, at: datetime,
                           elder_id: Optional[str] = None,
                           worker_id: Optional[str] = None, detail: str = "") -> int:
        sql = "SELECT * FROM plans WHERE status IN (?,?)"
        args: list[Any] = [PlanStatus.PENDING, PlanStatus.ASSIGNED]
        if elder_id is not None:
            sql += " AND elder_id=?"
            args.append(elder_id)
        if worker_id is not None:
            sql += " AND assignee_id=?"
            args.append(worker_id)
        rows = c.execute(sql, args).fetchall()
        for p in rows:
            c.execute("UPDATE plans SET status=? WHERE id=?", (PlanStatus.CANCELLED, p["id"]))
            self._event(c, at, "plan_cancelled", p["elder_id"], p["id"], reason, detail)
        return len(rows)

    def _pick_assignee(self, c, elder: dict, on_day: date) -> Optional[str]:
        rows = c.execute(
            """SELECT w.*,
                      (SELECT COUNT(*) FROM plans p
                        WHERE p.assignee_id=w.id AND p.due_date=?
                          AND p.status IN (?,?)) AS load
               FROM workers w
               LEFT JOIN agencies a ON a.id=w.agency_id
               WHERE w.active=1 AND w.role=? AND w.town=?
                 AND (w.agency_id IS NULL OR a.active=1)
               ORDER BY (w.village=?) DESC, load ASC, w.id""",
            (on_day.isoformat(), PlanStatus.PENDING, PlanStatus.ASSIGNED,
             Role.VILLAGER, elder["town"], elder["village"])).fetchall()
        for w in rows:
            if w["load"] < w["daily_capacity"]:
                return w["id"]
        # 本乡无可用容量：留空待乡镇改派，不跨乡自动派单。
        return None

    def _agency_of(self, c, worker_id: Optional[str]) -> Optional[str]:
        if worker_id is None:
            return None
        row = c.execute("SELECT agency_id FROM workers WHERE id=?", (worker_id,)).fetchone()
        return row["agency_id"] if row else None

    def _get_plan(self, c, plan_id: str):
        row = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("探访计划不存在")
        return row

    def _plan_dict(self, c, plan_id: str) -> dict:
        return dict(self._get_plan(c, plan_id))

    def _elder_town(self, c, elder_id: str) -> str:
        return c.execute("SELECT town FROM elders WHERE id=?", (elder_id,)).fetchone()["town"]

    def _is_holiday(self, c, d: date) -> bool:
        return c.execute("SELECT 1 FROM holidays WHERE day=?", (d.isoformat(),)).fetchone() is not None

    def _write_consent(self, c, elder_id: str, scope: str, granted: bool,
                       actor_id: str, reason: str, now: datetime) -> None:
        c.execute(
            """INSERT INTO consents(elder_id, scope, granted, updated_at, updated_by, reason)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(elder_id, scope) DO UPDATE SET
                 granted=excluded.granted, updated_at=excluded.updated_at,
                 updated_by=excluded.updated_by, reason=excluded.reason""",
            (elder_id, scope, 1 if granted else 0, _ts(now), actor_id, reason))
        # 审计永远追加：撤回不删除、覆盖不丢历史。
        c.execute(
            """INSERT INTO consent_audit(elder_id, scope, granted, changed_at, changed_by, reason)
               VALUES(?,?,?,?,?,?)""",
            (elder_id, scope, 1 if granted else 0, _ts(now), actor_id, reason))

    def _consent_granted(self, c, elder_id: str, scope: ConsentScope, at: datetime) -> bool:
        return self._consent_granted_at(c, elder_id, scope, at)

    def _consent_granted_at(self, c, elder_id: str, scope: ConsentScope, at: datetime) -> bool:
        row = c.execute(
            """SELECT granted FROM consent_audit
               WHERE elder_id=? AND scope=? AND changed_at<=?
               ORDER BY changed_at DESC, id DESC LIMIT 1""",
            (elder_id, scope, _ts(at))).fetchone()
        if row is not None:
            return bool(row["granted"])
        cur = c.execute("SELECT granted FROM consents WHERE elder_id=? AND scope=?",
                        (elder_id, scope)).fetchone()
        return bool(cur and cur["granted"])

    def _active_at(self, c, table: str, entity_id: str, at: datetime) -> bool:
        """判断机构/人员在“发生当时”是否有效（用于离线记录的历史判定）。"""
        row = c.execute(
            f"SELECT active, deactivated_at, created_at FROM {table} WHERE id=?",
            (entity_id,)).fetchone()
        if row is None:
            return False
        created = _parse_dt(row["created_at"])
        if created > at:
            return False
        deact = _parse_dt(row["deactivated_at"]) if row["deactivated_at"] else None
        if deact is not None and deact <= at:
            return False
        return True

    def _csv_row(self, p, v, risk, escs) -> list:
        if escs:
            levels = " -> ".join(e["level"] for e in escs)
            states = " -> ".join(e["state"] for e in escs)
            deadlines = ";".join(e["deadline_at"] for e in escs)
            timeouts = ";".join(e["timed_out_at"] or "" for e in escs)
        else:
            levels = states = deadlines = timeouts = ""
        return [
            p["id"], p["elder_id"], p["elder_name"], p["t_town"], p["t_village"], p["e_band"],
            p["due_date"], p["status"], p["worker_id"] or p["assignee_id"] or "",
            p["agency_name"] or "", v["outcome"] if v else "",
            v["occurred_at"] if v else "", v["received_at"] if v else "",
            v["merged_status"] if v else "", risk["id"] if risk else "",
            risk["band"] if risk else "", risk["status"] if risk else "",
            levels, states, deadlines, timeouts,
        ]

    @staticmethod
    def _enum_value(enum_cls, value: str, label: str) -> Any:
        try:
            return enum_cls(value)
        except ValueError as exc:
            raise ValidationError_(f"{label}取值不合法: {value!r}") from exc

    @staticmethod
    def _require_role(actor: dict, roles) -> None:
        allowed = roles if isinstance(roles, (list, tuple)) else [roles]
        allowed_values = {r.value if isinstance(r, Role) else r for r in allowed}
        if actor["role"] not in allowed_values:
            raise PermissionError_("当前角色无权执行该操作")

    @staticmethod
    def _require_town(actor: dict, town: str, *, allow_villager_creator: bool = False,
                      creator: Optional[str] = None) -> None:
        if actor["role"] == Role.COUNTY:
            return
        if actor["role"] == Role.TOWNSHIP:
            if actor["town"] != town:
                raise PermissionError_("不能跨乡镇操作")
            return
        if allow_villager_creator and creator == actor["id"]:
            return
        raise PermissionError_("无权操作该资源")

    @staticmethod
    def _require_level(actor: dict, level: str) -> None:
        if level == "county" and actor["role"] != Role.COUNTY:
            raise PermissionError_("该事项已升级到县级")
