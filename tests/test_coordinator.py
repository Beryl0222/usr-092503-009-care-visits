"""协调器核心行为测试:幂等、并发、可控时钟、授权、重启恢复。"""

import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from src.care_visits import (
    Conflict,
    Coordinator,
    Forbidden,
    ManualClock,
    RiskBand,
    Validation,
    VisitOutcome,
)

START = datetime(2026, 9, 20, 8, 0, 0)


def make_coordinator(db_path, clock=None):
    coord = Coordinator(db_path, clock=clock or ManualClock(START))
    workers = [
        (None, "县民政", "county", "county-1", "county-1", "w-county"),
        ("w-county", "乡镇干事", "township", "town-1", "town-1", "w-town"),
        ("w-county", "村服务员甲", "village", "village-1", "town-1", "w-v1"),
        ("w-county", "村服务员乙", "village", "village-1", "town-1", "w-v2"),
    ]
    for actor_id, name, role, site_id, township_id, worker_id in workers:
        if coord.store.query_one("SELECT id FROM workers WHERE id = ?", (worker_id,)):
            continue  # 重启场景:人员已在库中
        coord.register_worker(
            actor_id=actor_id, name=name, role=role,
            site_id=site_id, township_id=township_id, worker_id=worker_id,
        )
    return coord


def make_elder(coord, **kwargs):
    defaults = dict(
        actor_id="w-county", name="张奶奶", risk_level="high",
        site_id="village-1", township_id="town-1",
        address="幸福村 3 号", health_notes="高血压",
        family_contacts=[{"name": "张强", "phone": "13800000000"}],
        elder_id="e-1",
    )
    defaults.update(kwargs)
    return coord.register_elder(**defaults)


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.clock = ManualClock(START)
        self.coord = make_coordinator(self.db, self.clock)

    def tearDown(self):
        self.coord.close()
        self.tmp.cleanup()


class PlanTests(CoordinatorTestBase):
    def test_rolling_plan_follows_risk_cadence(self):
        make_elder(self.coord)
        tasks = self.coord.list_tasks(elder_id="e-1")
        dues = [t["due_date"] for t in tasks]
        # 高风险 7 天一访,计划窗口 14 天:今天、+7、+14
        self.assertEqual(dues, ["2026-09-20", "2026-09-27", "2026-10-04"])

    def test_hospitalization_pauses_and_resume_regenerates(self):
        make_elder(self.coord)
        result = self.coord.elder_status(
            actor_id="w-county", elder_id="e-1", action="hospitalize", until="2026-09-25"
        )
        self.assertTrue(result["explanations"])
        self.assertEqual(self.coord.list_tasks(elder_id="e-1", status="planned"), [])
        with self.assertRaises(Validation):
            self.coord.record_visit(
                worker_id="w-v1", elder_id="e-1", outcome="completed",
                offline_token="OFF-AAAA0001-AAAA0001", occurred_at=self.clock.now(),
            )
        self.coord.elder_status(actor_id="w-county", elder_id="e-1", action="return_home")
        self.assertTrue(self.coord.list_tasks(elder_id="e-1", status="planned"))


