"""县、乡、村三级养老探访协调器。

职责:
- 按老人风险等级生成滚动探访计划;
- 合并村级客户端离线上报(按发生顺序、凭证幂等);
- 对高风险线索计时升级,并维护接手/转派/联系家属/关闭的责任链;
- 授权撤回即时生效,既有审计保留;
- 住院、请假、断网恢复、机构退出等变化触发可解释的重新分配。

时钟可注入,测试用 ``ManualClock`` 控制升级时限;状态全部落在
SQLite,进程重启后升级计时与责任链不丢失。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import RiskBand, VisitOutcome, validate_offline_token
from .enums import AlertAction, AlertStatus, Role, Scope, TaskStatus
from .store import Store

# 风险等级对应的探访间隔(天)
CADENCE_DAYS = {"high": 7, "medium": 15, "low": 30}

# 线索等级对应的升级时限(分钟);None 表示不升级
ESCALATION_MINUTES = {
    RiskBand.URGENT: 120,
    RiskBand.CONCERN: 12 * 60,
    RiskBand.ROUTINE: None,
}

PLAN_HORIZON_DAYS = 14
INDEFINITE = "9999-12-31"


class CoordinatorError(Exception):
    """协调器业务错误基类。"""


class NotFound(CoordinatorError):
    pass


class Forbidden(CoordinatorError):
    pass


class Conflict(CoordinatorError):
    pass


class Validation(CoordinatorError):
    pass


class SystemClock:
    def now(self) -> datetime:
        return datetime.now()


class ManualClock:
    """测试用可控时钟。"""

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        self._now += timedelta(**kwargs)
        return self._now


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _as_dt(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


class Coordinator:
    """探访计划与风险线索的服务端协调器。"""

    def __init__(self, db_path: str | Path = ":memory:", clock=None):
        self.store = Store(db_path)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _audit(self, actor_id: str, action: str, target: str, detail: str = "") -> None:
        self.store.insert(
            "audit_log",
            {
                "actor_id": actor_id,
                "action": action,
                "target": target,
                "at": self._now().isoformat(),
                "detail": detail,
            },
        )

    def _get(self, table: str, row_id: str, label: str) -> dict:
        row = self.store.query_one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        if row is None:
            raise NotFound(f"{label}不存在: {row_id}")
        return row

    def _elder(self, elder_id: str) -> dict:
        row = self._get("elders", elder_id, "老人")
        if not isinstance(row["scopes"], list):
            row["scopes"] = json.loads(row["scopes"])
        if not isinstance(row["family_contacts"], list):
            row["family_contacts"] = json.loads(row["family_contacts"])
        row["authorized"] = bool(row["authorized"])
        return row

    def _worker(self, worker_id: str) -> dict:
        row = self._get("workers", worker_id, "服务人员")
        row["active"] = bool(row["active"])
        row["on_leave"] = bool(row["on_leave"])
        return row

    def _require_role(self, worker: dict, *roles: Role) -> None:
        if worker["role"] not in [str(r) for r in roles]:
            raise Forbidden(f"角色 {worker['role']} 无权执行该操作")

    # ------------------------------------------------------------------
    # 注册:老人 / 人员 / 机构(县级)
    # ------------------------------------------------------------------

    def register_elder(
        self,
        *,
        actor_id: str,
        name: str,
        risk_level: str,
        site_id: str,
        township_id: str,
        scopes: list[str] | None = None,
        address: str = "",
        health_notes: str = "",
        family_contacts: list | None = None,
        elder_id: str | None = None,
    ) -> dict:
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            if risk_level not in CADENCE_DAYS:
                raise Validation(f"未知风险等级: {risk_level}")
            elder_id = elder_id or _new_id("elder")
            granted = [str(s) for s in (scopes or [Scope.PROFILE, Scope.HEALTH, Scope.FAMILY])]
            self.store.insert(
                "elders",
                {
                    "id": elder_id,
                    "name": name,
                    "risk_level": risk_level,
                    "authorized": 1,
                    "suspended_until": None,
                    "scopes": granted,
                    "address": address,
                    "site_id": site_id,
                    "township_id": township_id,
                    "health_notes": health_notes,
                    "family_contacts": family_contacts or [],
                },
            )
            self._audit(actor_id, "elder.registered", elder_id, f"风险等级={risk_level}")
            self.ensure_plan(elder_id)
            return self._elder(elder_id)

    def register_worker(
        self,
        *,
        actor_id: str | None,
        name: str,
        role: str,
        site_id: str,
        township_id: str,
        capacity: int = 8,
        worker_id: str | None = None,
    ) -> dict:
        with self._lock:
            has_county = self.store.query_one(
                "SELECT COUNT(*) AS n FROM workers WHERE role = 'county' AND active = 1"
            )["n"]
            if has_county:
                actor = self._worker(actor_id)
                self._require_role(actor, Role.COUNTY)
            elif role != str(Role.COUNTY):
                raise Validation("系统首个注册人员必须是县级角色")
            if role not in [str(r) for r in Role]:
                raise Validation(f"未知角色: {role}")
            worker_id = worker_id or _new_id("worker")
            self.store.insert(
                "workers",
                {
                    "id": worker_id,
                    "name": name,
                    "role": role,
                    "site_id": site_id,
                    "township_id": township_id,
                    "active": 1,
                    "on_leave": 0,
                    "capacity": capacity,
                },
            )
            self._audit(actor_id or "system", "worker.registered", worker_id, f"角色={role}")
            return self._worker(worker_id)

    def register_agency(self, *, actor_id: str, name: str, agency_id: str | None = None) -> dict:
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            agency_id = agency_id or _new_id("agency")
            self.store.insert("agencies", {"id": agency_id, "name": name, "active": 1})
            self._audit(actor_id, "agency.registered", agency_id, name)
            return self._get("agencies", agency_id, "机构")

    # ------------------------------------------------------------------
    # 滚动探访计划
    # ------------------------------------------------------------------

    def _pick_agency(self) -> str | None:
        agencies = self.store.query_all("SELECT * FROM agencies WHERE active = 1")
        if not agencies:
            return None
        loads = {
            a["id"]: self.store.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE agency_id = ? AND status IN ('planned','claimed')",
                (a["id"],),
            )["n"]
            for a in agencies
        }
        return min(agencies, key=lambda a: loads[a["id"]])["id"]

    def ensure_plan(self, elder_id: str, horizon_days: int = PLAN_HORIZON_DAYS) -> list[dict]:
        """让老人的探访计划覆盖到 horizon;返回新生成的任务。"""
        with self._lock:
            elder = self._elder(elder_id)
            if not elder["authorized"]:
                return []
            cadence = CADENCE_DAYS[elder["risk_level"]]
            today = self._now().date()
            rows = self.store.query_all(
                "SELECT due_date FROM tasks WHERE elder_id = ? AND status != 'cancelled'",
                (elder_id,),
            )
            coverage = max((datetime.fromisoformat(r["due_date"]).date() for r in rows), default=None)
            floor = today - timedelta(days=cadence)
            if coverage is None or coverage < floor:
                coverage = floor
            if elder["suspended_until"]:
                suspended_until = datetime.fromisoformat(elder["suspended_until"]).date()
                coverage = max(coverage, suspended_until)
            horizon = today + timedelta(days=horizon_days)
            created = []
            next_due = coverage + timedelta(days=cadence)
            while next_due <= horizon:
                task_id = _new_id("task")
                self.store.insert(
                    "tasks",
                    {
                        "id": task_id,
                        "elder_id": elder_id,
                        "site_id": elder["site_id"],
                        "agency_id": self._pick_agency(),
                        "due_date": next_due.isoformat(),
                        "status": str(TaskStatus.PLANNED),
                    },
                )
                created.append(self._get("tasks", task_id, "任务"))
                next_due += timedelta(days=cadence)
            return created

    def list_tasks(self, *, elder_id: str | None = None, status: str | None = None) -> list[dict]:
        with self._lock:
            sql = "SELECT * FROM tasks WHERE 1=1"
            params: list = []
            if elder_id:
                sql += " AND elder_id = ?"
                params.append(elder_id)
            if status:
                sql += " AND status = ?"
                params.append(status)
            return self.store.query_all(sql + " ORDER BY due_date, id", tuple(params))

    # ------------------------------------------------------------------
    # 接单与离线记录合并
    # ------------------------------------------------------------------

    def claim_task(self, *, worker_id: str, task_id: str) -> dict:
        """村级人员接单;数据库条件更新保证并发下只有一人成功。"""
        with self._lock:
            worker = self._worker(worker_id)
            self._require_role(worker, Role.VILLAGE)
            if not worker["active"] or worker["on_leave"]:
                raise Forbidden("人员已失效或请假,不能接单")
            task = self._get("tasks", task_id, "任务")
            claimed = self.store.query_one(
                "SELECT COUNT(*) AS n FROM tasks WHERE claimed_by = ? AND status = 'claimed'",
                (worker_id,),
            )["n"]
            if claimed >= worker["capacity"]:
                raise Conflict("超出个人服务能力上限")
            cur = self.store.execute(
                "UPDATE tasks SET status = 'claimed', claimed_by = ?, version = version + 1"
                " WHERE id = ? AND status = 'planned'",
                (worker_id, task_id),
            )
            if cur.rowcount == 0:
                raise Conflict(f"任务已被他人接走: {task_id}")
            self._audit(worker_id, "task.claimed", task_id, f"老人={task['elder_id']}")
            return self._get("tasks", task_id, "任务")

    def _select_task_for_visit(self, elder_id: str, occurred_date: str) -> dict:
        open_tasks = self.store.query_all(
            "SELECT * FROM tasks WHERE elder_id = ? AND status IN ('planned','claimed')",
            (elder_id,),
        )
        due_before = [t for t in open_tasks if t["due_date"] <= occurred_date]
        if due_before:
            return max(due_before, key=lambda t: t["due_date"])
        if open_tasks:
            return min(open_tasks, key=lambda t: t["due_date"])
        # 没有开放任务(例如断网期间计划尚未生成),补建一个临时任务
        elder = self._elder(elder_id)
        task_id = _new_id("task")
        self.store.insert(
            "tasks",
            {
                "id": task_id,
                "elder_id": elder_id,
                "site_id": elder["site_id"],
                "agency_id": self._pick_agency(),
                "due_date": occurred_date,
                "status": str(TaskStatus.PLANNED),
            },
        )
        return self._get("tasks", task_id, "任务")

    def _resequence_merged(self, elder_id: str) -> None:
        """按实际发生时间重排合并序号,与上报到达顺序无关。"""
        done = self.store.query_all(
            "SELECT id FROM tasks WHERE elder_id = ? AND status = 'done'"
            " ORDER BY occurred_at, id",
            (elder_id,),
        )
        for seq, row in enumerate(done, start=1):
            self.store.execute("UPDATE tasks SET merged_seq = ? WHERE id = ?", (seq, row["id"]))

    def record_visit(
        self,
        *,
        worker_id: str,
        elder_id: str,
        outcome: str,
        offline_token: str,
        occurred_at: datetime | str,
        risk: dict | None = None,
    ) -> dict:
        """合并一条村级离线记录。

        同一 ``offline_token`` 重复上报时直接返回首次结果,不重复完成
        任务、不重复产生线索与审计。
        """
        with self._lock:
            worker = self._worker(worker_id)
            self._require_role(worker, Role.VILLAGE)
            try:
                validate_offline_token(offline_token)
            except ValueError as exc:
                raise Validation(str(exc)) from exc
            if outcome not in [str(o) for o in VisitOutcome]:
                raise Validation(f"未知探访结果: {outcome}")

            existing = self.store.query_one(
                "SELECT * FROM tasks WHERE offline_token = ?", (offline_token,)
            )
            if existing:
                return {"task": existing, "alert_id": None, "idempotent": True}

            elder = self._elder(elder_id)
            if not elder["authorized"]:
                raise Forbidden("老人已注销授权,停止探访登记")
            if elder["suspended_until"]:
                raise Validation(f"老人住院暂停探访至 {elder['suspended_until']}")
            if str(Scope.PROFILE) not in elder["scopes"]:
                raise Forbidden("老人已撤回基本服务授权")

            occurred = _as_dt(occurred_at)
            if occurred > self._now() + timedelta(minutes=5):
                raise Validation("发生时间不能晚于当前时间")
            occurred_date = occurred.date().isoformat()

            self.ensure_plan(elder_id)
            task = self._select_task_for_visit(elder_id, occurred_date)
            try:
                self.store.execute(
                    "UPDATE tasks SET status = 'done', outcome = ?, offline_token = ?,"
                    " occurred_at = ?, version = version + 1 WHERE id = ?",
                    (outcome, offline_token, occurred.isoformat(), task["id"]),
                )
            except sqlite3.IntegrityError:
                # 并发下同一凭证撞唯一约束:按首次上报处理
                existing = self.store.query_one(
                    "SELECT * FROM tasks WHERE offline_token = ?", (offline_token,)
                )
                if existing:
                    return {"task": existing, "alert_id": None, "idempotent": True}
                raise
            self._resequence_merged(elder_id)
            self._audit(
                worker_id,
                "visit.recorded",
                task["id"],
                f"老人={elder_id} 结果={outcome} 凭证={offline_token}",
            )

            alert_id = None
            if outcome == str(VisitOutcome.RISK_FOUND):
                band = (risk or {}).get("band", str(RiskBand.URGENT))
                detail = (risk or {}).get("detail", "")
                alert_id = self._raise_alert(
                    elder_id=elder_id,
                    task_id=task["id"],
                    band=band,
                    detail=detail,
                    raised_by=worker_id,
                )

            self.ensure_plan(elder_id)
            return {
                "task": self._get("tasks", task["id"], "任务"),
                "alert_id": alert_id,
                "idempotent": False,
            }

    # ------------------------------------------------------------------
    # 风险线索与责任链
    # ------------------------------------------------------------------

    def _raise_alert(self, *, elder_id: str, task_id: str | None, band: str, detail: str, raised_by: str) -> str:
        if band not in [str(b) for b in RiskBand]:
            raise Validation(f"未知线索等级: {band}")
        now = self._now()
        minutes = ESCALATION_MINUTES[RiskBand(band)]
        deadline = (now + timedelta(minutes=minutes)).isoformat() if minutes else None
        alert_id = _new_id("alert")
        self.store.insert(
            "alerts",
            {
                "id": alert_id,
                "elder_id": elder_id,
                "task_id": task_id,
                "band": band,
                "detail": detail,
                "status": str(AlertStatus.OPEN),
                "raised_by": raised_by,
                "raised_at": now.isoformat(),
                "deadline_at": deadline,
                "acked_by": None,
                "escalated": 0,
            },
        )
        self._alert_event(alert_id, AlertAction.RAISE, raised_by, detail)
        self._audit(raised_by, "alert.raised", alert_id, f"老人={elder_id} 等级={band}")
        return alert_id

    def _alert_event(
        self,
        alert_id: str,
        action: AlertAction,
        actor_id: str,
        note: str = "",
        from_handler: str | None = None,
        to_handler: str | None = None,
    ) -> None:
        self.store.insert(
            "alert_events",
            {
                "alert_id": alert_id,
                "action": str(action),
                "actor_id": actor_id,
                "at": self._now().isoformat(),
                "note": note,
                "from_handler": from_handler,
                "to_handler": to_handler,
            },
        )

    def _alert(self, alert_id: str) -> dict:
        return self._get("alerts", alert_id, "线索")

    def _escalation_target(self, elder: dict) -> str | None:
        rows = self.store.query_all(
            "SELECT * FROM workers WHERE role = 'township' AND active = 1 AND on_leave = 0"
            " AND township_id = ? ORDER BY id",
            (elder["township_id"],),
        )
        if not rows:
            rows = self.store.query_all(
                "SELECT * FROM workers WHERE role = 'township' AND active = 1 AND on_leave = 0 ORDER BY id"
            )
        if not rows:
            rows = self.store.query_all(
                "SELECT * FROM workers WHERE role = 'county' AND active = 1 ORDER BY id"
            )
        return rows[0]["id"] if rows else None

    def check_escalations(self) -> list[dict]:
        """把超过时限仍未接手的线索升级给乡镇/县级人员。

        只依赖持久化的 ``deadline_at``,服务重启后继续计时。
        """
        with self._lock:
            now = self._now().isoformat()
            due = self.store.query_all(
                "SELECT * FROM alerts WHERE status = 'open' AND escalated = 0"
                " AND deadline_at IS NOT NULL AND deadline_at <= ?",
                (now,),
            )
            fired = []
            for alert in due:
                elder = self._elder(alert["elder_id"])
                target = self._escalation_target(elder)
                self.store.execute(
                    "UPDATE alerts SET escalated = 1 WHERE id = ? AND escalated = 0",
                    (alert["id"],),
                )
                note = "超过处置时限自动升级" if target else "超过时限但无可用接手人"
                self._alert_event(alert["id"], AlertAction.ESCALATE, "system", note, to_handler=target)
                self._audit("system", "alert.escalated", alert["id"], f"接手人={target}")
                fired.append({"alert_id": alert["id"], "escalated_to": target})
            return fired

    def acknowledge_alert(self, *, alert_id: str, handler_id: str) -> dict:
        """乡镇/县级人员接手线索;并发下只有一人接手成功。"""
        with self._lock:
            handler = self._worker(handler_id)
            self._require_role(handler, Role.TOWNSHIP, Role.COUNTY)
            if not handler["active"] or handler["on_leave"]:
                raise Forbidden("人员已失效或请假,不能接手线索")
            self._alert(alert_id)
            cur = self.store.execute(
                "UPDATE alerts SET status = 'acked', acked_by = ? WHERE id = ? AND status = 'open'",
                (handler_id, alert_id),
            )
            if cur.rowcount == 0:
                raise Conflict("线索已被接手或关闭")
            self._alert_event(alert_id, AlertAction.ACK, handler_id)
            self._audit(handler_id, "alert.acked", alert_id)
            return self._alert(alert_id)

    def reassign_alert(self, *, alert_id: str, actor_id: str, to_handler_id: str, note: str = "") -> dict:
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.TOWNSHIP, Role.COUNTY)
            alert = self._alert(alert_id)
            if alert["status"] == str(AlertStatus.CLOSED):
                raise Conflict("线索已关闭,不能转派")
            target = self._worker(to_handler_id)
            self._require_role(target, Role.TOWNSHIP, Role.COUNTY)
            if not target["active"] or target["on_leave"]:
                raise Validation("目标人员不可用")
            previous = alert["acked_by"]
            self.store.execute(
                "UPDATE alerts SET status = 'acked', acked_by = ? WHERE id = ?",
                (to_handler_id, alert_id),
            )
            self._alert_event(
                alert_id, AlertAction.REASSIGN, actor_id, note,
                from_handler=previous, to_handler=to_handler_id,
            )
            self._audit(actor_id, "alert.reassigned", alert_id, f"{previous}->{to_handler_id} {note}")
            return self._alert(alert_id)

    def contact_family(self, *, alert_id: str, actor_id: str, note: str = "") -> dict:
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.TOWNSHIP, Role.COUNTY)
            alert = self._alert(alert_id)
            if alert["status"] == str(AlertStatus.CLOSED):
                raise Conflict("线索已关闭")
            if alert["acked_by"] not in (None, actor_id) and actor["role"] != str(Role.COUNTY):
                raise Forbidden("只有当前接手人或县级人员可以联系家属")
            elder = self._elder(alert["elder_id"])
            if str(Scope.FAMILY) not in elder["scopes"]:
                raise Forbidden("老人已撤回家属联系授权")
            self._alert_event(alert_id, AlertAction.CONTACT_FAMILY, actor_id, note)
            self._audit(actor_id, "alert.family_contacted", alert_id, note)
            return self._alert(alert_id)

    def close_alert(self, *, alert_id: str, actor_id: str, note: str = "") -> dict:
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.TOWNSHIP, Role.COUNTY)
            alert = self._alert(alert_id)
            if alert["status"] == str(AlertStatus.CLOSED):
                raise Conflict("线索已关闭")
            self.store.execute(
                "UPDATE alerts SET status = 'closed' WHERE id = ?", (alert_id,)
            )
            self._alert_event(alert_id, AlertAction.CLOSE, actor_id, note)
            self._audit(actor_id, "alert.closed", alert_id, note)
            return self._alert(alert_id)

    def list_alerts(self, *, status: str | None = None, township_id: str | None = None) -> list[dict]:
        with self._lock:
            sql = ("SELECT a.* FROM alerts a JOIN elders e ON e.id = a.elder_id WHERE 1=1")
            params: list = []
            if status:
                sql += " AND a.status = ?"
                params.append(status)
            if township_id:
                sql += " AND e.township_id = ?"
                params.append(township_id)
            return self.store.query_all(sql + " ORDER BY a.raised_at, a.id", tuple(params))

    def alert_chain(self, alert_id: str) -> list[dict]:
        """线索的完整责任链。"""
        with self._lock:
            self._alert(alert_id)
            return self.store.query_all(
                "SELECT * FROM alert_events WHERE alert_id = ? ORDER BY seq", (alert_id,)
            )

    # ------------------------------------------------------------------
    # 授权与访问控制
    # ------------------------------------------------------------------

    def set_consents(self, *, actor_id: str, elder_id: str, scopes: list[str]) -> dict:
        """县级更新老人授权范围;撤回立即生效,既有审计保留。"""
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            elder = self._elder(elder_id)
            new_scopes = [str(s) for s in scopes]
            revoked = sorted(set(elder["scopes"]) - set(new_scopes))
            granted = sorted(set(new_scopes) - set(elder["scopes"]))
            self.store.update("elders", elder_id, {"scopes": json.dumps(new_scopes, ensure_ascii=False)})
            self._audit(
                actor_id, "consent.updated", elder_id,
                f"新增={granted} 撤回={revoked}",
            )
            return self._elder(elder_id)

    def elder_view(self, *, actor_id: str, elder_id: str) -> dict:
        """按角色与授权范围返回老人信息;敏感字段访问留痕。"""
        with self._lock:
            actor = self._worker(actor_id)
            elder = self._elder(elder_id)
            if not elder["authorized"] and actor["role"] != str(Role.COUNTY):
                raise Forbidden("老人已注销,仅县级可查看")
            view = {
                "id": elder["id"],
                "name": elder["name"],
                "risk_level": elder["risk_level"],
                "site_id": elder["site_id"],
                "township_id": elder["township_id"],
                "authorized": elder["authorized"],
                "suspended_until": elder["suspended_until"],
            }
            if actor["role"] == str(Role.COUNTY):
                view.update(
                    {
                        "address": elder["address"],
                        "health_notes": elder["health_notes"],
                        "family_contacts": elder["family_contacts"],
                        "scopes": elder["scopes"],
                    }
                )
                return view
            if str(Scope.PROFILE) in elder["scopes"]:
                view["address"] = elder["address"]
            if str(Scope.HEALTH) in elder["scopes"]:
                view["health_notes"] = elder["health_notes"]
                self._audit(actor_id, "elder.health_viewed", elder_id)
            if str(Scope.FAMILY) in elder["scopes"]:
                view["family_contacts"] = elder["family_contacts"]
                self._audit(actor_id, "elder.family_viewed", elder_id)
            return view

    # ------------------------------------------------------------------
    # 状态变化与可解释的重新分配
    # ------------------------------------------------------------------

    def _cancel_open_tasks(self, elder_id: str, reason: str, explanations: list[str]) -> None:
        open_tasks = self.store.query_all(
            "SELECT * FROM tasks WHERE elder_id = ? AND status IN ('planned','claimed')",
            (elder_id,),
        )
        for task in open_tasks:
            self.store.execute(
                "UPDATE tasks SET status = 'cancelled', version = version + 1 WHERE id = ?",
                (task["id"],),
            )
            explanations.append(f"任务 {task['id']}(到期 {task['due_date']})已取消:{reason}")
            self._audit("system", "task.cancelled", task["id"], reason)

    def elder_status(self, *, actor_id: str, elder_id: str, action: str, **kwargs) -> dict:
        """老人状态变化:住院 / 出院 / 迁居 / 注销 / 恢复。"""
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            elder = self._elder(elder_id)
            explanations: list[str] = []
            if action == "hospitalize":
                until = kwargs.get("until") or INDEFINITE
                self.store.update("elders", elder_id, {"suspended_until": until})
                self._cancel_open_tasks(elder_id, f"老人临时住院(至 {until})", explanations)
                explanations.append(f"老人 {elder_id} 住院暂停探访至 {until},期间不再生成新任务")
                self._audit(actor_id, "elder.hospitalized", elder_id, f"至 {until}")
            elif action == "return_home":
                self.store.update("elders", elder_id, {"suspended_until": None})
                created = self.ensure_plan(elder_id)
                explanations.append(f"老人 {elder_id} 出院,重新生成 {len(created)} 条探访任务")
                self._audit(actor_id, "elder.returned_home", elder_id)
            elif action == "relocate":
                site_id = kwargs.get("site_id") or elder["site_id"]
                township_id = kwargs.get("township_id") or elder["township_id"]
                self._cancel_open_tasks(elder_id, "老人居住变化,原站点任务取消", explanations)
                self.store.update("elders", elder_id, {"site_id": site_id, "township_id": township_id})
                created = self.ensure_plan(elder_id)
                explanations.append(
                    f"老人 {elder_id} 迁至站点 {site_id},新站点生成 {len(created)} 条任务"
                )
                self._audit(actor_id, "elder.relocated", elder_id, f"新站点={site_id}")
            elif action == "deactivate":
                self.store.update("elders", elder_id, {"authorized": 0})
                self._cancel_open_tasks(elder_id, "老人注销,停止探访", explanations)
                explanations.append(f"老人 {elder_id} 已注销,后续探访与访问受限")
                self._audit(actor_id, "elder.deactivated", elder_id)
            elif action == "restore":
                self.store.update("elders", elder_id, {"authorized": 1})
                created = self.ensure_plan(elder_id)
                explanations.append(f"老人 {elder_id} 恢复服务,生成 {len(created)} 条任务")
                self._audit(actor_id, "elder.restored", elder_id)
            else:
                raise Validation(f"未知状态操作: {action}")
            return {"elder": self._elder(elder_id), "explanations": explanations}

    def worker_status(self, *, actor_id: str, worker_id: str, action: str) -> dict:
        """人员变化:请假 / 返岗 / 失效 / 恢复;负责事项自动重新分配。"""
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            worker = self._worker(worker_id)
            explanations: list[str] = []
            unavailable = action in ("leave", "deactivate")
            if action == "leave":
                self.store.update("workers", worker_id, {"on_leave": 1})
            elif action == "back":
                self.store.update("workers", worker_id, {"on_leave": 0})
            elif action == "deactivate":
                self.store.update("workers", worker_id, {"active": 0})
            elif action == "activate":
                self.store.update("workers", worker_id, {"active": 1})
            else:
                raise Validation(f"未知人员操作: {action}")
            if unavailable:
                released = self.store.query_all(
                    "SELECT * FROM tasks WHERE claimed_by = ? AND status = 'claimed'",
                    (worker_id,),
                )
                for task in released:
                    self.store.execute(
                        "UPDATE tasks SET status = 'planned', claimed_by = NULL,"
                        " version = version + 1 WHERE id = ?",
                        (task["id"],),
                    )
                    explanations.append(f"任务 {task['id']} 已释放待认领:人员{action}")
                    self._audit("system", "task.released", task["id"], f"人员={worker_id} {action}")
                handling = self.store.query_all(
                    "SELECT * FROM alerts WHERE acked_by = ? AND status = 'acked'",
                    (worker_id,),
                )
                for alert in handling:
                    elder = self._elder(alert["elder_id"])
                    target = self._escalation_target(elder)
                    if target == worker_id:
                        target = None
                    self.store.execute(
                        "UPDATE alerts SET acked_by = ? WHERE id = ?",
                        (target, alert["id"]),
                    )
                    self._alert_event(
                        alert["id"], AlertAction.REASSIGN, "system",
                        f"人员{action}自动转派", from_handler=worker_id, to_handler=target,
                    )
                    explanations.append(f"线索 {alert['id']} 已转派给 {target}:人员{action}")
                    self._audit("system", "alert.reassigned", alert["id"], f"人员{action}自动转派")
            self._audit(actor_id, f"worker.{action}", worker_id)
            return {"worker": self._worker(worker_id), "explanations": explanations}

    def agency_withdraw(self, *, actor_id: str, agency_id: str) -> dict:
        """机构退出:名下未完成任务回到待认领池。"""
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            self._get("agencies", agency_id, "机构")
            self.store.update("agencies", agency_id, {"active": 0})
            affected = self.store.query_all(
                "SELECT * FROM tasks WHERE agency_id = ? AND status IN ('planned','claimed')",
                (agency_id,),
            )
            explanations = []
            for task in affected:
                self.store.execute(
                    "UPDATE tasks SET agency_id = NULL, status = 'planned', claimed_by = NULL,"
                    " version = version + 1 WHERE id = ?",
                    (task["id"],),
                )
                explanations.append(f"任务 {task['id']} 因机构退出重新进入待认领池")
                self._audit("system", "task.reassigned", task["id"], f"机构 {agency_id} 退出")
            self._audit(actor_id, "agency.withdrawn", agency_id, f"影响任务 {len(affected)} 条")
            return {"explanations": explanations, "affected": len(affected)}

    # ------------------------------------------------------------------
    # 每日遗漏扫描与监管导出
    # ------------------------------------------------------------------

    def scan_omissions(self, *, day: str | None = None) -> dict:
        """扫描截至某日(默认今天)逾期未完成的探访与计划缺口。"""
        with self._lock:
            day = day or self._today()
            missed = self.store.query_all(
                "SELECT t.id AS task_id, t.elder_id, t.due_date, t.site_id, t.status"
                " FROM tasks t JOIN elders e ON e.id = t.elder_id"
                " WHERE t.status IN ('planned','claimed') AND t.due_date < ?"
                " AND e.authorized = 1 ORDER BY t.due_date",
                (day,),
            )
            gaps = []
            for elder in self.store.query_all(
                "SELECT id, suspended_until FROM elders WHERE authorized = 1"
            ):
                if elder["suspended_until"]:
                    continue
                open_tasks = self.store.query_one(
                    "SELECT COUNT(*) AS n FROM tasks WHERE elder_id = ?"
                    " AND status IN ('planned','claimed')",
                    (elder["id"],),
                )["n"]
                if open_tasks == 0:
                    gaps.append(elder["id"])
            overdue_alerts = self.store.query_all(
                "SELECT id, elder_id, band, deadline_at FROM alerts"
                " WHERE status = 'open' AND deadline_at IS NOT NULL AND deadline_at < ?",
                (self._now().isoformat(),),
            )
            return {
                "day": day,
                "generated_at": self._now().isoformat(),
                "missed": missed,
                "plan_gaps": gaps,
                "overdue_alerts": overdue_alerts,
            }

    def export_audit_csv(self, *, actor_id: str) -> str:
        """监管导出:完整审计流水(含责任链相关动作),CSV 格式。"""
        with self._lock:
            actor = self._worker(actor_id)
            self._require_role(actor, Role.COUNTY)
            rows = self.store.query_all("SELECT * FROM audit_log ORDER BY seq")
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["seq", "actor_id", "action", "target", "at", "detail"])
            for row in rows:
                writer.writerow(
                    [row["seq"], row["actor_id"], row["action"], row["target"], row["at"], row["detail"]]
                )
            self._audit(actor_id, "audit.exported", "audit_log", f"导出 {len(rows)} 条")
            return buf.getvalue()
