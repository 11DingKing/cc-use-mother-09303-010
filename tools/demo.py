"""端到端场景演示：联合考核首次汇总时的缺考口径分歧如何被治理。

直接运行：python3 tools/demo.py
使用内存数据库，按时间线打印每个关键动作与最终发布结果。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moderation import ModerationService, Store  # noqa: E402
from moderation.errors import ConflictError  # noqa: E402

DIMENSIONS = [
    {"key": "content", "title": "内容", "min": 0, "max": 10},
    {"key": "delivery", "title": "表达", "min": 0, "max": 10},
]


def line(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_report(report: dict) -> None:
    for s in report["students"]:
        if s["final_score"] is None:
            print(f"  {s['student_id']}: 口径={s['treatment']} → 排除，不参与排名")
        else:
            print(f"  {s['student_id']}: 总分={s['final_score']:g} 排名={s['rank']} "
                  f"各评分员合计={s['grader_totals']}")


def main() -> None:
    svc = ModerationService(Store(":memory:"))

    line("1. 建档：两方联考委、两名学生、量表 v1、持证评分员、一个批次")
    svc.register_party("party-a", "甲方大学", "协调员")
    svc.register_party("party-b", "乙方大学", "协调员")
    svc.register_student("s1", "张清", "协调员")
    svc.register_student("s2", "李微", "协调员")
    svc.create_rubric_version("rubric-x", "v1", "联合答辩量表", DIMENSIONS, "party-a", "协调员")
    svc.register_grader("g1", "party-a", "甲校王老师", "协调员")
    svc.register_grader("g2", "party-b", "乙校李老师", "协调员")
    svc.set_qualification("g1", "rubric-x", "v1", True, "协调员")
    svc.set_qualification("g2", "rubric-x", "v1", True, "协调员")
    # 约定：缺考默认排除、最少 2 名评分员、发布需 2 方签署
    svc.create_batch("b1", "2026 春季联合答辩", "rubric-x", "v1", "exclude", 2, 2,
                     "协调员", student_ids=["s1", "s2"])
    print("  批次 b1 已创建（rubric-x@v1，需 2 方会签）")

    line("2. 评分：s1 两位评分员均打分；s2 缺考，两方口径不同")
    for args in [("s1", "g1", "content", 9), ("s1", "g1", "delivery", 9),
                 ("s1", "g2", "content", 8), ("s1", "g2", "delivery", 8)]:
        svc.record_score("b1", *args, actor="评分员")
    svc.mark_absence("b1", "s2", "party-a", "zero", "甲方教务")
    svc.mark_absence("b1", "s2", "party-b", "exclude", "乙方教务")
    print("  甲方：s2 缺考记零分；乙方：s2 缺考排除")

    line("3. 封存前一致性检查 → 发现 D1 缺考口径分歧，进入讨论")
    report = svc.run_consistency_check("b1", "协调员")
    for d in report["discrepancies"]:
        print(f"  [{d['code']}] 学生 {d['student_id']} 明细={d['detail']}")
    sample = svc.list_review_samples("b1")[0]
    print(f"  → 生成复核样本 #{sample['sample_id']}，尝试封存：")
    try:
        svc.seal_batch("b1", "协调员")
    except ConflictError as exc:
        print(f"    封存被拒：{exc}")

    line("4. 双方在样本下讨论，委员会作出采用决定并决议样本")
    svc.discuss(sample["sample_id"], "party-a", "考场记录显示该生到场后弃考，应计零分", "甲代表")
    svc.discuss(sample["sample_id"], "party-b", "我方录入有误，认可计零分", "乙代表")
    svc.resolve_absence("b1", "s2", "zero", "经讨论：到场弃考按缺考计零分", "委员会")
    svc.resolve_sample(sample["sample_id"], "采用 zero 口径", "委员会")
    svc.change_absence_policy("b1", "zero", "委员会", "本年度统一缺考计零")
    print("  委员会决定：zero；批次口径变更已追加留痕")

    line("5. 复评示例：g2 对 s1 content 复评 8.5 → 进入第 2 轮，旧分保留")
    sid = svc.record_score("b1", "s1", "g2", "content", 8.5, "g2")
    print(f"  新成绩 #{sid}（round=2，round=1 的 8 分仍在库中）")

    line("6. 检查通过、封存、两方会签、发布")
    print("  检查：", svc.run_consistency_check("b1", "协调员"))
    svc.seal_batch("b1", "协调员")
    print("  只签署 1 方时尝试发布：")
    svc.sign("b1", "party-a", "甲方院长")
    try:
        svc.publish("b1", "协调员")
    except ConflictError as exc:
        print(f"    被拒：{exc}")
    svc.sign("b1", "party-b", "乙方院长")
    out = svc.publish("b1", "协调员")
    print("  两方签署完成，已发布：")
    show_report(out["report"])

    line("7. 可复算性：s1 最终 17.25 的溯源链（每位评分员 + 采用规则）")
    s1 = next(s for s in out["report"]["students"] if s["student_id"] == "s1")
    for step in s1["trace"]:
        print("  " + json.dumps(step, ensure_ascii=False))

    line("8. 审计：关键事件流（只追加，不可改写）")
    for ev in svc.store.list_events("b1"):
        if ev["event_type"] in (
            "batch.created", "consistency.checked", "batch.sealed",
            "batch.published", "batch.policy_changed", "score.recorded",
        ):
            print(f"  #{ev['seq']:>3} {ev['event_type']:<22} by {ev['actor']}")


if __name__ == "__main__":
    main()