class OfflineMergeTests(CoordinatorTestBase):
    def test_duplicate_offline_token_is_idempotent(self):
        make_elder(self.coord)
        first = self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="completed",
            offline_token="OFF-AAAA0001-AAAA0001", occurred_at=self.clock.now(),
        )
        self.assertFalse(first["idempotent"])
        again = self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="completed",
            offline_token="OFF-AAAA0001-AAAA0001", occurred_at=self.clock.now(),
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["task"]["id"], first["task"]["id"])
        done = self.coord.list_tasks(elder_id="e-1", status="done")
        self.assertEqual(len(done), 1)
        audits = self.coord.store.query_all(
            "SELECT * FROM audit_log WHERE action = 'visit.recorded'"
        )
        self.assertEqual(len(audits), 1)

    def test_duplicate_risk_report_does_not_duplicate_alert(self):
        make_elder(self.coord)
        payload = dict(
            worker_id="w-v1", elder_id="e-1", outcome="risk_found",
            offline_token="OFF-BBBB0002-BBBB0002", occurred_at=self.clock.now(),
            risk={"band": "urgent", "detail": "老人倒地"},
        )
        first = self.coord.record_visit(**payload)
        again = self.coord.record_visit(**payload)
        self.assertTrue(again["idempotent"])
        self.assertIsNotNone(first["alert_id"])
        self.assertEqual(len(self.coord.list_alerts()), 1)

    def test_merge_follows_occurrence_order_not_arrival(self):
        make_elder(self.coord)
        later = self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="completed",
            offline_token="OFF-CCCC0003-CCCC0003",
            occurred_at=datetime(2026, 9, 20, 7, 0, 0),
        )
        earlier = self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="completed",
            offline_token="OFF-CCCC0004-CCCC0004",
            occurred_at=datetime(2026, 9, 20, 6, 0, 0),
        )
        done = sorted(
            self.coord.list_tasks(elder_id="e-1", status="done"),
            key=lambda t: t["merged_seq"],
        )
        self.assertEqual(
            [t["id"] for t in done], [earlier["task"]["id"], later["task"]["id"]]
        )


