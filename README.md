# 建筑抗震鉴定与加固排序

依据结构、用途、人员密度和历史缺陷生成鉴定与加固优先级。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8317
```

默认端口为`8317`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `PATCH /api/items/{id}`：补全工单缺失字段（如人员密度），补全后照常读回
- `GET /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/batches`：批次入库，把工单、测量批次和加固方案串成同一批次
- `GET /api/items/{id}/batches`
- `GET /api/items/{id}/conclusion`
- `GET /api/batches/{id}`
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 批次入库规则

- **批次号幂等**：同一`batch_no`重传只入库一次，直接回读已提交批次，不重复写入。
- **一生效一待复核**：同一构件同一材料版本只保留一个生效版本。并发提交时，首个提交为`effective`，后到的因部分唯一索引冲突自动转`pending_review`（待复核），不覆盖先到的有效版本。
- **失效重算**：测量或人员密度变化后，旧结论置为失效（`valid=0`），并按生效测量的控制量与新密度重算优先级、期限和是否需要升级审核，生成新的生效结论。
- **失败重试**：批次写入在单个事务内完成；写入失败时原批次保留为`failed`，可用同一批次号重试，重试成功后转`committed`。
- **旧库迁移**：启动时自动为旧库补`density`等缺失字段并建批次相关表，旧工单无需手工迁移即可读回。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
