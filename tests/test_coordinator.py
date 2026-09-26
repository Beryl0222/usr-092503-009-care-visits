"""协调器核心测试：用 FakeClock 与文件型 SQLite 证明关键性质。"""

import os
import tempfile
import threading
import unittest
from datetime import date

from src.care_visits.app import App
from src.care_visits.clock import FakeClock
from src.care_visits.contracts import PlanStatus, Role, new_offline_token
from src.care_visits.errors import (
    ConflictError,
    ConsentError,
    PermissionError_,
)
from src.care_visits.store import Storage


class AppTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "care.db")
        self.clock = FakeClock()
        self.store = Storage(self.db)
        self.app = App(self.store, self.clock)
        self.app.create_worker(None, "admin", "县管理员", Role.COUNTY,
                               town="COUNTY", token="TOK-ADMIN00-00000001")
        self.admin = self.app.authenticate("TOK-ADMIN00-00000001")
        self.app.create_agency(self.admin, "ag1", "阳光社工")
        self.app.create_worker(self.admin, "tw1", "乡民政员", Role.TOWNSHIP,
                               town="T1", token="TOK-TOWN00-00000001")
        self.township = self.app.authenticate("TOK-TOWN00-00000001")
        self.app.create_worker(self.admin, "tw2", "他乡民政员", Role.TOWNSHIP,
                               town="T2", token="TOK-TOWN00-00000002")
        self.township2 = self.app.authenticate("TOK-TOWN00-00000002")
        self.app.create_worker(self.admin, "v1", "村员甲", Role.VILLAGER,
                               town="T1", village="V1", agency_id="ag1",
                               token="TOK-VILL00-00000001")
        self.v1 = self.app.authenticate("TOK-VILL00-00000001")
        self.app.create_worker(self.admin, "v2", "村员乙", Role.VILLAGER,
                               town="T1", village="V1",
                               token="TOK-VILL00-00000002")
        self.v2 = self.app.authenticate("TOK-VILL00-00000002")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def make_elder(self, elder_id="el1", band="routine", town="T1", village="V1"):
        self.app.create_elder(self.admin, elder_id, f"老人{elder_id}", town, village, band)
        plans = [p for p in self.app.list_plans(self.admin) if p["elder_id"] == elder_id]
        return plans[0]

    def reopen_app(self):
        """模拟服务重启：换 Storage/App 实例，数据库文件与时钟延续。"""
        self.store.close()
        store = Storage(self.db)
        return App(store, self.clock), store


class OfflineIdempotencyTests(AppTestBase):
    def test_duplicate_token_is_idempotent(self):
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        token = new_offline_token()
        when = self.clock.now().isoformat()

        first = self.app.submit_visit(
            self.v1, token=token, outcome="risk_found", occurred_at=when,
            plan_id=plan["id"], risk_band="urgent", risk_description="跌倒无法起身")
        self.assertFalse(first["duplicate"])
        rid = first["risk_id"]

        # 网络恢复后客户端重试同一凭证（完全相同的载荷）。
        second = self.app.submit_visit(
            self.v1, token=token, outcome="risk_found", occurred_at=when,
            plan_id=plan["id"], risk_band="urgent", risk_description="跌倒无法起身")
        third = self.app.submit_visit(
            self.v1, token=token, outcome="completed", occurred_at=when,
            plan_id=plan["id"])  # 连结果被篡改也以首次为准

        self.assertTrue(second["duplicate"])
        self.assertTrue(third["duplicate"])
        self.assertEqual(second["visit_id"], first["visit_id"])

        with self.store.read() as c:
            n_records = c.execute("SELECT COUNT(*) AS n FROM visit_records").fetchone()["n"]
            n_risks = c.execute("SELECT COUNT(*) AS n FROM risks").fetchone()["n"]
            done = c.execute("SELECT * FROM plans WHERE id=?", (plan["id"],)).fetchone()
        self.assertEqual(n_records, 1, "重复上报不得产生第二条记录")
        self.assertEqual(n_risks, 1, "重复上报不得重复创建风险线索")
        self.assertEqual(done["status"], PlanStatus.DONE)

    def test_records_merge_in_occurrence_order(self):
        # 高风险：每日一访。断网期间积压多条记录，恢复后乱序送达，按发生时间归位。
        plan1 = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan1["id"])
        day1 = self.clock.now()
        self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="completed",
            occurred_at=day1.isoformat(), plan_id=plan1["id"])

        self.clock.advance(days=2)
        self.app.run_daily_scan()
        plan3 = [p for p in self.app.list_plans(self.admin)
                 if p["elder_id"] == "el1" and p["due_date"] == "2026-01-03"][0]
        self.app.claim_plan(self.v1, plan3["id"])
        day3 = self.clock.now()
        # 恢复连接：先发第 3 天的“未遇”。
        self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="not_found",
            occurred_at=day3.isoformat(), plan_id=plan3["id"])
        # 再补发第 2 天积压的记录（当天没有有效计划，仅按老人留事实）。
        day2 = day1.replace(day=2)
        backfill = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="completed",
            occurred_at=day2.isoformat(), elder_id="el1")
        self.assertEqual(backfill["merged_status"], "applied_unlinked")

        with self.store.read() as c:
            rows = [r["occurred_at"] for r in c.execute(
                "SELECT occurred_at FROM visit_records WHERE elder_id='el1' ORDER BY occurred_at, id")]
        self.assertEqual([r[:10] for r in rows],
                         ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_record_against_cancelled_plan_is_kept_not_replayed(self):
        plan = self.make_elder(band="urgent")
        visit_time = self.clock.now()
        self.clock.advance(hours=1)
        # 离线期间：服务员请假，计划被取消并改派。
        self.app.deactivate_worker(self.admin, "v1", "临时请假")
        self.clock.advance(hours=1)
        # 旧客户端带着已取消计划的 id 恢复上报，到访事实保留但不复活任务。
        out = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="completed",
            occurred_at=visit_time.isoformat(), plan_id=plan["id"])
        self.assertEqual(out["merged_status"], "applied_unlinked")
        with self.store.read() as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE elder_id='el1' "
                "AND status IN ('pending','assigned')").fetchone()["n"], 1)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE id=? AND status='cancelled'",
                (plan["id"],)).fetchone()["n"], 1)


