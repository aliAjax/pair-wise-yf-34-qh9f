# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

在此之上叠加了**监管抽查对账**：把飞行计划、批准记录和空域限制接成对账链路，核对实际飞行与批准结论。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区；与已批准计划重叠时原批准自动失效。
- `POST /api/restrictions/{id}/change`：调整限制范围、高度、时间或解除（`lifted`）；仍生效的限制会级联失效重叠批准。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 抽查对账（监管批次）

- `POST /api/recon/batches`：审核员创建监管批次，按**批准版本**快照每个计划的窗口、呼号和运营方（`receipt_deadline`、`evidence_hours` 可覆盖默认值）。
- `POST /api/recon/batches/{id}/receipts`：运营方离线回传回执。`entries` 逐条独立事务落库，写入失败后只重试没有处理的条目；`offline_id` 幂等，**重放只记一次**；回执可携带 `plan_revision`，即使对应改过的计划版本，也会落到批次锁定的批准版本上并标记 `revision_mismatch`。
- 实际起降与批准窗口一致（容差 5 分钟）自动核销；偏差进入待复核；同一计划出现**冲突的实际起降**时两版都保留，条目转为待裁决。
- `POST /api/recon/batches/{id}/adjudicate`：审核员提交裁决单（`sheet_offline_id` 幂等）。两人同时提交同一批次时**先写入的一方生效**，后到的人在响应里看到当前结论。违规结论按发现类型扣分并通知。
- 计划变更/取消或空域限制新增、变化会让原批准失效：条目转入补证状态并给出**补证期限**，运营方补证（`kind: "supplement"`）后回到待复核。
- `POST /api/recon/sweep`：逾期处理，未回执记 `no_show`、补证超期发提醒。扣分台账和通知都带去重键，重试、重放、**服务重启都不会重复扣分或发通知**。
- `GET /api/recon/console`：协调台，按角色展示待复核、已核销、补证期限和待回执；运营方只见自己的条目。
- `GET /api/recon/batches`、`/api/recon/batches/{id}`、`/api/recon/items/{id}`：批次与条目明细（含两版冲突回执与扣分记录）。

### 分层

判定（`recon/adjudication.py`，纯函数）、存储（`recon/store.py`，全部 SQL 与幂等键）、编排（`recon/service.py`）与接口入口（`recon/api.py`）分开维护；`app.py` 只负责计划/限制主流程，并在变更时调用对账级联钩子。共享的时间与几何工具在 `common.py`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite（单连接加写锁串行化写事务）适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
