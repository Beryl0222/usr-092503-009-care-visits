"""HTTP API 测试：起真实 wsgiref 服务器，验证鉴权、角色隔离与端到端业务流。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from wsgiref.simple_server import make_server
from urllib.parse import urlencode

from src.care_visits.app import App
from src.care_visits.clock import FakeClock
from src.care_visits.contracts import Role, new_offline_token
from src.care_visits.http import HttpApi
from src.care_visits.store import Storage


class HttpTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "care.db")
        self.clock = FakeClock()
        self.store = Storage(self.db)
        self.app = App(self.store, self.clock)
        self.http = HttpApi(self.app)
        self.server = make_server("127.0.0.1", 0, self.http)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._seed()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.tmp.cleanup()

    def _seed(self) -> None:
        self.app.create_worker(None, "admin", "县管理员", Role.COUNTY,
                               town="COUNTY", token="TOK-ADMIN00-00000001")
        self.admin_tok = "TOK-ADMIN00-00000001"
        self.app.create_agency(self.app.authenticate(self.admin_tok), "ag1", "阳光社工")
        self.app.create_worker(self.app.authenticate(self.admin_tok), "tw1", "乡民政",
                               Role.TOWNSHIP, town="T1", token="TOK-TOWN00-00000001")
        self.town_tok = "TOK-TOWN00-00000001"
        self.app.create_worker(self.app.authenticate(self.admin_tok), "tw9", "他乡民政",
                               Role.TOWNSHIP, town="T9", token="TOK-TOWN00-00000009")
        self.other_town_tok = "TOK-TOWN00-00000009"
        self.app.create_worker(self.app.authenticate(self.admin_tok), "v1", "村员甲",
                               Role.VILLAGER, town="T1", village="V1", agency_id="ag1",
                               token="TOK-VILL00-00000001")
        self.v1_tok = "TOK-VILL00-00000001"
        self.app.create_worker(self.app.authenticate(self.admin_tok), "v2", "村员乙",
                               Role.VILLAGER, town="T1", village="V1",
                               token="TOK-VILL00-00000002")
        self.v2_tok = "TOK-VILL00-00000002"
        self.app.create_elder(self.app.authenticate(self.admin_tok),
                              "el1", "张奶奶", "T1", "V1", "urgent")
        self.plan_id = [p for p in self.app.list_plans(self.app.authenticate(self.admin_tok))
                        if p["elder_id"] == "el1"][0]["id"]

    def req(self, method: str, path: str, token: str | None = None,
            body: dict | None = None, raw: bool = False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, ctype, (payload if raw else json.loads(payload))
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, exc.headers.get("Content-Type", ""), json.loads(payload)
            except json.JSONDecodeError:
                return exc.code, exc.headers.get("Content-Type", ""), payload


class AuthAndRoleTests(HttpTestBase):
    def test_health_and_bootstrap(self):
        status, _, body = self.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        # 已有账号，bootstrap 必须 409。
        status, _, body = self.req("POST", "/bootstrap", body={
            "worker_id": "x", "name": "y", "token": "TOK-X00000-00000001"})
        self.assertEqual(status, 409)

    def test_missing_or_bad_token_is_401(self):
        status, _, body = self.req("GET", "/plans")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, _, _ = self.req("GET", "/plans", token="TOK-00000000-00000000")
        self.assertEqual(status, 401)

    def test_role_isolation_matrix(self):
        # 村级不能建档/导出/改派
        status, _, body = self.req("POST", "/elders", self.v1_tok,
                                   {"id": "elx", "name": "x", "town": "T1", "village": "V1"})
        self.assertEqual(status, 403)
        qs = urlencode({"start": "2026-01-01", "end": "2026-01-31"})
        status, _, _ = self.req("GET", f"/admin/export?{qs}", self.town_tok)
        self.assertEqual(status, 403)

        # 乡镇不能建账号
        status, _, _ = self.req("POST", "/admin/workers", self.town_tok,
                                {"id": "w", "name": "w", "role": "villager",
                                 "town": "T1", "village": "V1"})
        self.assertEqual(status, 403)

        # 乡镇看不到他乡计划（el1 在 T1，T9 查询为空）
        status, _, body = self.req("GET", "/plans", self.other_town_tok)
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

        # 村级只能看到派给自己的计划：v2 看不到 el1 的计划（派给了 v1）
        status, _, body = self.req("GET", "/plans", self.v2_tok)
        self.assertEqual(status, 200)
        self.assertEqual(body, [])
        status, _, body = self.req("GET", "/plans", self.v1_tok)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), 1)

    def test_villager_cannot_claim_other_township_plan(self):
        # T9 乡有一条未派计划，T1 的 v1 不能接（跨乡镇）；本乡内跨村可以。
        self.app.create_worker(self.app.authenticate(self.admin_tok), "v9", "他乡村员",
                               Role.VILLAGER, town="T9", village="V9", daily_capacity=0,
                               token="TOK-VILL00-00000009")
        self.app.create_elder(self.app.authenticate(self.admin_tok),
                              "el9", "他乡老人", "T9", "V9", "routine")
        plan9 = [p for p in self.app.list_plans(self.app.authenticate(self.admin_tok))
                 if p["elder_id"] == "el9"][0]
        status, _, body = self.req("POST", f"/plans/{plan9['id']}/claim", self.v1_tok, {})
        self.assertEqual(status, 403)

        # 本乡 V2 村的未派计划，V1 村的 v1 可接（乡镇统筹跨村支援）
        with self.store.begin() as c:
            c.execute("UPDATE workers SET daily_capacity=0 WHERE town='T1' AND role='villager'")
        self.app.create_elder(self.app.authenticate(self.admin_tok),
                              "el2", "李爷爷", "T1", "V2", "routine")
        with self.store.begin() as c:
            c.execute("UPDATE workers SET daily_capacity=6 WHERE town='T1' AND role='villager'")
        plan2 = [p for p in self.app.list_plans(self.app.authenticate(self.admin_tok))
                 if p["elder_id"] == "el2"][0]
        self.assertIsNone(plan2["assignee_id"])
        status, _, body = self.req("POST", f"/plans/{plan2['id']}/claim", self.v1_tok, {})
        self.assertEqual(status, 200)


class EndToEndFlowTests(HttpTestBase):
    def test_claim_conflict_offline_idempotency_and_export(self):
        # 构造一条 V1 村的未派计划（临时把承载量清零），两个村员并发抢单。
        with self.store.begin() as c:
            c.execute("UPDATE workers SET daily_capacity=0 WHERE id IN ('v1','v2')")
        self.app.create_elder(self.app.authenticate(self.admin_tok),
                              "el3", "王大爷", "T1", "V1", "routine")
        with self.store.begin() as c:
            c.execute("UPDATE workers SET daily_capacity=6 WHERE id IN ('v1','v2')")
        plan3 = [p for p in self.app.list_plans(self.app.authenticate(self.admin_tok))
                 if p["elder_id"] == "el3"][0]
        self.assertIsNone(plan3["assignee_id"])

        outcomes = []

        def claim(tok):
            outcomes.append(self.req("POST", f"/plans/{plan3['id']}/claim", tok, {}))

        t1 = threading.Thread(target=claim, args=(self.v1_tok,))
        t2 = threading.Thread(target=claim, args=(self.v2_tok,))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(o[0] for o in outcomes)
        self.assertEqual(statuses, [200, 409], f"并发接单必须一成一冲突: {statuses}")
        loser = next(o for o in outcomes if o[0] == 409)
        self.assertEqual(loser[2]["error"]["code"], "conflict")

        # v1 对自己的 el1 计划正常接单，v2 再抢已派给他人的计划得到 409（已被接走）
        status, _, body = self.req("POST", f"/plans/{self.plan_id}/claim", self.v1_tok, {})
        self.assertEqual(status, 200)
        status, _, body = self.req("POST", f"/plans/{self.plan_id}/claim", self.v2_tok, {})
        self.assertEqual(status, 409)

        # 离线风险上报
        token = new_offline_token()
        payload = {
            "token": token, "outcome": "risk_found",
            "occurred_at": self.clock.now().isoformat(),
            "plan_id": self.plan_id, "risk_band": "urgent",
            "risk_description": "老人跌倒，意识清醒但无法站立",
        }
        status, ctype, first = self.req("POST", "/visits", self.v1_tok, payload)
        self.assertEqual(status, 201)
        self.assertFalse(first["duplicate"])
        rid = first["risk_id"]

        # 网络抖动重试：同一凭证重复上报，幂等
        status, _, second = self.req("POST", "/visits", self.v1_tok, payload)
        self.assertEqual(status, 201)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["visit_id"], first["visit_id"])

        # 乡镇看到线索，2 小时内未处置 -> 超时升县级
        status, _, risks = self.req("GET", "/risks", self.town_tok)
        self.assertEqual(status, 200)
        self.assertEqual(len(risks), 1)
        self.clock.advance(hours=2, minutes=1)
        status, _, scan = self.req("POST", "/admin/daily-scan", self.admin_tok, {})
        self.assertEqual(status, 200)
        self.assertEqual(len(scan["escalations_fired"]), 1)

        # 乡镇此时不能接手（已升县级），县级可以
        status, _, body = self.req("POST", f"/risks/{rid}/take", self.town_tok,
                                   {"note": "迟来的接手"})
        self.assertEqual(status, 403)
        status, _, body = self.req("POST", f"/risks/{rid}/take", self.admin_tok,
                                   {"note": "县级上门处置"})
        self.assertEqual(status, 200)
        self.assertEqual(body["escalation"]["level"], "county")

        # 联系家属、关闭都留责任链
        status, _, _ = self.req("POST", f"/risks/{rid}/contact-family", self.admin_tok,
                                {"note": "电话联系其子，1 小时内到家"})
        self.assertEqual(status, 200)
        status, _, _ = self.req("POST", f"/risks/{rid}/close", self.admin_tok,
                                {"note": "送医检查无大碍，子女陪护"})
        self.assertEqual(status, 200)

        status, _, detail = self.req("GET", f"/risks/{rid}", self.admin_tok)
        actions = [e["action"] for e in detail["chain"]]
        self.assertEqual(actions, [
            "open", "open_escalation", "timeout", "escalate",
            "take", "contact_family", "close"])

        # 监管导出：县级 CSV，含超时升级与关闭
        qs = urlencode({"start": "2026-01-01", "end": "2026-01-03"})
        status, ctype, raw = self.req("GET", f"/admin/export?{qs}", self.admin_tok, raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/csv", ctype)
        text = raw.decode("utf-8-sig")
        self.assertIn("计划ID", text)
        self.assertIn("timed_out", text)
        self.assertIn("township -> county", text)  # 两级升级轨迹
        self.assertIn("timed_out -> closed", text)
        self.assertIn("closed", text)

    def test_consent_withdraw_blocks_new_visit_immediately(self):
        self.req("POST", f"/plans/{self.plan_id}/claim", self.v1_tok, {})
        # 县级撤回探访授权
        status, _, body = self.req("POST", "/elders/el1/consent", self.admin_tok,
                                   {"scope": "visit", "granted": False, "reason": "老人撤回"})
        self.assertEqual(status, 200)
        self.assertFalse(body["granted"])

        # 计划已取消；之后的到访 403
        self.clock.advance(hours=1)
        status, _, body = self.req("POST", "/visits", self.v1_tok, {
            "token": new_offline_token(), "outcome": "completed",
            "occurred_at": self.clock.now().isoformat(), "plan_id": self.plan_id})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "rejected_consent_withdrawn")

        # 但该拒绝记录在监管视角仍可审计（记录数为 1，且没有 done 计划）
        with self.store.read() as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM visit_records").fetchone()["n"], 1)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) AS n FROM consent_audit WHERE elder_id='el1'").fetchone()["n"], 4)

    def test_restart_process_keeps_risk_chain_and_deadline(self):
        self.req("POST", f"/plans/{self.plan_id}/claim", self.v1_tok, {})
        self.req("POST", "/visits", self.v1_tok, {
            "token": new_offline_token(), "outcome": "risk_found",
            "occurred_at": self.clock.now().isoformat(), "plan_id": self.plan_id,
            "risk_band": "concern", "risk_description": "持续低烧"})
        rid = self.app.list_risks(self.app.authenticate(self.town_tok))[0]["id"]

        # 模拟服务重启：丢弃进程内对象，用同一数据库文件重建。
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.store.close()
        self.store = Storage(self.db)
        self.app = App(self.store, self.clock)
        self.http = HttpApi(self.app)
        self.server = make_server("127.0.0.1", 0, self.http)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        # 23 小时未超时，乡镇仍可接手；责任链完整
        self.clock.advance(hours=23)
        status, _, body = self.req("POST", f"/risks/{rid}/take", self.town_tok,
                                   {"note": "重启后接手"})
        self.assertEqual(status, 200)
        self.assertEqual(body["escalation"]["state"], "taken")
        status, _, detail = self.req("GET", f"/risks/{rid}", self.town_tok)
        self.assertEqual([e["action"] for e in detail["chain"]],
                         ["open", "open_escalation", "take"])

    def test_validation_errors_are_structured(self):
        status, _, body = self.req("POST", "/visits", self.v1_tok, {
            "token": "BAD-TOKEN", "outcome": "completed",
            "occurred_at": self.clock.now().isoformat(), "plan_id": self.plan_id})
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "validation_error")

        status, _, body = self.req("POST", f"/plans/{self.plan_id}/claim", self.v1_tok)
        # body=None（非 JSON 请求体）也能正常处理
        self.assertIn(status, (400, 404, 409, 200))


if __name__ == "__main__":
    unittest.main()