class ConcurrentClaimTests(AppTestBase):
    def test_only_one_worker_wins_under_twenty_threads(self):
        # 20 个同村村级人员抢同一条未派计划（全部容量设 0，使其保持待接单）。
        tokens = []
        for i in range(20):
            wid = f"vc{i:02d}"
            tok = f"TOK-CONC{i:02d}-00000001"
            self.app.create_worker(self.admin, wid, f"竞争员{i}", Role.VILLAGER,
                                   town="T9", village="V9", daily_capacity=0, token=tok)
            tokens.append(tok)
        self.app.create_elder(self.admin, "elc", "竞争老人", "T9", "V9", "routine")
        plan = [p for p in self.app.list_plans(self.admin) if p["elder_id"] == "elc"][0]
        self.assertIsNone(plan["assignee_id"])

        results: list[str] = []
        barrier = threading.Barrier(20)

        def race(token: str) -> None:
            actor = self.app.authenticate(token)
            barrier.wait()
            try:
                out = self.app.claim_plan(actor, plan["id"])
                results.append(f"ok:{out['assignee_id']}")
            except (ConflictError, PermissionError_):
                results.append("lost")

        threads = [threading.Thread(target=race, args=(t,)) for t in tokens]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [r for r in results if r.startswith("ok:")]
        self.assertEqual(len(results), 20)
        self.assertEqual(len(winners), 1, f"接单赢家必须唯一，实际: {winners}")
        with self.store.read() as c:
            row = c.execute("SELECT * FROM plans WHERE id=?", (plan["id"],)).fetchone()
        self.assertEqual(row["status"], PlanStatus.ASSIGNED)
        self.assertEqual(row["assignee_id"], winners[0][3:])


