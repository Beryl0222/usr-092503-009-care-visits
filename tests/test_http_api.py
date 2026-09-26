"""HTTP API 的角色隔离与端到端行为测试。"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from src.care_visits import Coordinator, ManualClock
from src.care_visits.http_api import make_server

START = datetime(2026, 9, 20, 8, 0, 0)
TOKENS = {"tok-county": "w-county", "tok-town": "w-town", "tok-village": "w-v1"}


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.coord = Coordinator(Path(cls.tmp.name) / "api.db", clock=ManualClock(START))
        cls.coord.register_worker(
            actor_id=None, name="县民政", role="county",
            site_id="county-1", township_id="county-1", worker_id="w-county",
        )
        cls.coord.register_worker(
            actor_id="w-county", name="乡镇干事", role="township",
            site_id="town-1", township_id="town-1", worker_id="w-town",
        )
        cls.coord.register_worker(
            actor_id="w-county", name="村服务员", role="village",
            site_id="village-1", township_id="town-1", worker_id="w-v1",
        )
        cls.coord.register_elder(
            actor_id="w-county", name="张奶奶", risk_level="high",
            site_id="village-1", township_id="town-1", elder_id="e-1",
        )
        cls.server = make_server(cls.coord, TOKENS)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.coord.close()
        cls.tmp.cleanup()

    def call(self, method, path, token=None, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (raw if "csv" in ctype else json.loads(raw))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_unauthenticated_rejected(self):
        status, body = self.call("GET", "/api/tasks")
        self.assertEqual(status, 401)

    def test_role_isolation_on_export_and_scan(self):
        status, _ = self.call("GET", "/api/export/audit.csv", token="tok-village")
        self.assertEqual(status, 403)
        status, _ = self.call("GET", "/api/scan/omissions", token="tok-town")
        self.assertEqual(status, 403)
        status, body = self.call("GET", "/api/scan/omissions", token="tok-county")
        self.assertEqual(status, 200)
        self.assertIn("missed", body)
        status, body = self.call("GET", "/api/export/audit.csv", token="tok-county")
        self.assertEqual(status, 200)
        self.assertIn("visit.recorded", body)

    def test_village_cannot_manage_alerts_or_consents(self):
        status, _ = self.call("GET", "/api/alerts", token="tok-village")
        self.assertEqual(status, 403)
        status, _ = self.call(
            "POST", "/api/elders/e-1/consents", token="tok-village", body={"scopes": []}
        )
        self.assertEqual(status, 403)

    def test_offline_visit_upload_and_idempotent_replay_over_http(self):
        payload = {
            "elder_id": "e-1", "outcome": "completed",
            "offline_token": "OFF-HTTP0001-HTTP0001", "occurred_at": START.isoformat(),
        }
        status, first = self.call("POST", "/api/visits", token="tok-village", body=payload)
        self.assertEqual(status, 201)
        self.assertFalse(first["idempotent"])
        status, again = self.call("POST", "/api/visits", token="tok-village", body=payload)
        self.assertEqual(status, 201)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["task"]["id"], first["task"]["id"])

    def test_risk_alert_escalation_chain_over_http(self):
        payload = {
            "elder_id": "e-1", "outcome": "risk_found",
            "offline_token": "OFF-HTTP0002-HTTP0002", "occurred_at": START.isoformat(),
            "risk": {"band": "urgent", "detail": "燃气未关"},
        }
        status, result = self.call("POST", "/api/visits", token="tok-village", body=payload)
        self.assertEqual(status, 201)
        alert_id = result["alert_id"]
        status, alert = self.call("POST", f"/api/alerts/{alert_id}/ack", token="tok-town")
        self.assertEqual(status, 200)
        self.assertEqual(alert["status"], "acked")
        status, _ = self.call(
            "POST", f"/api/alerts/{alert_id}/contact-family",
            token="tok-town", body={"note": "已联系家属"},
        )
        self.assertEqual(status, 200)
        status, _ = self.call(
            "POST", f"/api/alerts/{alert_id}/close", token="tok-town", body={"note": "处置完成"}
        )
        self.assertEqual(status, 200)
        status, body = self.call("GET", f"/api/alerts/{alert_id}/chain", token="tok-county")
        self.assertEqual(status, 200)
        self.assertEqual(
            [e["action"] for e in body["chain"]],
            ["raise", "ack", "contact_family", "close"],
        )

    def test_county_only_registration(self):
        status, _ = self.call(
            "POST", "/api/workers", token="tok-town",
            body={"name": "越权", "role": "village", "site_id": "v", "township_id": "t"},
        )
        self.assertEqual(status, 403)
        status, worker = self.call(
            "POST", "/api/workers", token="tok-county",
            body={"name": "新村员", "role": "village", "site_id": "village-1", "township_id": "town-1"},
        )
        self.assertEqual(status, 201)
        self.assertIn("id", worker)


if __name__ == "__main__":
    unittest.main()
