# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

监管抽查对账子系统把飞行计划、批准记录和空域限制接成抽查对账：运营方离线回传的回执按监管批次落到批准版本，重放只记一次；计划或限制后来变化则原批准失效；同一计划的冲突实际起降留两版等审核员裁决；两名审核员同时提交同一批次时先写入的一方生效，后到的人看到当前结论；写入失败只重试未处理条目，服务中断再启动也不会重复扣分或发通知。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/restrictions/{id}/change`：版本化变更限制（`expected_revision`）， bumped 版本会使相关批准失效。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

### 监管抽查对账

- `POST /api/recon/batches`：创建监管批次（审核员/指挥官）。
- `POST /api/recon/batches/{id}/receipts`：运营方回传离线回执，按 `offline_id` 去重、落到 `plan_revision` 对应的批准版本；批次已结束时回执立即处理。
- `POST /api/recon/batches/{id}/conclude`：按批次对账。先写入的一方生效，后到的一方读到当前结论；失败只重试 `processed=0` 的条目。
- `GET /api/recon/batches`、`GET /api/recon/batches/{id}`：批次与回执、结论明细。
- `GET /api/recon/desk`：协调台，按角色返回待复核、已核销和补证期限。
- `POST /api/recon/conclusions/{id}/adjudicate`：审核员对冲突的两版实际起降裁决（`accept`/`reject`/`need_more`）。

## 代码结构

判定、存储与接口入口分开维护：

- `app.py`：HTTP 接口入口与飞行计划/批准主服务。
- `recon/judge.py`：纯判定逻辑，依据回执、批准版本快照和限制版本给出结论。
- `recon/storage.py`：对账表结构与迁移。
- `recon/service.py`：编排存储与判定，负责批次并发、逐条重试、幂等扣分与通知。
- `recon/util.py`：时间与几何等底层工具。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