class EscalationClockTests(AppTestBase):
    def _raise_urgent_risk(self, villager=None):
        plan = self.make_elder(band="urgent")
        villager = villager or self.v1
        self.app.claim_plan(villager, plan["id"])
        out = self.app.submit_visit(
            villager, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="urgent", risk_description="意识模糊")
        return out["risk_id"]

    def test_urgent_deadline_uses_controlled_clock_across_midnight(self):
        self.clock.set(self.clock.now().replace(hour=23, minute=0))
        rid = self._raise_urgent_risk()
        risk = self.app.list_risks(self.township)[0]
        # 23:00 发现，2 小时时限 = 次日 01:00（跨午夜）。
        self.assertEqual(risk["escalation"]["deadline_at"],
                         "2026-01-02T01:00:00+00:00")

        self.clock.advance(hours=1, minutes=59)
        self.assertEqual(self.app.sweep_due(), [])  # 时限未到不得升级

        self.clock.advance(minutes=2)
        fired = self.app.sweep_due()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["from_level"], "township")
        self.assertEqual(fired[0]["to_level"], "county")

        # 乡镇人员此时无权接手已升县级的事项。
        with self.assertRaises(PermissionError_):
            self.app.take_risk(self.township, rid)

    def test_concern_deadline_is_twenty_four_hours(self):
        plan = self.make_elder(band="concern")
        self.app.claim_plan(self.v1, plan["id"])
        self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="concern", risk_description="情绪低落")
        self.clock.advance(hours=23, minutes=59)
        self.assertEqual(self.app.sweep_due(), [])
        self.clock.advance(minutes=2)
        self.assertEqual(len(self.app.sweep_due()), 1)

    def test_timing_survives_restart_and_sweeps_again(self):
        self.clock.set(self.clock.now().replace(hour=23, minute=0))
        rid = self._raise_urgent_risk()
        self.clock.advance(hours=2, minutes=1)
        self.app.sweep_due()  # 升县级，县级截止 03:00

        # 服务重启：新进程、新连接，升级事项仍在计时，责任链不丢。
        app2, store2 = self.reopen_app()
        risk = app2.get_risk(self.admin, rid)
        actions = [e["action"] for e in risk["chain"]]
        self.assertIn("open", actions)
        self.assertIn("timeout", actions)
        self.assertEqual(
            [e for e in risk["escalations"] if e["state"] == "pending"][0]["deadline_at"],
            "2026-01-02T03:01:00+00:00")

        self.clock.advance(hours=2)
        fired = app2.sweep_due()
        self.assertEqual(len(fired), 1, "重启后县级超时仍须继续催办")
        risk2 = app2.get_risk(self.admin, rid)
        self.assertEqual(sum(1 for e in risk2["chain"] if e["action"] == "timeout"), 2)
        store2.close()

    def test_restart_before_deadline_keeps_escalation_alive(self):
        rid = self._raise_urgent_risk()
        self.clock.advance(minutes=30)
        app2, store2 = self.reopen_app()
        self.assertEqual(app2.sweep_due(), [])
        snap = app2.take_risk(self.township, rid, "重启后仍可接手")
        self.assertEqual(snap["escalation"]["state"], "taken")
        store2.close()


class ConsentTests(AppTestBase):
    def test_withdraw_visit_consent_takes_immediate_effect_but_keeps_audit(self):
        plan = self.make_elder(band="urgent")
        visit_time = self.clock.now()
        self.clock.advance(hours=1)

        self.app.set_consent(self.admin, "el1", "visit", False, "老人书面撤回")
        # 未完成计划立即取消。
        with self.store.read() as c:
            self.assertEqual(c.execute(
                "SELECT status FROM plans WHERE id=?", (plan["id"],)).fetchone()["status"],
                PlanStatus.CANCELLED)
            n_audit = c.execute(
                "SELECT COUNT(*) AS n FROM consent_audit WHERE elder_id='el1'").fetchone()["n"]
        self.assertGreaterEqual(n_audit, 2, "授权与撤回都必须留下审计")

        # 撤回后新发生的到访被拒绝：不完成计划，但记录留存可审计。
        self.clock.advance(hours=1)
        with self.assertRaises(ConsentError) as ctx:
            self.app.submit_visit(
                self.v1, token=new_offline_token(), outcome="completed",
                occurred_at=self.clock.now().isoformat(), plan_id=plan["id"])
        self.assertEqual(ctx.exception.code, "rejected_consent_withdrawn")
        with self.store.read() as c:
            row = c.execute(
                "SELECT * FROM visit_records WHERE merged_status='rejected_consent_withdrawn'").fetchone()
            self.assertIsNotNone(row, "拒绝记录必须作为审计保留")
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE id=? AND status='done'",
                (plan["id"],)).fetchone()["n"], 0)

        # 撤回前实际发生（离线积压）的记录恢复后仍按事实合并。
        ok = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="declined",
            occurred_at=visit_time.isoformat(), plan_id=plan["id"])
        self.assertEqual(ok["merged_status"], "applied_unlinked")

        # 审计行不得因任何操作被删除。
        app2, store2 = self.reopen_app()
        with store2.read() as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM consent_audit WHERE elder_id='el1'").fetchone()["n"],
                n_audit)
        store2.close()

    def test_regrant_visits_resumes_rolling_plan(self):
        self.make_elder(band="urgent")
        self.app.set_consent(self.admin, "el1", "visit", False, "撤回")
        self.assertEqual(
            [p for p in self.app.list_plans(self.admin)
             if p["elder_id"] == "el1" and p["status"] in ("pending", "assigned")],
            [])
        self.app.set_consent(self.admin, "el1", "visit", True, "老人重新同意")
        live = [p for p in self.app.list_plans(self.admin)
                if p["elder_id"] == "el1" and p["status"] == "pending"]
        self.assertEqual(len(live), 1)

    def test_risk_care_withdrawn_blocks_escalation_but_keeps_lead(self):
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        self.app.set_consent(self.admin, "el1", "risk_care", False, "不愿被升级处置")
        out = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="urgent", risk_description="有伤但拒绝升级联系")
        self.assertIsNotNone(out["risk_id"])
        self.clock.advance(hours=10)
        self.assertEqual(self.app.sweep_due(), [], "撤回风险处置授权后不得启动升级计时")
        risks = self.app.list_risks(self.township)
        self.assertIsNone(risks[0]["escalation"])

    def test_family_contact_consent_enforced(self):
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        out = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="concern", risk_description="需要家属照护")
        rid = out["risk_id"]
        self.app.set_consent(self.admin, "el1", "family_contact", False, "撤回家属联系")
        with self.assertRaises(ConsentError):
            self.app.contact_family(self.township, rid)


