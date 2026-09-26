"""角色隔离的 HTTP API。

仅依赖标准库:``ThreadingHTTPServer`` + 路由表。调用方通过
``Authorization: Bearer <token>`` 携带令牌,令牌到人员的映射由
部署方注入(见 ``make_server`` 的 ``tokens`` 参数)。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .coordinator import Conflict, Coordinator, CoordinatorError, Forbidden, NotFound, Validation
from .enums import Role

ERROR_STATUS = {
    NotFound: 404,
    Forbidden: 403,
    Conflict: 409,
    Validation: 400,
}


def make_server(
    coordinator: Coordinator,
    tokens: dict[str, str],
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """构建 HTTP 服务;``tokens`` 为 令牌 -> 人员ID 的映射。"""

    def routes():
        R = Role
        return [
            ("POST", r"/api/elders", _create_elder, {R.COUNTY}),
            ("GET", r"/api/elders/(?P<id>[^/]+)", _get_elder, None),
            ("POST", r"/api/elders/(?P<id>[^/]+)/consents", _set_consents, {R.COUNTY}),
            ("POST", r"/api/elders/(?P<id>[^/]+)/status", _elder_status, {R.COUNTY}),
            ("POST", r"/api/workers", _create_worker, {R.COUNTY}),
            ("POST", r"/api/workers/(?P<id>[^/]+)/status", _worker_status, {R.COUNTY}),
            ("POST", r"/api/agencies", _create_agency, {R.COUNTY}),
            ("POST", r"/api/agencies/(?P<id>[^/]+)/withdraw", _agency_withdraw, {R.COUNTY}),
            ("GET", r"/api/tasks", _list_tasks, None),
            ("POST", r"/api/tasks/(?P<id>[^/]+)/claim", _claim_task, {R.VILLAGE}),
            ("POST", r"/api/visits", _record_visit, {R.VILLAGE}),
            ("GET", r"/api/alerts", _list_alerts, {R.TOWNSHIP, R.COUNTY}),
            ("GET", r"/api/alerts/(?P<id>[^/]+)/chain", _alert_chain, {R.TOWNSHIP, R.COUNTY}),
            ("POST", r"/api/alerts/(?P<id>[^/]+)/ack", _ack_alert, {R.TOWNSHIP, R.COUNTY}),
            ("POST", r"/api/alerts/(?P<id>[^/]+)/reassign", _reassign_alert, {R.TOWNSHIP, R.COUNTY}),
            ("POST", r"/api/alerts/(?P<id>[^/]+)/contact-family", _contact_family, {R.TOWNSHIP, R.COUNTY}),
            ("POST", r"/api/alerts/(?P<id>[^/]+)/close", _close_alert, {R.TOWNSHIP, R.COUNTY}),
            ("POST", r"/api/escalations/check", _check_escalations, {R.COUNTY}),
            ("GET", r"/api/scan/omissions", _scan_omissions, {R.COUNTY}),
            ("GET", r"/api/export/audit.csv", _export_audit, {R.COUNTY}),
        ]

    # -- 端点处理 ------------------------------------------------------

    def _create_elder(worker, m, body, query):
        return 201, coordinator.register_elder(actor_id=worker["id"], **body)

    def _get_elder(worker, m, body, query):
        return 200, coordinator.elder_view(actor_id=worker["id"], elder_id=m.group("id"))

    def _set_consents(worker, m, body, query):
        return 200, coordinator.set_consents(
            actor_id=worker["id"], elder_id=m.group("id"), scopes=body.get("scopes", [])
        )

    def _elder_status(worker, m, body, query):
        action = body.pop("action", None)
        return 200, coordinator.elder_status(
            actor_id=worker["id"], elder_id=m.group("id"), action=action, **body
        )

    def _create_worker(worker, m, body, query):
        return 201, coordinator.register_worker(actor_id=worker["id"], **body)

    def _worker_status(worker, m, body, query):
        return 200, coordinator.worker_status(
            actor_id=worker["id"], worker_id=m.group("id"), action=body.get("action")
        )

    def _create_agency(worker, m, body, query):
        return 201, coordinator.register_agency(actor_id=worker["id"], **body)

    def _agency_withdraw(worker, m, body, query):
        return 200, coordinator.agency_withdraw(actor_id=worker["id"], agency_id=m.group("id"))

    def _list_tasks(worker, m, body, query):
        tasks = coordinator.list_tasks(
            elder_id=query.get("elder_id"), status=query.get("status")
        )
        if worker["role"] == str(Role.VILLAGE):
            tasks = [
                t for t in tasks
                if t["site_id"] == worker["site_id"] or t["claimed_by"] == worker["id"]
            ]
        return 200, {"tasks": tasks}

    def _claim_task(worker, m, body, query):
        return 200, coordinator.claim_task(worker_id=worker["id"], task_id=m.group("id"))

    def _record_visit(worker, m, body, query):
        return 201, coordinator.record_visit(worker_id=worker["id"], **body)

    def _list_alerts(worker, m, body, query):
        township_id = None
        if worker["role"] == str(Role.TOWNSHIP):
            township_id = worker["township_id"]
        return 200, {
            "alerts": coordinator.list_alerts(status=query.get("status"), township_id=township_id)
        }

    def _alert_chain(worker, m, body, query):
        return 200, {"chain": coordinator.alert_chain(m.group("id"))}

    def _ack_alert(worker, m, body, query):
        return 200, coordinator.acknowledge_alert(alert_id=m.group("id"), handler_id=worker["id"])

    def _reassign_alert(worker, m, body, query):
        return 200, coordinator.reassign_alert(
            alert_id=m.group("id"), actor_id=worker["id"],
            to_handler_id=body.get("to_handler_id"), note=body.get("note", ""),
        )

    def _contact_family(worker, m, body, query):
        return 200, coordinator.contact_family(
            alert_id=m.group("id"), actor_id=worker["id"], note=body.get("note", "")
        )

    def _close_alert(worker, m, body, query):
        return 200, coordinator.close_alert(
            alert_id=m.group("id"), actor_id=worker["id"], note=body.get("note", "")
        )

    def _check_escalations(worker, m, body, query):
        return 200, {"escalated": coordinator.check_escalations()}

    def _scan_omissions(worker, m, body, query):
        return 200, coordinator.scan_omissions(day=query.get("day"))

    def _export_audit(worker, m, body, query):
        return 200, coordinator.export_audit_csv(actor_id=worker["id"]), "text/csv; charset=utf-8"

    route_table = [(method, re.compile(f"^{pattern}$"), fn, roles) for method, pattern, fn, roles in routes()]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默访问日志,测试输出保持干净
            pass

        def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
            if content_type.startswith("application/json"):
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            else:
                data = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authenticate(self):
            header = self.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ").strip()
            worker_id = tokens.get(token)
            if not worker_id:
                return None
            try:
                return coordinator._worker(worker_id)
            except NotFound:
                return None

        def _handle(self, method: str):
            path, _, raw_query = self.path.partition("?")
            query = dict(
                pair.split("=", 1) for pair in raw_query.split("&") if "=" in pair
            )
            worker = self._authenticate()
            if worker is None:
                return self._send(401, {"error": "未认证或令牌无效"})
            for route_method, pattern, fn, roles in route_table:
                match = pattern.match(path)
                if route_method != method or not match:
                    continue
                if roles is not None and worker["role"] not in [str(r) for r in roles]:
                    return self._send(403, {"error": "当前角色无权访问该资源"})
                try:
                    body = {}
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    result = fn(worker, match, body, query)
                    status, payload = result[0], result[1]
                    content_type = result[2] if len(result) > 2 else "application/json; charset=utf-8"
                    return self._send(status, payload, content_type)
                except CoordinatorError as exc:
                    status = next((s for t, s in ERROR_STATUS.items() if isinstance(exc, t)), 400)
                    return self._send(status, {"error": str(exc)})
                except (json.JSONDecodeError, TypeError) as exc:
                    return self._send(400, {"error": f"请求格式错误: {exc}"})
            return self._send(404, {"error": "资源不存在"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


def start_escalation_timer(coordinator: Coordinator, interval_seconds: float = 60.0):
    """后台线程周期性检查升级时限;时钟由协调器注入,重启后状态仍在库中。"""

    stop = threading.Event()

    def loop():
        while not stop.wait(interval_seconds):
            coordinator.check_escalations()

    thread = threading.Thread(target=loop, name="escalation-timer", daemon=True)
    thread.start()
    return stop
