"""领域服务层：所有业务规则与状态流转的唯一入口。

状态机（对应领域契约 states）：
    评分(open) ──封存──▶ 校准/复核(sealed) ──达到签署数──▶ 会签通过 ──▶ 发布(published)
                  ▲ 重开（仅 published 前，且全程留痕、早先记录保留）

关键不变量（对应契约 invariants）：
- 量表版本：批次锁定 rubric 版本；评分必须携带该版本；
- 评分员资格：评分员须持有该 rubric 版本的有效资格，且未对该生回避；
- 差异复核：封存前强制一致性检查，差异必须全部解决（或经决议留痕）；
- 法定发布：当前有效签署方数达到批次约定数方可发布。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .engine import ScoreRecord, assign_ranks, compute_student
from .errors import ConflictError, NotFoundError, ValidationError
from .store import Store

PRESENT, ZERO, EXCLUDE = "present", "zero", "exclude"
TREATMENTS = (PRESENT, ZERO, EXCLUDE)


class ModerationService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- 内部工具 -------------------------------------------------------------

    @staticmethod
    def _require(value: Any, message: str) -> Any:
        if value is None or value == "":
            raise ValidationError(message)
        return value

    def _batch_or_404(self, batch_id: str):
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return batch

    def _event(self, event_type: str, payload: dict, actor: str) -> int:
        return self.store.append_event(event_type, payload, actor)

    # -- 基础数据 -------------------------------------------------------------

    def register_party(self, party_id: str, name: str, actor: str = "system") -> None:
        self._require(party_id, "party_id 不能为空")
        if self.store.get_party(party_id) is not None:
            raise ConflictError(f"联考方已存在：{party_id}")
        seq = self._event("party.registered", {"party_id": party_id, "name": name}, actor)
        self.store.conn.execute(
            "INSERT INTO parties (party_id, name, created_seq) VALUES (?, ?, ?)",
            (party_id, name, seq),
        )
        self.store.commit()

    def register_student(self, student_id: str, name: str, actor: str = "system") -> None:
        self._require(student_id, "student_id 不能为空")
        if self.store.get_student(student_id) is not None:
            raise ConflictError(f"学生已存在：{student_id}")
        seq = self._event("student.registered", {"student_id": student_id, "name": name}, actor)
        self.store.conn.execute(
            "INSERT INTO students (student_id, name, created_seq) VALUES (?, ?, ?)",
            (student_id, name, seq),
        )
        self.store.commit()

    # -- 量表版本 -------------------------------------------------------------

    def create_rubric_version(
        self,
        rubric_id: str,
        version: str,
        title: str,
        dimensions: list[dict],
        party_id: str,
        actor: str,
    ) -> None:
        self._require(rubric_id, "rubric_id 不能为空")
        self._require(version, "version 不能为空")
        if not dimensions or not all(d.get("key") for d in dimensions):
            raise ValidationError("量表至少包含一个带 key 的维度")
        keys = [d["key"] for d in dimensions]
        if len(keys) != len(set(keys)):
            raise ValidationError("量表维度 key 不能重复")
        for d in dimensions:
            lo, hi = d.get("min"), d.get("max")
            if lo is None or hi is None or float(lo) >= float(hi):
                raise ValidationError(f"维度 {d['key']} 的分值区间非法")
        if self.store.get_party(party_id) is None:
            raise NotFoundError(f"联考方不存在：{party_id}")
        if self.store.get_rubric(rubric_id, version) is not None:
            raise ConflictError(f"量表版本已存在：{rubric_id}@{version}")
        seq = self._event(
            "rubric.version_created",
            {"rubric_id": rubric_id, "version": version, "title": title, "dimensions": dimensions},
            actor,
        )
        self.store.conn.execute(
            "INSERT INTO rubric_versions (rubric_id, version, title, dimensions_json, "
            "created_party, created_seq) VALUES (?, ?, ?, ?, ?, ?)",
            (rubric_id, version, title, json.dumps(dimensions, ensure_ascii=False), party_id, seq),
        )
        self.store.commit()

    # -- 评分员资格 -----------------------------------------------------------

    def register_grader(self, grader_id: str, party_id: str, name: str, actor: str = "system") -> None:
        if self.store.get_party(party_id) is None:
            raise NotFoundError(f"联考方不存在：{party_id}")
        if self.store.get_grader(grader_id) is not None:
            raise ConflictError(f"评分员已存在：{grader_id}")
        seq = self._event(
            "grader.registered", {"grader_id": grader_id, "party_id": party_id, "name": name}, actor
        )
        self.store.conn.execute(
            "INSERT INTO graders (grader_id, party_id, name, created_seq) VALUES (?, ?, ?, ?)",
            (grader_id, party_id, name, seq),
        )
        self.store.commit()

    def set_qualification(
        self,
        grader_id: str,
        rubric_id: str,
        version: str,
        qualified: bool,
        actor: str,
    ) -> None:
        """授予/撤销资格都是追加事件，当前状态取最新一条。"""
        if self.store.get_grader(grader_id) is None:
            raise NotFoundError(f"评分员不存在：{grader_id}")
        if self.store.get_rubric(rubric_id, version) is None:
            raise NotFoundError(f"量表版本不存在：{rubric_id}@{version}")
        action = "granted" if qualified else "revoked"
        seq = self._event(
            "grader.qualification_changed",
            {"grader_id": grader_id, "rubric_id": rubric_id, "version": version, "action": action},
            actor,
        )
        self.store.conn.execute(
            "INSERT INTO grader_qualifications (grader_id, rubric_id, version, action, "
            "actor, recorded_seq) VALUES (?, ?, ?, ?, ?, ?)",
            (grader_id, rubric_id, version, action, actor, seq),
        )
        self.store.commit()

    # -- 批次 -----------------------------------------------------------------

    def create_batch(
        self,
        batch_id: str,
        title: str,
        rubric_id: str,
        rubric_version: str,
        absence_policy: str,
        required_signatures: int,
        min_graders: int,
        actor: str,
        student_ids: list[str] | None = None,
        deadline: str | None = None,
    ) -> None:
        if self.store.get_batch(batch_id) is not None:
            raise ConflictError(f"批次已存在：{batch_id}")
        if self.store.get_rubric(rubric_id, rubric_version) is None:
            raise NotFoundError(f"量表版本不存在：{rubric_id}@{rubric_version}")
        if absence_policy not in ("zero", "exclude"):
            raise ValidationError("缺考口径必须是 zero 或 exclude")
        if required_signatures < 1:
            raise ValidationError("约定签署数至少为 1")
        if min_graders < 1:
            raise ValidationError("最少评分员数至少为 1")
        seq = self._event(
            "batch.created",
            {
                "batch_id": batch_id,
                "title": title,
                "rubric_id": rubric_id,
                "rubric_version": rubric_version,
                "absence_policy": absence_policy,
                "required_signatures": required_signatures,
                "min_graders": min_graders,
                "deadline": deadline,
            },
            actor,
        )
        self.store.conn.execute(
            "INSERT INTO batches (batch_id, title, rubric_id, rubric_version, absence_policy, "
            "required_signatures, min_graders, deadline, status, created_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (
                batch_id, title, rubric_id, rubric_version, absence_policy,
                required_signatures, min_graders, deadline, seq,
            ),
        )
        for sid in student_ids or []:
            self._enroll(batch_id, sid, actor, seq)
        self.store.commit()

    def _enroll(self, batch_id: str, student_id: str, actor: str, created_seq: int | None = None) -> None:
        if self.store.get_student(student_id) is None:
            raise NotFoundError(f"学生不存在：{student_id}")
        if self.store.is_enrolled(batch_id, student_id):
            raise ConflictError(f"学生已在批次中：{student_id}")
        seq = created_seq or self._event(
            "batch.student_enrolled", {"batch_id": batch_id, "student_id": student_id}, actor
        )
        self.store.conn.execute(
            "INSERT INTO enrollments (batch_id, student_id, recorded_seq) VALUES (?, ?, ?)",
            (batch_id, student_id, seq),
        )

    def enroll_student(self, batch_id: str, student_id: str, actor: str) -> None:
        batch = self._batch_or_404(batch_id)
        if batch["status"] != "open":
            raise ConflictError("批次已封存，不能新增学生；如需调整请先重开")
        self._enroll(batch_id, student_id, actor)
        self.store.commit()

    def change_absence_policy(self, batch_id: str, policy: str, actor: str, reason: str) -> None:
        """调整批次默认缺考口径（zero/exclude）：追加留痕，早先口径不被抹掉。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布，口径不可再改")
        if policy not in ("zero", "exclude"):
            raise ValidationError("缺考口径必须是 zero 或 exclude")
        seq = self._event(
            "batch.policy_changed",
            {"batch_id": batch_id, "absence_policy": policy, "reason": reason},
            actor,
        )
        self.store.conn.execute(
            "INSERT INTO batch_policy_changes (batch_id, absence_policy, actor, reason, recorded_seq) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, policy, actor, reason, seq),
        )
        self.store.conn.execute(
            "UPDATE batches SET absence_policy = ? WHERE batch_id = ?", (policy, batch_id)
        )
        self.store.commit()

    # -- 评分 / 回避 / 迟到 ---------------------------------------------------

    def _require_open_for_scoring(self, batch) -> None:
        if batch["status"] == "published":
            raise ConflictError("批次已发布，评分通道关闭")

    def record_score(
        self,
        batch_id: str,
        student_id: str,
        grader_id: str,
        dimension: str,
        score: float,
        actor: str,
        is_late: bool = False,
    ) -> int:
        """录入一条原始评分。

        - 必须批次开放、学生在册；
        - 评分员须持批次量表版本的有效资格；
        - 存在回避记录则禁止评分；
        - 分值必须落在该维度区间内；
        - 同一(评分员, 维度)再次录入自动进入下一**轮次**，永不覆盖早先记录；
        - 封存后仍可录入（迟到成绩，is_late=1），但会触发批次需重新检查/会签重置。
        """
        batch = self._batch_or_404(batch_id)
        self._require_open_for_scoring(batch)
        if not self.store.is_enrolled(batch_id, student_id):
            raise NotFoundError(f"学生不在批次中：{student_id}")
        grader = self.store.get_grader(grader_id)
        if grader is None:
            raise NotFoundError(f"评分员不存在：{grader_id}")
        if not self.store.grader_is_qualified(grader_id, batch["rubric_id"], batch["rubric_version"]):
            raise ConflictError(
                f"评分员 {grader_id} 不具备量表 {batch['rubric_id']}@{batch['rubric_version']} 的有效资格"
            )
        if self.store.has_recusal(batch_id, student_id, grader_id):
            raise ConflictError(f"评分员 {grader_id} 已回避该学生，不能录入成绩")
        rubric = self.store.get_rubric(batch["rubric_id"], batch["rubric_version"])
        dims = {d["key"]: d for d in json.loads(rubric["dimensions_json"])}
        if dimension not in dims:
            raise ValidationError(f"量表 {rubric['rubric_id']} 无维度：{dimension}")
        score = float(score)
        if not (float(dims[dimension]["min"]) <= score <= float(dims[dimension]["max"])):
            raise ValidationError(
                f"维度 {dimension} 分值 {score} 超出区间 "
                f"[{dims[dimension]['min']}, {dims[dimension]['max']}]"
            )

        was_sealed = batch["status"] == "sealed"
        nxt = self.store.latest_round(batch_id, student_id, grader_id, dimension) + 1
        payload = {
            "batch_id": batch_id,
            "student_id": student_id,
            "grader_id": grader_id,
            "round": nxt,
            "dimension": dimension,
            "score": score,
            "is_late": bool(is_late or was_sealed),
        }
        try:
            seq = self._event("score.recorded", payload, actor)
            cur = self.store.conn.execute(
                "INSERT INTO scores (batch_id, student_id, grader_id, round, dimension, score, "
                "rubric_id, rubric_version, is_late, actor, recorded_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id, student_id, grader_id, nxt, dimension, score,
                    batch["rubric_id"], batch["rubric_version"],
                    1 if (is_late or was_sealed) else 0, actor, seq,
                ),
            )
            score_id = int(cur.lastrowid)
            if was_sealed:
                # 封存后到达的迟到成绩：早先封存记录保留，批次回到 open，
                # 一致性检查结论与签署全部失效，必须重新走流程。
                self.store.conn.execute(
                    "UPDATE batches SET status = 'open', reopen_count = reopen_count + 1 "
                    "WHERE batch_id = ?",
                    (batch_id,),
                )
                self._event(
                    "batch.reopened",
                    {"batch_id": batch_id, "reason": "late_score", "score_id": score_id},
                    actor,
                )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return score_id

    def record_recusal(
        self, batch_id: str, student_id: str, grader_id: str, reason: str, actor: str
    ) -> None:
        """登记回避：只追加。已回避则报冲突；回避后该评分员的成绩一律拒收。"""
        batch = self._batch_or_404(batch_id)
        self._require_open_for_scoring(batch)
        if not self.store.is_enrolled(batch_id, student_id):
            raise NotFoundError(f"学生不在批次中：{student_id}")
        if self.store.get_grader(grader_id) is None:
            raise NotFoundError(f"评分员不存在：{grader_id}")
        if self.store.has_recusal(batch_id, student_id, grader_id):
            raise ConflictError("该回避已登记，回避记录不可撤销")
        try:
            seq = self._event(
                "grader.recused",
                {
                    "batch_id": batch_id,
                    "student_id": student_id,
                    "grader_id": grader_id,
                    "reason": reason,
                },
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO recusals (batch_id, student_id, grader_id, reason, actor, recorded_seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, student_id, grader_id, reason, actor, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    # -- 缺考口径 -------------------------------------------------------------

    def mark_absence(
        self,
        batch_id: str,
        student_id: str,
        party_id: str,
        treatment: str,
        actor: str,
    ) -> None:
        """某联考方上报对某学生的缺考口径。可反复更正，每次追加，分歧由检查发现。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布，不能再上报缺考口径")
        if not self.store.is_enrolled(batch_id, student_id):
            raise NotFoundError(f"学生不在批次中：{student_id}")
        if self.store.get_party(party_id) is None:
            raise NotFoundError(f"联考方不存在：{party_id}")
        if treatment not in TREATMENTS:
            raise ValidationError("treatment 必须是 present/zero/exclude")
        try:
            seq = self._event(
                "absence.marked",
                {
                    "batch_id": batch_id,
                    "student_id": student_id,
                    "party_id": party_id,
                    "treatment": treatment,
                },
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO absence_marks (batch_id, student_id, party_id, treatment, actor, recorded_seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, student_id, party_id, treatment, actor, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    def resolve_absence(
        self,
        batch_id: str,
        student_id: str,
        treatment: str,
        reason: str,
        actor: str,
    ) -> None:
        """委员会经讨论后对缺考口径作出采用决定（追加，当前值取最新一条）。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布")
        if not self.store.is_enrolled(batch_id, student_id):
            raise NotFoundError(f"学生不在批次中：{student_id}")
        if treatment not in TREATMENTS:
            raise ValidationError("treatment 必须是 present/zero/exclude")
        try:
            seq = self._event(
                "absence.resolved",
                {
                    "batch_id": batch_id,
                    "student_id": student_id,
                    "treatment": treatment,
                    "reason": reason,
                },
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO enrollment_decisions (batch_id, student_id, treatment, reason, "
                "actor, recorded_seq) VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, student_id, treatment, reason, actor, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    # -- 一致性检查与复核样本 -------------------------------------------------

    def _score_records(self, batch_id: str) -> list[ScoreRecord]:
        return [
            ScoreRecord(
                score_id=r["id"],
                student_id=r["student_id"],
                grader_id=r["grader_id"],
                round=r["round"],
                dimension=r["dimension"],
                score=r["score"],
                is_late=bool(r["is_late"]),
                rubric_id=r["rubric_id"],
                rubric_version=r["rubric_version"],
            )
            for r in self.store.list_scores(batch_id)
        ]

    def _rubric_dims(self, batch) -> list[str]:
        rubric = self.store.get_rubric(batch["rubric_id"], batch["rubric_version"])
        return [d["key"] for d in json.loads(rubric["dimensions_json"])]

    def run_consistency_check(self, batch_id: str, actor: str) -> dict:
        """封存前的一致性检查。发现的问题全部生成复核样本并写入检查报告。

        检查项：
        D1 缺考口径分歧：各方对同一学生的最新标记不一致，且尚无委员会决定；
        D2 缺考未决议：有任一方标记缺考(zero/exclude)但没有采用决定；
        D3 评分员不足：有效评分员数低于 min_graders；
        D4 维度缺失：评分员未覆盖量表全部维度；
        D5 评分分歧：同一维度不同评分员采用分极差超阈值（默认取量表区间的 40%）；
        D6 量表版本错配（结构上已由录入约束拦截，仍做巡检）；
        D7 回避与成绩并存（结构上拦截录入，巡检历史数据）。
        """
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布，无需再检查")
        students = self.store.list_enrolled_students(batch_id)
        dims = self._rubric_dims(batch)
        rubric = self.store.get_rubric(batch["rubric_id"], batch["rubric_version"])
        dim_meta = {d["key"]: d for d in json.loads(rubric["dimensions_json"])}
        records = self._score_records(batch_id)
        marks = self.store.current_marks(batch_id)
        recusals = {
            (r["student_id"], r["grader_id"]) for r in self.store.list_recusals(batch_id)
        }

        by_student: dict[str, list[ScoreRecord]] = defaultdict(list)
        for rec in records:
            by_student[rec.student_id].append(rec)

        discrepancies: list[dict] = []
        resolved_keys = self.store.resolved_discrepancy_keys(batch_id)

        def add(code: str, student_id: str, detail: dict) -> None:
            key = f"{code}:{student_id}" + (
                ":" + detail["dimension"] if "dimension" in detail else ""
            )
            discrepancies.append({"code": code, "key": key, "student_id": student_id, "detail": detail})

        for sid in students:
            # D1 / D2：缺考口径（委员会已有采用决定的，以决定为准，不再报分歧）
            party_treatments = {
                party: row["treatment"]
                for (student, party), row in marks.items()
                if student == sid
            }
            decision = self.store.current_decision(batch_id, sid)
            unique_treatments = set(party_treatments.values())
            unanimous_absence = (
                len(unique_treatments) == 1 and next(iter(unique_treatments)) in (ZERO, EXCLUDE)
            )
            if decision is None:
                if len(unique_treatments) > 1:
                    add("D1", sid, {"party_treatments": party_treatments})
                # 各方一致认定缺考时按该口径采用，无需额外决议；仅有部分方标记、
                # 且无法确认一致时，要求委员会决议（D2）。
                elif any(t in (ZERO, EXCLUDE) for t in unique_treatments) and not unanimous_absence:
                    add("D2", sid, {"party_treatments": party_treatments})
            if decision is not None:
                effective_treatment = decision["treatment"]
            elif unanimous_absence:
                effective_treatment = next(iter(unique_treatments))
            elif any(t in (ZERO, EXCLUDE) for t in unique_treatments):
                effective_treatment = batch["absence_policy"]  # 分歧态，发布前会被拦截
            else:
                effective_treatment = PRESENT

            recs = [r for r in by_student.get(sid, [])]
            adopted: dict[str, dict[str, ScoreRecord]] = {}
            for r in recs:
                adopted.setdefault(r.grader_id, {})
                cur = adopted[r.grader_id].get(r.dimension)
                if cur is None or (r.round, r.score_id) > (cur.round, cur.score_id):
                    adopted[r.grader_id][r.dimension] = r

            # D3：评分员数量（有回避的评分员不计入；已按缺考处理的学生不要求评分）
            active_graders = [g for g in adopted if (sid, g) not in recusals]
            if effective_treatment in (ZERO, EXCLUDE):
                pass  # 缺考计零或排除，不要求评分
            elif len(active_graders) < batch["min_graders"]:
                add("D3", sid, {"active_graders": len(active_graders),
                                "required": batch["min_graders"]})

            # D4：维度缺失
            if effective_treatment == PRESENT:
                for g, dd in adopted.items():
                    if (sid, g) in recusals:
                        continue
                    missing = [d for d in dims if d not in dd]
                    if missing:
                        add("D4", sid, {"grader_id": g, "missing_dimensions": missing})

                # D5：评分分歧（按维度比较各评分员采用分）
                for d in dims:
                    vals = {g: dd[d].score for g, dd in adopted.items() if d in dd and (sid, g) not in recusals}
                    if len(vals) >= 2:
                        spread = max(vals.values()) - min(vals.values())
                        threshold = 0.4 * (
                            float(dim_meta[d]["max"]) - float(dim_meta[d]["min"])
                        )
                        if spread > threshold:
                            add("D5", sid, {
                                "dimension": d,
                                "values": vals,
                                "spread": round(spread, 6),
                                "threshold": round(threshold, 6),
                            })

            # D6：量表版本错配
            for r in recs:
                if (r.rubric_id, r.rubric_version) != (batch["rubric_id"], batch["rubric_version"]):
                    add("D6", sid, {"score_id": r.score_id,
                                    "rubric": f"{r.rubric_id}@{r.rubric_version}"})

            # D7：回避者仍有成绩
            for r in recs:
                if (sid, r.grader_id) in recusals:
                    add("D7", sid, {"grader_id": r.grader_id, "score_id": r.score_id})

        fresh = [d for d in discrepancies if d["key"] not in resolved_keys]
        # 已存在开放样本的差异不重复建单（多次运行检查/封存被拒时保持一个讨论线程）
        open_keys = {
            r["discrepancy_key"]
            for r in self.store.conn.execute(
                "SELECT discrepancy_key FROM review_samples "
                "WHERE batch_id = ? AND status = 'open' AND discrepancy_key IS NOT NULL",
                (batch_id,),
            ).fetchall()
        }
        try:
            seq = self._event(
                "consistency.checked",
                {"batch_id": batch_id, "found": len(discrepancies), "fresh": len(fresh),
                 "codes": sorted({d["code"] for d in fresh})},
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO consistency_runs (batch_id, status, discrepancies_json, ran_seq) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, "issues_found" if fresh else "clean",
                 json.dumps(discrepancies, ensure_ascii=False), seq),
            )
            for d in fresh:
                if d["key"] in open_keys:
                    continue
                self.store.conn.execute(
                    "INSERT INTO review_samples (batch_id, student_id, reason, discrepancy_key, "
                    "status, created_seq) VALUES (?, ?, ?, ?, 'open', ?)",
                    (batch_id, d["student_id"],
                     f"{d['code']} {json.dumps(d['detail'], ensure_ascii=False)}",
                     d["key"], seq),
                )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {"found": len(discrepancies), "fresh": len(fresh),
                "resolved_before": len(discrepancies) - len(fresh),
                "discrepancies": fresh}

    def list_review_samples(self, batch_id: str) -> list[dict]:
        self._batch_or_404(batch_id)
        rows = self.store.conn.execute(
            "SELECT * FROM review_samples WHERE batch_id = ? ORDER BY sample_id", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def discuss(self, sample_id: int, party_id: str, comment: str, actor: str) -> None:
        sample = self.store.get_sample(sample_id)
        if sample is None:
            raise NotFoundError(f"复核样本不存在：{sample_id}")
        if self.store.get_party(party_id) is None:
            raise NotFoundError(f"联考方不存在：{party_id}")
        if sample["status"] == "resolved":
            raise ConflictError("样本已决议，不能再追加讨论")
        try:
            seq = self._event(
                "review.discussed",
                {"sample_id": sample_id, "party_id": party_id, "comment": comment},
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO review_discussions (sample_id, party_id, comment, recorded_seq) "
                "VALUES (?, ?, ?, ?)",
                (sample_id, party_id, comment, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    def resolve_sample(self, sample_id: int, decision: str, actor: str) -> None:
        """决议复核样本。若是缺考口径类问题，应同时（或之后）调用 resolve_absence
        留下采用的口径决定；决议本身只追加。"""
        sample = self.store.get_sample(sample_id)
        if sample is None:
            raise NotFoundError(f"复核样本不存在：{sample_id}")
        if sample["status"] == "resolved":
            raise ConflictError("样本已决议")
        try:
            seq = self._event(
                "review.resolved",
                {"sample_id": sample_id, "decision": decision},
                actor,
            )
            self.store.conn.execute(
                "UPDATE review_samples SET status = 'resolved', decision = ?, resolved_seq = ? "
                "WHERE sample_id = ?",
                (decision, seq, sample_id),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    # -- 封存 / 重开 ----------------------------------------------------------

    def seal_batch(self, batch_id: str, actor: str) -> dict:
        """封存：必须先通过一致性检查（无未解决差异）。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] != "open":
            raise ConflictError(f"批次当前状态为 {batch['status']}，不能封存")
        report = self.run_consistency_check(batch_id, actor)
        if report["fresh"]:
            raise ConflictError(
                f"存在 {report['fresh']} 项未解决差异，全部进入讨论并决议后方可封存"
            )
        try:
            seq = self._event("batch.sealed", {"batch_id": batch_id}, actor)
            self.store.conn.execute(
                "UPDATE batches SET status = 'sealed', sealed_seq = ? WHERE batch_id = ?",
                (seq, batch_id),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {"status": "sealed", "check": report}

    def reopen_batch(self, batch_id: str, actor: str, reason: str) -> None:
        """委员会主动重开（区别于迟到成绩触发的自动重开）。

        早先封存、签署、检查记录全部保留；重开后它们一律失效，须重新检查与会签。
        已发布批次不可重开。
        """
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布，不能重开")
        if batch["status"] == "open":
            raise ConflictError("批次本就处于开放状态")
        try:
            self.store.conn.execute(
                "UPDATE batches SET status = 'open', reopen_count = reopen_count + 1 "
                "WHERE batch_id = ?",
                (batch_id,),
            )
            self._event("batch.reopened", {"batch_id": batch_id, "reason": reason}, actor)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    # -- 会签与发布 -----------------------------------------------------------

    def sign(self, batch_id: str, party_id: str, actor: str) -> None:
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布")
        if self.store.get_party(party_id) is None:
            raise NotFoundError(f"联考方不存在：{party_id}")
        try:
            seq = self._event(
                "signature.signed", {"batch_id": batch_id, "party_id": party_id}, actor
            )
            self.store.conn.execute(
                "INSERT INTO signature_events (batch_id, party_id, action, actor, recorded_seq) "
                "VALUES (?, ?, 'sign', ?, ?)",
                (batch_id, party_id, actor, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    def unsign(self, batch_id: str, party_id: str, actor: str) -> None:
        """撤回签署（重开前允许）：只追加 sign/unsign 事件。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布")
        try:
            seq = self._event(
                "signature.unsigned", {"batch_id": batch_id, "party_id": party_id}, actor
            )
            self.store.conn.execute(
                "INSERT INTO signature_events (batch_id, party_id, action, actor, recorded_seq) "
                "VALUES (?, ?, 'unsign', ?, ?)",
                (batch_id, party_id, actor, seq),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

    def _effective_treatment(self, batch, student_id: str, marks: dict) -> tuple[str, str | None]:
        """确定学生采用的缺考口径：委员会决定优先；否则按各方标记与批次默认口径推断。"""
        decision = self.store.current_decision(batch["batch_id"], student_id)
        if decision is not None:
            return decision["treatment"], f"committee:{decision['reason']}"
        party_treatments = {
            party: row["treatment"]
            for (student, party), row in marks.items()
            if student == student_id
        }
        non_present = [t for t in party_treatments.values() if t != PRESENT]
        if not non_present:
            return PRESENT, None
        # 无委员会决定且口径不一致时，调用方不应进入发布（检查会拦截）；
        # 为复算健壮性，保守按批次默认口径处理并标注。
        return batch["absence_policy"], "batch_default_fallback"

    def compute_report(self, batch_id: str) -> dict:
        """沿每位评分员的原始评分与采用规则完整复算，生成带溯源的报告。"""
        batch = self._batch_or_404(batch_id)
        dims = self._rubric_dims(batch)
        records = self._score_records(batch_id)
        marks = self.store.current_marks(batch_id)
        recusals = {(r["student_id"], r["grader_id"]) for r in self.store.list_recusals(batch_id)}
        by_student: dict[str, list[ScoreRecord]] = defaultdict(list)
        for rec in records:
            if (rec.student_id, rec.grader_id) in recusals:
                continue  # 回避评分员的任何成绩都不进入复算
            by_student[rec.student_id].append(rec)

        results = []
        for sid in self.store.list_enrolled_students(batch_id):
            treatment, reason = self._effective_treatment(batch, sid, marks)
            res = compute_student(sid, treatment, by_student.get(sid, []), dims, reason)
            results.append(res)
        assign_ranks(results)

        return {
            "batch_id": batch_id,
            "rubric": {"rubric_id": batch["rubric_id"], "version": batch["rubric_version"]},
            "absence_policy": self.store.current_absence_policy(batch),
            "min_graders": batch["min_graders"],
            "students": [
                {
                    "student_id": r.student_id,
                    "treatment": r.treatment,
                    "final_score": r.final_score,
                    "rank": r.rank,
                    "grader_totals": r.grader_totals,
                    "trace": r.trace,
                }
                for r in results
            ],
        }

    def publish(self, batch_id: str, actor: str) -> dict:
        """法定发布：封存状态 + 无未解决差异 + 有效签署方数达到约定数。"""
        batch = self._batch_or_404(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布")
        if batch["status"] != "sealed":
            raise ConflictError("批次尚未封存，不能发布")
        open_samples = self.store.list_open_samples(batch_id)
        if open_samples:
            raise ConflictError(f"仍有 {len(open_samples)} 个复核样本未决议，不能发布")
        sigs = self.store.current_signatures(batch_id)
        if len(sigs) < batch["required_signatures"]:
            raise ConflictError(
                f"签署不足：当前 {len(sigs)} 方，约定需要 {batch['required_signatures']} 方"
            )
        report = self.compute_report(batch_id)
        try:
            seq = self._event(
                "batch.published",
                {"batch_id": batch_id, "signatories": sorted(sigs), "report": report},
                actor,
            )
            self.store.conn.execute(
                "INSERT INTO published_results (batch_id, report_json, published_seq) "
                "VALUES (?, ?, ?)",
                (batch_id, json.dumps(report, ensure_ascii=False, sort_keys=True), seq),
            )
            self.store.conn.execute(
                "UPDATE batches SET status = 'published', published_seq = ? WHERE batch_id = ?",
                (seq, batch_id),
            )
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {"status": "published", "signatories": sorted(sigs), "report": report}

    def get_published_report(self, batch_id: str) -> dict:
        self._batch_or_404(batch_id)
        row = self.store.list_published(batch_id)
        if row is None:
            raise NotFoundError("批次尚未发布")
        return json.loads(row["report_json"])

    # -- 查询 -----------------------------------------------------------------

    def batch_status(self, batch_id: str) -> dict:
        batch = self._batch_or_404(batch_id)
        sigs = self.store.current_signatures(batch_id)
        return {
            "batch_id": batch_id,
            "title": batch["title"],
            "status": batch["status"],
            "reopen_count": batch["reopen_count"],
            "rubric_id": batch["rubric_id"],
            "rubric_version": batch["rubric_version"],
            "absence_policy": self.store.current_absence_policy(batch),
            "required_signatures": batch["required_signatures"],
            "current_signatures": sorted(sigs),
            "signature_count": len(sigs),
            "open_samples": len(self.store.list_open_samples(batch_id)),
        }