class ReassignmentTests(AppTestBase):
    def test_worker_inactivation_reassigns_explainably(self):
        plan = self.make_elder(band="routine")
        self.assertEqual(plan["assignee_id"], "v1")
        result = self.app.deactivate_worker(self.admin, "v1", "服务员请假")
        self.assertEqual(result["cancelled_plans"], 1)
        live = [p for p in self.app.list_plans(self.admin)
                if p["elder_id"] == "el1" and p["status"] == "pending"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["assignee_id"], "v2")
        with self.store.read() as c:
            reasons = [r["reason"] for r in c.execute(
                "SELECT reason FROM plan_events WHERE plan_id=? ORDER BY id", (plan["id"],))]
        self.assertEqual(reasons, ["scheduled", "worker_inactive"])

    def test_agency_exit_reassigns_and_blocks_future_offline_records(self):
        plan = self.make_elder(band="routine")
        visit_time = self.clock.now()
        self.clock.advance(hours=2)
        self.app.deactivate_agency(self.admin, "ag1", "机构合同到期退出")
        with self.store.read() as c:
            self.assertEqual(c.execute(
                "SELECT status FROM plans WHERE id=?", (plan["id"],)).fetchone()["status"],
                PlanStatus.CANCELLED)
        # 退出之后发生的记录被拒绝；退出之前发生的仍可合并。
        self.clock.advance(hours=1)
        with self.assertRaises(ConsentError) as ctx:
            self.app.submit_visit(
                self.v1, token=new_offline_token(), outcome="completed",
                occurred_at=self.clock.now().isoformat(), plan_id=plan["id"])
        self.assertEqual(ctx.exception.code, "rejected_agency_exited")
        before = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="completed",
            occurred_at=visit_time.isoformat(), plan_id=plan["id"])
        self.assertEqual(before["merged_status"], "applied_unlinked")

    def test_holiday_defers_plan_to_next_workday(self):
        self.app.add_holiday(self.admin, "2026-01-01", "元旦")
        self.app.create_elder(self.admin, "elh", "节假日老人", "T1", "V1", "routine")
        plan = [p for p in self.app.list_plans(self.admin) if p["elder_id"] == "elh"][0]
        self.assertEqual(plan["due_date"], "2026-01-02")

    def test_hospitalization_pauses_and_discharge_resumes(self):
        plan = self.make_elder(band="routine")
        self.app.set_residence(self.admin, "el1", "hospitalized", "临时住院")
        self.assertEqual(
            [p for p in self.app.list_plans(self.admin)
             if p["elder_id"] == "el1" and p["status"] in ("pending", "assigned")],
            [])
        self.clock.advance(days=5)
        self.app.set_residence(self.admin, "el1", "home", "出院回家")
        live = [p for p in self.app.list_plans(self.admin)
                if p["elder_id"] == "el1" and p["status"] == "pending"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["due_date"], "2026-01-06")


