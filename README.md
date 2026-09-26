# 农村养老探访风险协调器

县、乡、村三级协同的探访服务端协调器：按老人授权范围、风险等级、居住状态与服务能力
生成滚动探访计划；村级客户端凭唯一凭证离线记录到访/未遇/拒访/风险发现，恢复连接后
按**发生时间**合并且重复上报幂等；高风险线索按**可控时钟**计时升级，接手、转派、
联系家属、关闭全程留下不可变责任链。

## 设计要点

- **滚动计划**：风险等级决定频次（普通 7 天 / 关注 3 天 / 高风险 1 天），按村级人员
  日承载量均衡派单，本乡可跨村统筹、不跨乡；住院暂停、出院补排、节假日顺延到下一工作日。
- **并发接单唯一**：所有写事务 `BEGIN IMMEDIATE`，接单是单条带状态与归属条件的
  原子 `UPDATE`，并发抢单恰有一人成功（20 线程并发测试证明）。
- **离线幂等合并**：`OFF-XXXXXXXX-XXXXXXXX` 凭证全局唯一，重复上报原样返回首次结果，
  不产生第二条记录、不重复完成任务、不重复建风险；记录以 `occurred_at`（实际发生时间）
  落库，乱序送达仍按事实排序。发生时已撤权/人员离场/机构退出的记录以 `rejected_*`
  留存审计，但不完成计划、不升级。
- **授权立即生效**：撤回探访授权即刻取消未完成计划并阻止新记录；既有审计
  （`consent_audit`、已归档记录、责任链）只追加、不删除。
- **升级 SLA**：紧急 2 小时、关注 24 小时（乡镇），超时自动升县级；时限全部取自注入的
  `Clock`，生产用 `SystemClock`、测试用 `FakeClock`，跨午夜精确到秒；转派不重置截止时间。
- **可解释重分配**：服务员请假、机构退出、住院、撤权都会取消旧计划（状态 `cancelled`，
  `plan_events` 记录原因）并立即重排；监管导出中全程可追溯。
- **重启不丢**：计划、升级截止时间、责任链全部在 SQLite；重启后 `sweep_due()` 可补跑，
  正在计时的升级事项继续生效。
- **每日漏访扫描**：生成当日计划，把过期未完成计划标记漏访，并催办超时升级。
- **监管导出**：县级专用 CSV（UTF-8 BOM），含计划状态、合并状态、逐级升级轨迹与超时时间。

## 角色与 HTTP API

Bearer 令牌鉴权（`Authorization: Bearer <token>`）。

| 角色 | 可见范围 | 关键权限 |
|---|---|---|
| `county` 县级 | 全县 | 建档、人员/机构/节假日管理、授权变更、改派、风险全流程、每日扫描、监管导出 |
| `township` 乡镇 | 本乡 | 查看计划/风险、接手、转派、联系家属、关闭（限本人已接手）、改派 |
| `villager` 村级 | 本人/本乡 | 查看本人计划、接单、上报到访/未遇/拒访/风险发现 |

主要接口（均为 JSON，除导出外）：

- `POST /bootstrap`（仅空库时创建首个县级账号）、`GET /healthz`
- `POST /admin/agencies`、`POST /admin/agencies/{id}/deactivate`
- `POST /admin/workers`、`POST /admin/workers/{id}/deactivate`、`POST /admin/holidays`
- `POST /admin/daily-scan`、`GET /admin/export?start=YYYY-MM-DD&end=YYYY-MM-DD`（CSV）
- `POST /elders`、`POST /elders/{id}/risk-band`、`POST /elders/{id}/consent`、`POST /elders/{id}/residence`
- `GET /plans`、`POST /plans/generate`、`POST /plans/{id}/claim`、`POST /plans/{id}/reassign`
- `POST /visits`（离线记录，body 含唯一 `token`、`occurred_at`、`outcome`，可附 `plan_id`/`elder_id`）
- `GET /risks`、`GET /risks/{id}`（含完整责任链）、`POST /risks/{id}/take`、
  `/transfer`、`/contact-family`、`/close`

## 本地运行

初始化首个县级账号（令牌仅显示一次）：

```bash
python3 -m src.care_visits.http --db care.db --init-admin admin 县管理员
```

启动服务（含后台升级时限轮询与每日扫描）：

```bash
python3 -m src.care_visits.http --db care.db --host 0.0.0.0 --port 8080
```

执行测试与构建检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

测试覆盖：离线重复上报幂等、乱序按发生时间合并、20 线程并发接单唯一、FakeClock
跨午夜升级与重启后计时继续、授权撤回即时生效且审计保留、人员/机构/住院/节假日
重分配、每日漏访扫描、监管 CSV 导出、三级角色 401/403 隔离（真实 HTTP 端到端）。
