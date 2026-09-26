"""角色隔离的 HTTP API（纯 WSGI，无第三方依赖）。

权限矩阵：
  县级 county    建档/机构人员管理、授权变更、节假日、改派、风险全流程、漏访扫描、监管导出
  乡镇 township  看本乡计划与风险、接手/转派/联系家属/关闭（关闭限本人接手）、改派
  村级 villager  看本人计划、并发接单、上报到访/未遇/拒访/风险发现

启动：
    python3 -m src.care_visits.http --db care.db --host 0.0.0.0 --port 8080
首次启动可用：
    python3 -m src.care_visits.http --db care.db --init-admin 县民政 admin
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

from .app import App
from .clock import Clock, SystemClock
from .errors import AppError
from .store import Storage

JSON = "application/json; charset=utf-8"


class HttpApi:
    def __init__(self, app: App) -> None:
        self.app = app
        self._routes: list[tuple[str, str, Callable, bool]] = []
        self._register()

    def route(self, method: str, pattern: str, handler: Callable, auth: bool = True) -> None:
        self._routes.append((method, pattern, handler, auth))

    # ---------------------------------------------------------------- WSGI

    def __call__(self, environ, start_response):
        try:
            path = environ["PATH_INFO"] or "/"
            method = environ["REQUEST_METHOD"]
            body = self._read_body(environ)
            actor: Optional[dict] = None
            for m, pattern, handler, needs_auth in self._routes:
                kwargs = self._match(pattern, path)
                if kwargs is None or m != method:
                    continue
                if needs_auth:
                    actor = self._authenticate(environ)
                result = handler(actor, body, kwargs, environ)
                status, headers, payload = result
                start_response(status, headers)
                return payload
            raise AppError("接口不存在", code="not_found", status=404)
        except AppError as exc:
            data = json.dumps({"error": {"code": exc.code, "message": exc.message}},
                              ensure_ascii=False).encode()
            start_response(f"{exc.status} {exc.status}", [("Content-Type", JSON)])
            return [data]
        except Exception as exc:  # noqa: BLE001 - 兜底，不泄露堆栈
            data = json.dumps({"error": {"code": "internal_error", "message": str(exc)}},
                              ensure_ascii=False).encode()
            start_response("500 500", [("Content-Type", JSON)])
            return [data]

    def _json(self, obj: Any, status: int = 200):
        text = json.dumps(obj, ensure_ascii=False, default=str)
        return f"{status} {status}", [("Content-Type", JSON)], [text.encode()]

    def _csv(self, text: str, filename: str):
        return "200 200", [
            ("Content-Type", "text/csv; charset=utf-8"),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
        ], [text.encode("utf-8")]

    @staticmethod
    def _read_body(environ) -> dict:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if length <= 0:
            return {}
        raw = environ["wsgi.input"].read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AppError("请求体不是合法 JSON", code="bad_json", status=400) from exc
        if not isinstance(data, dict):
            raise AppError("请求体必须是 JSON 对象", status=400)
        return data

    def _authenticate(self, environ) -> dict:
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            raise AppError("缺少 Bearer 令牌", code="unauthorized", status=401)
        return self.app.authenticate(header[7:].strip())

    @staticmethod
    def _match(pattern: str, path: str) -> Optional[dict]:
        ps, xs = pattern.strip("/").split("/"), path.strip("/").split("/")
        if len(ps) != len(xs):
            return None
        kwargs: dict[str, str] = {}
        for p, x in zip(ps, xs):
            if p.startswith("{") and p.endswith("}"):
                kwargs[p[1:-1]] = x
            elif p != x:
                return None
        return kwargs

    @staticmethod
    def _qs(environ, name, default=None):
        qs = parse_qs(environ.get("QUERY_STRING", ""))
        return qs.get(name, [default])[0]

    # ---------------------------------------------------------------- 路由

    def _register(self) -> None:
        r = self.route
        r("GET", "/healthz", self._health, auth=False)
        r("POST", "/bootstrap", self._bootstrap, auth=False)

        r("POST", "/admin/agencies", self._create_agency)
        r("POST", "/admin/agencies/{agency_id}/deactivate", self._deactivate_agency)
        r("POST", "/admin/workers", self._create_worker)
        r("POST", "/admin/workers/{worker_id}/deactivate", self._deactivate_worker)
        r("POST", "/admin/holidays", self._add_holiday)
        r("POST", "/admin/daily-scan", self._daily_scan)
        r("GET", "/admin/export", self._export)

        r("POST", "/elders", self._create_elder)
        r("POST", "/elders/{elder_id}/risk-band", self._set_risk_band)
        r("POST", "/elders/{elder_id}/consent", self._set_consent)
        r("POST", "/elders/{elder_id}/residence", self._set_residence)

        r("GET", "/plans", self._list_plans)
        r("POST", "/plans/generate", self._generate_plans)
        r("POST", "/plans/{plan_id}/claim", self._claim)
        r("POST", "/plans/{plan_id}/reassign", self._reassign)

        r("POST", "/visits", self._submit_visit)

        r("GET", "/risks", self._list_risks)
        r("GET", "/risks/{risk_id}", self._get_risk)
        r("POST", "/risks/{risk_id}/take", self._take)
        r("POST", "/risks/{risk_id}/transfer", self._transfer)
        r("POST", "/risks/{risk_id}/contact-family", self._contact_family)
        r("POST", "/risks/{risk_id}/close", self._close)

    # ---------------------------------------------------------------- 处理函数

    def _health(self, actor, body, kw, env):
        return self._json({"status": "ok"})

    def _bootstrap(self, actor, body, kw, env):
        # 仅允许在系统尚无任何账号时引导首个县级账号。
        with self.app.store.read() as c:
            if c.execute("SELECT COUNT(*) AS n FROM workers").fetchone()["n"] > 0:
                raise AppError("系统已初始化", code="conflict", status=409)
        out = self.app.create_worker(
            None, body["worker_id"], body.get("name", body["worker_id"]), "county",
            town=body.get("town", "county"), token=body.get("token"))
        return self._json(out, 201)

    def _create_agency(self, actor, body, kw, env):
        out = self.app.create_agency(actor, body["id"], body["name"])
        return self._json(out, 201)

    def _deactivate_agency(self, actor, body, kw, env):
        return self._json(self.app.deactivate_agency(
            actor, kw["agency_id"], body.get("reason", "机构退出")))

    def _create_worker(self, actor, body, kw, env):
        out = self.app.create_worker(
            actor, body["id"], body["name"], body["role"],
            town=body.get("town"), village=body.get("village"),
            agency_id=body.get("agency_id"),
            daily_capacity=int(body.get("daily_capacity", 6)),
            token=body.get("token"))
        return self._json(out, 201)

    def _deactivate_worker(self, actor, body, kw, env):
        return self._json(self.app.deactivate_worker(
            actor, kw["worker_id"], body.get("reason", "人员失效")))

    def _add_holiday(self, actor, body, kw, env):
        return self._json(self.app.add_holiday(actor, body["day"], body["name"]))

    def _daily_scan(self, actor, body, kw, env):
        day = body.get("date")
        return self._json(self.app.run_daily_scan(
            datetime.strptime(day, "%Y-%m-%d").date() if day else None))

    def _export(self, actor, body, kw, env):
        start = self._qs(env, "start")
        end = self._qs(env, "end")
        if not start or not end:
            raise AppError("必须提供 start 与 end 查询参数", status=422, code="validation_error")
        text = self.app.regulatory_export(actor, start, end)
        return self._csv(text, f"regulatory_{start}_{end}.csv")

    def _create_elder(self, actor, body, kw, env):
        out = self.app.create_elder(
            actor, body["id"], body["name"], body["town"], body["village"],
            body.get("risk_band", "routine"), scopes=body.get("scopes"))
        return self._json(out, 201)

    def _set_risk_band(self, actor, body, kw, env):
        return self._json(self.app.set_risk_band(actor, kw["elder_id"], body["risk_band"]))

    def _set_consent(self, actor, body, kw, env):
        return self._json(self.app.set_consent(
            actor, kw["elder_id"], body["scope"], bool(body["granted"]),
            body.get("reason", "")))

    def _set_residence(self, actor, body, kw, env):
        return self._json(self.app.set_residence(
            actor, kw["elder_id"], body["status"], body.get("reason", "")))

    def _list_plans(self, actor, body, kw, env):
        return self._json(self.app.list_plans(
            actor, status=self._qs(env, "status"),
            mine=self._qs(env, "mine") in ("1", "true", "yes")))

    def _generate_plans(self, actor, body, kw, env):
        return self._json({"created": self.app.generate_plans()})

    def _claim(self, actor, body, kw, env):
        return self._json(self.app.claim_plan(actor, kw["plan_id"]))

    def _reassign(self, actor, body, kw, env):
        return self._json(self.app.reassign_plan(
            actor, kw["plan_id"], body["to_worker_id"], body.get("reason", "")))

    def _submit_visit(self, actor, body, kw, env):
        out = self.app.submit_visit(
            actor, token=body["token"], outcome=body["outcome"],
            occurred_at=body["occurred_at"], plan_id=body.get("plan_id"),
            elder_id=body.get("elder_id"),
            note=body.get("note", ""), risk_band=body.get("risk_band"),
            risk_description=body.get("risk_description", ""))
        return self._json(out, 201)

    def _list_risks(self, actor, body, kw, env):
        return self._json(self.app.list_risks(actor, status=self._qs(env, "status")))

    def _get_risk(self, actor, body, kw, env):
        return self._json(self.app.get_risk(actor, kw["risk_id"]))

    def _take(self, actor, body, kw, env):
        return self._json(self.app.take_risk(actor, kw["risk_id"], body.get("note", "")))

    def _transfer(self, actor, body, kw, env):
        return self._json(self.app.transfer_risk(
            actor, kw["risk_id"], body["to_worker_id"], body.get("note", "")))

    def _contact_family(self, actor, body, kw, env):
        return self._json(self.app.contact_family(actor, kw["risk_id"], body.get("note", "")))

    def _close(self, actor, body, kw, env):
        return self._json(self.app.close_risk(actor, kw["risk_id"], body.get("note", "")))


def build_api(db_path: str = ":memory:", clock: Optional[Clock] = None) -> tuple[HttpApi, App, Storage]:
    store = Storage(db_path)
    app = App(store, clock or SystemClock())
    return HttpApi(app), app, store


# ---------------------------------------------------------------- 定时扫描线程

class Scheduler(threading.Thread):
    """后台轮询升级时限；每天固定时刻执行一次漏访扫描。计时来自注入时钟。"""

    def __init__(self, app: App, *, sweep_interval: float = 30.0,
                 daily_hour_minute: tuple[int, int] = (0, 5),
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(daemon=True, name="care-scheduler")
        self.app = app
        self.sweep_interval = sweep_interval
        self.daily_hour_minute = daily_hour_minute
        self._sleep = sleep
        self._stop = threading.Event()
        self._last_scan_date: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.app.sweep_due()
                now = self.app.clock.now()
                hh, mm = self.daily_hour_minute
                today = now.date().isoformat()
                if now.hour >= hh and (now.minute >= mm or now.hour > hh) \
                        and self._last_scan_date != today:
                    self.app.run_daily_scan()
                    self._last_scan_date = today
            except Exception:  # noqa: BLE001 - 调度线程不能因单次错误退出
                pass
            self._stop.wait(self.sweep_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="农村养老探访风险协调器")
    parser.add_argument("--db", default="care.db")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--init-admin", nargs=2, metavar=("WORKER_ID", "NAME"))
    parser.add_argument("--admin-town", default="county")
    args = parser.parse_args()

    store = Storage(args.db)
    app = App(store, SystemClock())
    if args.init_admin:
        wid, name = args.init_admin
        try:
            out = app.create_worker(None, wid, name, "county", town=args.admin_town)
            print(f"已创建县级账号 {wid}，令牌：{out['token']}（仅显示一次，请妥善保存）")
        except Exception as exc:  # noqa: BLE001
            print(f"未创建县级账号：{exc}")
        return

    from wsgiref.simple_server import make_server

    scheduler = Scheduler(app)
    scheduler.start()
    httpd = make_server(args.host, args.port, HttpApi(app))
    print(f"协调器已启动：http://{args.host}:{args.port}（数据库 {args.db}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        scheduler.stop()


if __name__ == "__main__":
    main()
