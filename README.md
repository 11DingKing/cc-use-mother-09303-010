# 跨国联合考核 moderation

本项目维护跨国联合考核 moderation 的领域约定、角色边界与样例数据，并在此基础上提供
完整服务端：管理考核批次、量表版本、评分员资格、原始评分、缺考口径与复核样本，
封存前运行一致性检查并把差异送入讨论，达到约定签署数后才可发布，结果可沿每位评分员
与采用规则确定性复算。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/moderation_service/`：moderation 服务端
  - `store.py`：SQLite 存储。评分、回避、缺考声明/撤回、迟到成绩、复评分、
    复核决议、封存快照、会签均为**只追加**记录。
  - `engine.py`：批次状态机（评分→校准→复核→会签→发布）、封存前一致性检查、
    缺考口径（零分/排除）、事件归约与成绩复算、法定签署数控制。
  - `server.py`：标准库 `http.server` 实现的 REST 接口（零第三方依赖）。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tools/run_server.py`：启动服务端。
- `tests/`：契约回归与服务端领域规则测试（含端到端 HTTP 冒烟）。

## 关键领域规则

- **量表版本**：评分与签署都校验评分员在相应时点持有该量表版本的有效资格；
  资格撤销按时间点生效，历史评分不被改写。
- **缺考口径**：批次按参与方约定口径（`zero` 记零分 / `exclude` 排除）。
  口径分歧、一方缺考另一方有成绩等情形在封存前检出并送入讨论。
- **封存前一致性检查**：漏报（无成绩也无缺考声明）、同方既缺考又有成绩为阻断项；
  分差超阈值、口径分歧、迟到成绩、回避为讨论项。封存生成不可变快照（含校验和）。
- **只追加，不覆盖**：复评分、回避、迟到成绩、缺考撤回、批次重开都以新事件表达；
  迟到成绩不覆盖评分员早先成绩；批次重开开启新窗口，历史封存快照保持不变。
- **复核**：差异项在讨论中结案（协调员确认或采纳复评分），支持登记复核样本。
- **法定发布**：会签阶段按约定规则（`both` 双方均签 / `any`）与签署数校验，
  未达法定签署数不能发布。发布时冻结规则、结果（`as_of_seq`）与校验和。
- **可复算**：`GET /batches/{id}/students/{sid}/trace` 给出每位评分员的完整
  事件历史、有效值与采用规则；`POST /batches/{id}/verify` 沿事件流重算并比对
  发布校验和。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/parties` `/rubrics` `/raters` `/students` `/batches` | 主数据与批次创建 |
| POST | `/raters/{id}/qualifications`、`.../revoke` | 授予/撤销量表资格 |
| POST | `/batches/{id}/students` | 批次加入学生 |
| POST | `/batches/{id}/scores` | 提交评分（`accept_late` 显式确认迟到） |
| POST | `/batches/{id}/absences`、`/absence-retractions` | 缺考登记/撤回 |
| POST | `/batches/{id}/recusals` | 评分员回避 |
| POST | `/batches/{id}/checks`、`/seal` | 一致性检查与封存 |
| POST | `/batches/{id}/reopen`、`/review`、`/countersign` | 重开/流转状态 |
| POST | `/discussions/{id}/comments`、`/acknowledge` | 讨论与结案 |
| POST | `/batches/{id}/review-samples`、`/rescores` | 复核样本与复评分 |
| POST | `/batches/{id}/signatures`、`/publish`、`/verify` | 会签、发布、复算校验 |
| GET | `/batches/{id}`、`/events`、`/discussions`、`/seals`、`/results`、`/publication` | 查询 |
| GET | `/batches/{id}/students/{sid}/trace` | 评分员级可复算追溯 |

## 运行

```bash
python3 tools/run_server.py --db moderation.db --port 8080
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