class DailyScanAndExportTests(AppTestBase):
    def test_daily_scan_marks_missed_and_generates_today(self):
        plan = self.make_elder(band="routine")  # 应访 1 月 1 日
        self.clock.advance(days=8)
        report = self.app.run_daily_scan()
        self.assertEqual(report["missed"], 1)
        self.assertIn(plan["id"], report["missed_plan_ids"])
        self.assertGreaterEqual(report["created"], 1)
        with self.store.read() as c:
            self.assertEqual(c.execute(
                "SELECT status FROM plans WHERE id=?", (plan["id"],)).fetchone()["status"],
                PlanStatus.MISSED)

    def test_regulatory_export_contains_full_chain(self):
        self.clock.set(self.clock.now().replace(hour=23, minute=0))
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="urgent", risk_description="昏迷")
        rid = self.app.list_risks(self.township)[0]["id"]
        self.clock.advance(hours=2, minutes=1)
        self.app.sweep_due()
        self.app.take_risk(self.admin, rid, "县级接手处置")
        self.app.contact_family(self.admin, rid, "已联系其子")
        self.app.close_risk(self.admin, rid, "送医后稳定")

        csv_text = self.app.regulatory_export(self.admin, "2026-01-01", "2026-01-03")
        self.assertIn("计划ID", csv_text)
        self.assertIn(plan["id"], csv_text)
        self.assertIn(rid, csv_text)
        self.assertIn("county", csv_text)
        self.assertIn("timed_out", csv_text)
        self.assertIn("closed", csv_text)
        # 责任链动作齐全。
        snap = self.app.get_risk(self.admin, rid)
        actions = [e["action"] for e in snap["chain"]]
        for action in ("open", "open_escalation", "timeout", "escalate",
                       "take", "contact_family", "close"):
            self.assertIn(action, actions)
        # 监管导出仅限县级。
        with self.assertRaises(PermissionError_):
            self.app.regulatory_export(self.township, "2026-01-01", "2026-01-03")

    def test_township_can_only_close_own_taken_risk(self):
        plan = self.make_elder(band="concern")
        self.app.claim_plan(self.v1, plan["id"])
        out = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="concern", risk_description="烫伤")
        rid = out["risk_id"]
        with self.assertRaises(PermissionError_):
            self.app.close_risk(self.township2, rid)          # 外乡镇
        with self.assertRaises(PermissionError_):
            self.app.close_risk(self.township, rid)           # 本乡但未接手
        self.app.take_risk(self.township, rid, "本乡接手")
        self.assertEqual(self.app.close_risk(self.township, rid, "处置完成")["status"], "closed")

    def test_transfer_keeps_deadline_and_blocks_cross_town(self):
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        out = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="urgent", risk_description="走失倾向")
        rid = out["risk_id"]
        before = self.app.list_risks(self.township)[0]["escalation"]["deadline_at"]
        with self.assertRaises(PermissionError_):
            self.app.transfer_risk(self.township, rid, "tw2")  # 不能转外乡镇
        self.app.transfer_risk(self.township, rid, "admin", "需要县级资源")
        after = self.app.list_risks(self.admin)[0]["escalation"]
        self.assertEqual(after["level"], "county")
        self.assertEqual(after["deadline_at"], before, "转派/升级不得重置 SLA 截止时间")

    def test_risk_band_change_changes_frequency(self):
        plan = self.make_elder(band="routine")
        self.app.claim_plan(self.v1, plan["id"])
        self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="completed",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"])
        # 普通风险 7 天一访：第 5 天不应排。
        self.clock.advance(days=4)
        self.app.run_daily_scan()
        self.assertFalse(any(p["due_date"] == "2026-01-05"
                             for p in self.app.list_plans(self.admin) if p["elder_id"] == "el1"))
        # 调整为高风险后：每天一访，次日即排。
        self.app.set_risk_band(self.admin, "el1", "urgent")
        self.clock.advance(days=1)
        self.app.run_daily_scan()
        self.assertTrue(any(p["due_date"] == "2026-01-06"
                            for p in self.app.list_plans(self.admin) if p["elder_id"] == "el1"))


class SchedulerTests(AppTestBase):
    def test_background_scheduler_uses_controlled_clock(self):
        from src.care_visits.http import Scheduler

        self.clock.set(self.clock.now().replace(hour=23, minute=0))
        plan = self.make_elder(band="urgent")
        self.app.claim_plan(self.v1, plan["id"])
        rid = self.app.submit_visit(
            self.v1, token=new_offline_token(), outcome="risk_found",
            occurred_at=self.clock.now().isoformat(), plan_id=plan["id"],
            risk_band="urgent", risk_description="夜间急症")["risk_id"]
        self.clock.advance(hours=2, minutes=1)

        sched = Scheduler(self.app, sweep_interval=0.05)
        sched.start()
        try:
            deadline = threading.Event()
            for _ in range(100):
                risk = self.app.get_risk(self.admin, rid)
                if any(e["action"] == "timeout" for e in risk["chain"]):
                    deadline.set()
                    break
                deadline.wait(0.05)
            self.assertTrue(deadline.wait(2), "后台调度必须按可控时钟触发超时升级")
        finally:
            sched.stop()


if __name__ == "__main__":
    unittest.main()
