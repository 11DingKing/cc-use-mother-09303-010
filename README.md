# 跨国联合考核 moderation

本项目维护跨国联合考核 moderation 的领域约定、角色边界与完整服务端，供后端服务、接口和自动化验证统一使用。契约覆盖中外教师、考核协调员、学生，并明确**量表版本、评分员资格、差异复核、法定发布**四项关键约束。

## 服务端能力

服务端围绕批次（batch）组织全部考核数据，实现题目要求的完整闭环：

- **批次管理**：创建批次并锁定量表版本、缺考默认口径（`zero` 计零 / `exclude` 排除）、约定签署数、最少评分员数；支持在册学生、截止时间。
- **量表版本**：量表按 `rubric_id@version` 版本化，维度与分值区间固化；批次与每条评分都绑定版本，错配成绩无法录入。
- **评分员资格**：资格按量表版本授予/撤销（追加事件，当前状态取最新一条）；无有效资格、已回避的评分员无法录成绩。
- **原始评分（只追加）**：复评自动进入下一**轮次**，迟到成绩标记 `is_late`；SQLite 触发器在数据库层拒绝 UPDATE/DELETE，早先记录永不覆盖。
- **缺考口径治理**：各方独立上报 `present/zero/exclude`；封存前检查自动发现口径分歧（D1）与未决议缺考（D2），生成复核样本进入讨论；委员会采用决定同样只追加。
- **一致性检查与复核样本**：封存前强制执行 7 类检查（缺考分歧、缺考未决议、评分员不足、维度缺失、评分分差过大、版本错配、回避与成绩并存），差异全部决议后方可封存；每个样本带多方讨论线程。
- **回避**：回避登记只追加且不可撤销，回避者名下任何历史成绩都不进入复算。
- **封存 / 重开**：封存后到达的迟到成绩自动重开批次（`reopen_count+1`），早先封存、检查、签署记录全部保留但失效，须重新检查与会签；已发布批次不可重开、不可改写。
- **会签与法定发布**：签署/撤回为追加事件，重开后旧签署自动失效；只有处于封存状态、无未决议样本、且**有效签署方数达到批次约定数**时才能发布。
- **可复算**：`GET /batches/{id}/recompute` 与发布报告附完整溯源链（`trace`），沿每位评分员各轮采用分 → 评分员合计 → 评分员均值逐步复算，缺考口径标注采用依据。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/moderation/`：服务端实现（零第三方依赖，仅标准库 + SQLite）。
  - `store.py`：SQLite 存储层，核心表只追加，触发器拒绝改写。
  - `engine.py`：确定性复算引擎（轮次采用、评分员合计、均值、排名）。
  - `service.py`：领域服务，状态机与全部业务规则。
  - `api.py` / `server.py`：HTTP API（`http.server`）与启动入口。
- `tools/check_contract.py`：契约摘要检查。
- `tools/demo.py`：端到端场景演示（缺考口径分歧 → 讨论 → 决议 → 会签发布）。
- `tests/`：契约与服务端回归测试（13 个用例）。

## 运行

启动 HTTP 服务（默认内存库；`--db` 指定持久化路径）：

```bash
PYTHONPATH=src python3 -m moderation.server --db data/moderation.db --port 8080
```

端到端演示：

```bash
python3 tools/demo.py
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

## HTTP API 摘要

| 动作 | 方法与路径 |
|---|---|
| 联考方 / 学生 / 量表 / 评分员 | `POST /parties` `/students` `/rubrics` `/graders` |
| 授予或撤销资格 | `POST /graders/qualifications` |
| 创建批次 / 加学生 / 改口径 | `POST /batches/{id}` `/batches/{id}/students` `/batches/{id}/policy` |
| 评分（复评自动新一轮）/ 回避 | `POST /batches/{id}/scores` `/batches/{id}/recusals` |
| 缺考上报 / 委员会采用决定 | `POST /batches/{id}/absence-marks` `/batches/{id}/absence-decisions` |
| 一致性检查 / 样本列表 | `POST /batches/{id}/checks` `GET /batches/{id}/samples` |
| 讨论 / 决议样本 | `POST /samples/{sid}/discussions` `/samples/{sid}/resolve` |
| 封存 / 重开 | `POST /batches/{id}/seal` `/batches/{id}/reopen` |
| 签署 / 撤签 | `POST /batches/{id}/signatures` `/batches/{id}/unsign` |
| 发布 / 已发布报告 / 实时复算 | `POST /batches/{id}/publish` `GET /batches/{id}/report` `/recompute` |
| 批次状态 / 审计事件流 | `GET /batches/{id}/status` `GET /events?batch_id=...` |

操作者通过请求体 `actor` 字段或 `X-Actor` 头传递；错误返回 `400/404/409` 与中文错误信息。
