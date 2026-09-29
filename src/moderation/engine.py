"""确定性复算引擎。

学生最终分数必须能够沿"每位评分员的每条原始评分 + 委员会采用的缺考口径 +
批次聚合规则"重新算出来。本模块不做任何数据库写入，只读取快照式输入并返回
带完整 trace 的结果，便于：
1. 封存前一致性检查；
2. 发布报告随附可复核的计算依据；
3. 事后任意时刻按同样输入复算核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class ScoreRecord:
    score_id: int
    student_id: str
    grader_id: str
    round: int
    dimension: str
    score: float
    is_late: bool
    rubric_id: str
    rubric_version: str


@dataclass
class StudentResult:
    student_id: str
    treatment: str  # present | zero | exclude
    grader_totals: dict[str, float] = field(default_factory=dict)
    final_score: float | None = None
    rank: int | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)


def adopt_grader_scores(
    student_id: str,
    scores: Iterable[ScoreRecord],
    rubric_dimensions: list[str],
) -> tuple[dict[str, dict[str, ScoreRecord]], list[dict[str, Any]]]:
    """采用规则一：同一 (评分员, 维度) 取**最大轮次**的记录，早先记录永不覆盖、
    只作为历史保留。返回 {grader_id: {dimension: record}} 与溯源步骤。"""
    latest: dict[str, dict[str, ScoreRecord]] = {}
    history: dict[tuple[str, str], list[ScoreRecord]] = {}
    for rec in scores:
        history.setdefault((rec.grader_id, rec.dimension), []).append(rec)
    trace: list[dict[str, Any]] = []
    for (grader_id, dimension), recs in history.items():
        recs.sort(key=lambda r: (r.round, r.score_id))
        winner = recs[-1]
        latest.setdefault(grader_id, {})[dimension] = winner
        trace.append(
            {
                "rule": "latest_round_wins",
                "student_id": student_id,
                "grader_id": grader_id,
                "dimension": dimension,
                "rounds": [r.round for r in recs],
                "adopted_score_id": winner.score_id,
                "adopted_round": winner.round,
                "adopted_value": winner.score,
                "late": bool(winner.is_late),
                "superseded": [
                    {"score_id": r.score_id, "round": r.round, "value": r.score}
                    for r in recs[:-1]
                ],
            }
        )
    return latest, trace


def compute_student(
    student_id: str,
    treatment: str,
    scores: list[ScoreRecord],
    rubric_dimensions: list[str],
    absent_reason: str | None = None,
) -> StudentResult:
    """按批次聚合规则计算单个学生。

    采用规则二：每个评分员的总分 = 各维度采用分之和（量表维度结构固定）；
    采用规则三：学生最终分 = 有效评分员总分的算术平均（回避者不出现在输入中）；
    缺考口径 zero → 计 0 分并参与排名；exclude → 不计分、不参与排名。
    """
    result = StudentResult(student_id=student_id, treatment=treatment)

    if treatment == "exclude":
        result.trace.append(
            {"rule": "absence_exclude", "student_id": student_id, "reason": absent_reason}
        )
        return result

    if treatment == "zero":
        result.final_score = 0.0
        result.trace.append(
            {"rule": "absence_zero", "student_id": student_id, "reason": absent_reason}
        )
        return result

    latest, trace = adopt_grader_scores(student_id, scores, rubric_dimensions)
    result.trace.extend(trace)
    totals: dict[str, float] = {}
    for grader_id, dims in latest.items():
        total = round(sum(dims[d].score for d in rubric_dimensions if d in dims), 6)
        totals[grader_id] = total
        result.trace.append(
            {
                "rule": "grader_total",
                "student_id": student_id,
                "grader_id": grader_id,
                "formula": " + ".join(
                    f"{d}={dims[d].score}" for d in rubric_dimensions if d in dims
                ),
                "total": total,
            }
        )
    result.grader_totals = totals
    if totals:
        result.final_score = round(sum(totals.values()) / len(totals), 6)
        result.trace.append(
            {
                "rule": "mean_of_graders",
                "student_id": student_id,
                "grader_count": len(totals),
                "formula": "(" + " + ".join(str(v) for v in totals.values()) + f") / {len(totals)}",
                "final_score": result.final_score,
            }
        )
    return result


def assign_ranks(results: list[StudentResult]) -> None:
    """标准竞争排名（1224）：仅对有最终分的学生排名，exclude 不参与。"""
    ranked = [r for r in results if r.final_score is not None]
    ranked.sort(key=lambda r: (-r.final_score, r.student_id))
    last_score: float | None = None
    last_rank = 0
    for idx, res in enumerate(ranked, start=1):
        if last_score is not None and res.final_score == last_score:
            res.rank = last_rank
        else:
            res.rank = idx
            last_rank = idx
        last_score = res.final_score
