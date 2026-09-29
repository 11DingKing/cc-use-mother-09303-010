"""跨国联合考核 moderation 服务端。

模块划分：

- ``store``：SQLite 存储。评分相关数据（评分、回避、缺考声明、迟到成绩、
  复核决议、检查记录、会签、封存快照）全部为只追加事件，批次表仅保存流程状态投影。
- ``engine``：领域逻辑。批次状态机、封存前一致性检查、缺考口径、
  成绩归约与沿评分员/规则的确定性复算、会签与发布。
- ``server``：基于标准库 http.server 的 REST 接口。
"""
from .engine import ModerationService
from .store import Store

__all__ = ["ModerationService", "Store"]