class ClaimTests(CoordinatorTestBase):
    def test_concurrent_claim_only_one_wins(self):
        make_elder(self.coord)
        task = self.coord.list_tasks(elder_id="e-1", status="planned")[0]
        results, errors = [], []
        barrier = threading.Barrier(2)

        def claim(worker_id):
            try:
                barrier.wait(timeout=5)
                results.append(self.coord.claim_task(worker_id=worker_id, task_id=task["id"]))
            except Conflict as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=claim, args=("w-v1",)),
            threading.Thread(target=claim, args=("w-v2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        final = [t for t in self.coord.list_tasks(elder_id="e-1") if t["id"] == task["id"]][0]
        self.assertEqual(final["status"], "claimed")
        self.assertIn(final["claimed_by"], ("w-v1", "w-v2"))

    def test_worker_on_leave_cannot_claim(self):
        make_elder(self.coord)
        task = self.coord.list_tasks(elder_id="e-1", status="planned")[0]
        self.coord.worker_status(actor_id="w-county", worker_id="w-v1", action="leave")
        with self.assertRaises(Forbidden):
            self.coord.claim_task(worker_id="w-v1", task_id=task["id"])


class EscalationTests(CoordinatorTestBase):
    def _raise_urgent(self):
        make_elder(self.coord)
        return self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="risk_found",
            offline_token="OFF-DDDD0005-DDDD0005", occurred_at=self.clock.now(),
            risk={"band": "urgent", "detail": "老人意识模糊"},
        )["alert_id"]

    def test_escalation_uses_controllable_clock(self):
        alert_id = self._raise_urgent()
        self.clock.advance(minutes=119)
        self.assertEqual(self.coord.check_escalations(), [])
        self.clock.advance(minutes=1)
        fired = self.coord.check_escalations()
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["alert_id"], alert_id)
        self.assertEqual(fired[0]["escalated_to"], "w-town")
        # 重复扫描不会重复升级
        self.assertEqual(self.coord.check_escalations(), [])
        actions = [e["action"] for e in self.coord.alert_chain(alert_id)]
        self.assertEqual(actions, ["raise", "escalate"])

    def test_deadline_crosses_midnight(self):
        self.clock.advance(hours=15, minutes=30)  # 2026-09-20 23:30
        alert_id = self._raise_urgent()
        alert = [a for a in self.coord.list_alerts() if a["id"] == alert_id][0]
        self.assertEqual(alert["deadline_at"], "2026-09-21T01:30:00")
        self.clock.advance(minutes=119)  # 01:29 仍未到时限
        self.assertEqual(self.coord.check_escalations(), [])
        self.clock.advance(minutes=1)  # 01:30 到期
        self.assertEqual(len(self.coord.check_escalations()), 1)

    def test_restart_keeps_escalation_timer_and_chain(self):
        alert_id = self._raise_urgent()
        self.coord.close()
        # 服务重启:同一数据库、同一时钟,升级事项继续计时
        self.coord = make_coordinator(self.db, self.clock)
        self.clock.advance(minutes=130)
        fired = self.coord.check_escalations()
        self.assertEqual(len(fired), 1)
        self.coord.acknowledge_alert(alert_id=alert_id, handler_id="w-town")
        self.coord.contact_family(alert_id=alert_id, actor_id="w-town", note="已通知儿子")
        self.coord.close()
        # 再次重启,责任链完整,已接手线索不再升级
        self.coord = make_coordinator(self.db, self.clock)
        self.assertEqual(self.coord.check_escalations(), [])
        actions = [e["action"] for e in self.coord.alert_chain(alert_id)]
        self.assertEqual(actions, ["raise", "escalate", "ack", "contact_family"])

    def test_concurrent_ack_only_one_wins(self):
        alert_id = self._raise_urgent()
        self.coord.register_worker(
            actor_id="w-county", name="县值班", role="county",
            site_id="county-1", township_id="county-1", worker_id="w-county2",
        )
        results, errors = [], []
        barrier = threading.Barrier(2)

        def ack(handler_id):
            try:
                barrier.wait(timeout=5)
                results.append(self.coord.acknowledge_alert(alert_id=alert_id, handler_id=handler_id))
            except Conflict as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=ack, args=("w-town",)),
            threading.Thread(target=ack, args=("w-county2",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)


class ConsentTests(CoordinatorTestBase):
    def test_withdrawal_takes_effect_immediately_and_audit_survives(self):
        make_elder(self.coord)
        view = self.coord.elder_view(actor_id="w-town", elder_id="e-1")
        self.assertIn("family_contacts", view)
        self.assertIn("health_notes", view)
        self.coord.set_consents(
            actor_id="w-county", elder_id="e-1", scopes=["profile"]
        )
        view = self.coord.elder_view(actor_id="w-town", elder_id="e-1")
        self.assertNotIn("family_contacts", view)
        self.assertNotIn("health_notes", view)
        self.assertIn("address", view)  # 基本授权仍在
        # 既有审计(含撤回前的查看记录)依法保留
        actions = [r["action"] for r in self.coord.store.query_all("SELECT * FROM audit_log")]
        self.assertIn("elder.family_viewed", actions)
        self.assertIn("consent.updated", actions)

    def test_contact_family_blocked_after_withdrawal(self):
        make_elder(self.coord)
        alert_id = self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="risk_found",
            offline_token="OFF-EEEE0006-EEEE0006", occurred_at=self.clock.now(),
            risk={"band": "urgent"},
        )["alert_id"]
        self.coord.acknowledge_alert(alert_id=alert_id, handler_id="w-town")
        self.coord.set_consents(actor_id="w-county", elder_id="e-1", scopes=["profile", "health"])
        with self.assertRaises(Forbidden):
            self.coord.contact_family(alert_id=alert_id, actor_id="w-town")

    def test_deactivated_elder_stops_visits(self):
        make_elder(self.coord)
        self.coord.elder_status(actor_id="w-county", elder_id="e-1", action="deactivate")
        with self.assertRaises(Forbidden):
            self.coord.record_visit(
                worker_id="w-v1", elder_id="e-1", outcome="completed",
                offline_token="OFF-FFFF0007-FFFF0007", occurred_at=self.clock.now(),
            )
        with self.assertRaises(Forbidden):
            self.coord.elder_view(actor_id="w-town", elder_id="e-1")


