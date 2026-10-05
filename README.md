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
- `POST /api/items`（可选 `occupant_density`）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/batches`：震后复评批次，同批串起工单修订、离线测量与加固方案
- `GET /api/batches/{batch_no}`
- `GET /api/items/{id}/components[?status=effective|pending_review|superseded]`
- `POST /api/component-versions/{id}/promote`：待复核版本人工提为生效
- `GET /api/items/{id}/schemes`
- `GET /api/items/{id}/conclusions`
- `POST /api/items/{id}/review-decision`：评审委员会对审核结论 `active`/`rejected`
- `POST /api/items/{id}/backfill`：旧工单缺字段补全后照常读回并重算
- `GET /api/audit`

允许角色：assessor, structural_engineer, review_board, viewer。风险分值和人员密度共同影响排序；审核通过前必须完成评估、设计和施工证据登记。

## 震后复评批次语义

- **同一批次**：工单版本号、构件测量版本、加固方案版本在一个数据库事务内提交，整批成功或整批回滚，不存在半批覆盖。
- **并发去重**：同一构件已有生效材料版本时，后到版本不覆盖，自动转 `pending_review`（生效位由部分唯一索引保证只有一个），经工程师或评审委员会 `promote` 后才生效。
- **失效重算**：生效测量或人员密度改变输入指纹后，旧优先级结论与审核结论置 `invalidated`，优先级重算，审核回到 `pending`；仅加固方案换版时只失效审核结论。
- **批次幂等**：`batch_no` 加内容哈希。同号同内容重传只入库一次（返回 `duplicate: true`）；同号不同内容返回 409。
- **失败重试**：写入失败（含工单乐观锁冲突）整批回滚、原工单保留，失败尝试记入 `batch_attempts`，同一批次号修正后可重试。
- **旧工单兼容**：新字段（`occupant_density`、`current_batch_no`）可空，读回时按默认值兜底，`backfill` 持久化补全并重算。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
