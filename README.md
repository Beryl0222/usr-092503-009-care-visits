# 农村养老探访风险协调器

县级民政部门的服务端协调器:把乡镇站点、村级服务人员和社会机构连接起来,
为空巢、独居及留守老人提供持续探访。系统根据老人授权范围、风险等级、
居住变化和服务能力生成滚动探访计划;村级人员离线记录、恢复连接后按
发生顺序合并;高风险线索按时限升级并留下完整责任链。

## 模块

- `src/care_visits/contracts.py` — 探访结果、风险等级、离线凭证格式等稳定契约
- `src/care_visits/enums.py` — 角色、授权范围、任务与线索状态
- `src/care_visits/store.py` — SQLite 持久化(重启不丢状态)
- `src/care_visits/coordinator.py` — 协调器核心:计划、合并、升级、授权、重分配
- `src/care_visits/http_api.py` — 角色隔离的 HTTP API 与监管导出

## 关键设计

- **滚动计划**:按风险等级生成(高 7 天 / 中 15 天 / 低 30 天),覆盖未来 14 天;
  住院期间暂停,出院、迁居、机构退出后自动重建并给出可解释说明。
- **离线合并**:村级客户端生成唯一凭证 `OFF-XXXXXXXX-XXXXXXXX`;重复上报按
  凭证幂等,合并序号按实际发生时间排序,与到达顺序无关。
- **并发唯一**:接单与线索接手都用数据库条件更新,并发下只有一人成功。
- **升级时限**:urgent 120 分钟、concern 12 小时,时限持久化在库中,
  服务重启后继续计时;时钟可注入(`ManualClock`),测试可控。
- **授权即效**:老人撤回某类授权后立即限制后续访问;既有审计日志保留不删。
- **责任链**:线索的 上报/升级/接手/转派/联系家属/关闭 全部留痕可查。

## HTTP API(摘要)

认证:`Authorization: Bearer <token>`,令牌到人员的映射由部署方注入
`make_server(coordinator, tokens)`。

| 端点 | 角色 |
| --- | --- |
| `POST /api/elders`、`/api/workers`、`/api/agencies` | 县级 |
| `POST /api/elders/{id}/consents`、`/api/elders/{id}/status` | 县级 |
| `POST /api/workers/{id}/status`、`/api/agencies/{id}/withdraw` | 县级 |
| `GET /api/tasks`(村级仅见本站/本人) | 全部 |
| `POST /api/tasks/{id}/claim`、`POST /api/visits` | 村级 |
| `GET /api/alerts`、`/api/alerts/{id}/chain` | 乡镇/县级 |
| `POST /api/alerts/{id}/ack|reassign|contact-family|close` | 乡镇/县级 |
| `POST /api/escalations/check` | 县级(另可挂后台定时器) |
| `GET /api/scan/omissions`、`GET /api/export/audit.csv` | 县级 |

## 本地运行

执行测试:

```bash
python3 -m unittest discover -s tests -v
```

执行构建检查:

```bash
python3 -m compileall -q src
```