class ReassignmentTests(CoordinatorTestBase):
    def test_worker_leave_releases_tasks_and_alerts(self):
        make_elder(self.coord)
        # 认领未来到期的任务,避免被当天的风险上报合并掉
        task = [t for t in self.coord.list_tasks(elder_id="e-1", status="planned")
                if t["due_date"] == "2026-09-27"][0]
        self.coord.claim_task(worker_id="w-v1", task_id=task["id"])
        alert_id = self.coord.record_visit(
            worker_id="w-v2", elder_id="e-1", outcome="risk_found",
            offline_token="OFF-AAAA1001-AAAA1001", occurred_at=self.clock.now(),
            risk={"band": "urgent"},
        )["alert_id"]
        self.coord.acknowledge_alert(alert_id=alert_id, handler_id="w-town")
        result = self.coord.worker_status(actor_id="w-county", worker_id="w-v1", action="leave")
        self.assertTrue(any("已释放" in e for e in result["explanations"]))
        final = [t for t in self.coord.list_tasks(elder_id="e-1") if t["id"] == task["id"]][0]
        self.assertEqual(final["status"], "planned")
        self.assertIsNone(final["claimed_by"])
        # 乡镇干事请假,手中线索自动转派给县级
        result = self.coord.worker_status(actor_id="w-county", worker_id="w-town", action="leave")
        self.assertTrue(any("转派" in e for e in result["explanations"]))
        chain = self.coord.alert_chain(alert_id)
        self.assertEqual(chain[-1]["action"], "reassign")
        self.assertEqual(chain[-1]["from_handler"], "w-town")
        self.assertEqual(chain[-1]["to_handler"], "w-county")

    def test_agency_withdrawal_returns_tasks_to_pool(self):
        self.coord.register_agency(actor_id="w-county", name="夕阳红社工", agency_id="ag-1")
        make_elder(self.coord)
        tasks = self.coord.list_tasks(elder_id="e-1", status="planned")
        self.assertTrue(all(t["agency_id"] == "ag-1" for t in tasks))
        result = self.coord.agency_withdraw(actor_id="w-county", agency_id="ag-1")
        self.assertEqual(result["affected"], len(tasks))
        for t in self.coord.list_tasks(elder_id="e-1", status="planned"):
            self.assertIsNone(t["agency_id"])

    def test_relocation_moves_plan_to_new_site(self):
        make_elder(self.coord)
        result = self.coord.elder_status(
            actor_id="w-county", elder_id="e-1", action="relocate",
            site_id="village-2", township_id="town-2",
        )
        self.assertTrue(result["explanations"])
        planned = self.coord.list_tasks(elder_id="e-1", status="planned")
        self.assertTrue(planned)
        self.assertTrue(all(t["site_id"] == "village-2" for t in planned))


class ScanAndExportTests(CoordinatorTestBase):
    def test_daily_omission_scan(self):
        make_elder(self.coord)
        self.clock.advance(days=1)  # 昨天的任务已逾期
        scan = self.coord.scan_omissions()
        self.assertEqual(scan["day"], "2026-09-21")
        self.assertEqual(len(scan["missed"]), 1)
        self.assertEqual(scan["missed"][0]["due_date"], "2026-09-20")
        self.assertEqual(scan["plan_gaps"], [])

    def test_audit_export_csv(self):
        make_elder(self.coord)
        self.coord.record_visit(
            worker_id="w-v1", elder_id="e-1", outcome="completed",
            offline_token="OFF-AAAA2002-AAAA2002", occurred_at=self.clock.now(),
        )
        csv_text = self.coord.export_audit_csv(actor_id="w-county")
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], "seq,actor_id,action,target,at,detail")
        self.assertTrue(any("visit.recorded" in line for line in lines))
        self.assertTrue(any("elder.registered" in line for line in lines))
        with self.assertRaises(Forbidden):
            # 乡镇角色不能导出监管数据
            self.coord.export_audit_csv(actor_id="w-town")


if __name__ == "__main__":
    unittest.main()
