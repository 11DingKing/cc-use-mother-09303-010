"""领域引擎：批次状态机、一致性检查、缺考口径、归约复算与会签发布。

事件归约规则（全部从只追加事件流确定性计算）：

- 同一评分员对同一学生的有效成绩，按 ``seq`` 折叠：``score``/``rescore``
  更新有效值；``late`` 仅在该评分员尚无有效成绩时生效（**迟到成绩不覆盖
  早先记录**）；``recusal`` 之后该评分员被排除，其更早的事件仍保留在日志中。
- 各方缺考状态由最后一条 ``absent`` / ``absence_retraction`` 事件决定。
- 封存快照记录当时的 ``max_seq``，之后新增的复评分等事件不会改变历史快照，
  只影响新一轮的归约结果（**复评分、回避、批次重开都不覆盖早先记录**）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import ConflictError, NotFoundError, ValidationError
from .store import Store

STATUS_FLOW = ["grading", "calibration", "review", "countersign", "published"]
STATUS_CN = {
    "grading": "评分",
    "calibration": "校准",
    "review": "复核",
    "countersign": "会签",
    "published": "发布",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round4(x: float) -> float:
    return round(float(x) + 0.0, 4)


class ModerationService:
    def __init__(
        self, store: Store, now_fn: Callable[[], str] = utcnow_iso,
    ) -> None:
        self.store = store
        self.now = now_fn

    # ================= 主数据：方、量表、评分员资格、学生 =================

    def register_party(self, code: str, name: str) -> dict:
        if self.store.get_party(code) is not None:
            raise ConflictError(f"方 {code} 已存在")
        self.store.create_party(code, name, self.now())
        return {"code": code, "name": name}

    def register_rubric(
        self, id_: str, title: str, scale_min: float, scale_max: float,
        checksum: str | None = None,
    ) -> dict:
        if scale_min >= scale_max:
            raise ValidationError("量表下界必须小于上界")
        if self.store.get_rubric(id_) is not None:
            raise ConflictError(f"量表版本 {id_} 已存在")
        checksum = checksum or _sha256(
            _canonical([title, scale_min, scale_max])
        )
        self.store.create_rubric(
            id_, title, float(scale_min), float(scale_max), checksum, self.now(),
        )
        return {"id": id_, "title": title, "checksum": checksum}

    def register_rater(
        self, id_: str, name: str, party: str, role: str = "teacher",
    ) -> dict:
        if role not in ("teacher", "coordinator"):
            raise ValidationError("角色必须是 teacher 或 coordinator")
        if self.store.get_party(party) is None:
            raise NotFoundError(f"方 {party} 不存在")
        if self.store.get_rater(id_) is not None:
            raise ConflictError(f"评分员 {id_} 已存在")
        self.store.create_rater(id_, name, party, role, self.now())
        return {"id": id_, "name": name, "party": party, "role": role}

    def grant_qualification(self, rater_id: str, rubric_id: str) -> dict:
        rater = self._rater(rater_id)
        if self.store.get_rubric(rubric_id) is None:
            raise NotFoundError(f"量表版本 {rubric_id} 不存在")
        if self.store.qualification_active(rater_id, rubric_id, self.now()):
            raise ConflictError("资格已处于有效状态")
        self.store.grant_qualification(rater_id, rubric_id, self.now())
        return {"rater_id": rater_id, "rubric_id": rubric_id, "active": True}

    def revoke_qualification(self, rater_id: str, rubric_id: str) -> dict:
        self._rater(rater_id)
        if not self.store.qualification_active(rater_id, rubric_id, self.now()):
            raise ConflictError("资格当前无效，无法撤销")
        self.store.revoke_qualification(rater_id, rubric_id, self.now())
        return {"rater_id": rater_id, "rubric_id": rubric_id, "active": False}

    def register_student(self, id_: str, name: str) -> dict:
        if self.store.get_student(id_) is not None:
            raise ConflictError(f"学生 {id_} 已存在")
        self.store.create_student(id_, name, self.now())
        return {"id": id_, "name": name}

    # ================= 批次配置 =================

    def create_batch(
        self, id_: str, rubric_id: str, absence_policies: dict[str, str],
        gap_threshold: float = 10.0, grading_deadline: str | None = None,
        min_signatures: int = 2, signature_rule: str = "both",
    ) -> dict:
        if self.store.get_batch(id_) is not None:
            raise ConflictError(f"批次 {id_} 已存在")
        if self.store.get_rubric(rubric_id) is None:
            raise NotFoundError(f"量表版本 {rubric_id} 不存在")
        if len(absence_policies) < 2:
            raise ValidationError("至少需要约定两方的缺考口径")
        for party, policy in absence_policies.items():
            if self.store.get_party(party) is None:
                raise NotFoundError(f"方 {party} 不存在")
            if policy not in ("zero", "exclude"):
                raise ValidationError("缺考口径必须是 zero 或 exclude")
        if signature_rule not in ("both", "any"):
            raise ValidationError("签署规则必须是 both 或 any")
        if min_signatures < 1:
            raise ValidationError("约定签署数至少为 1")
        parties = sorted(absence_policies)
        batch = {
            "id": id_,
            "rubric_id": rubric_id,
            "parties": [(p, absence_policies[p]) for p in parties],
            "gap_threshold": float(gap_threshold),
            "grading_deadline": grading_deadline,
            "min_signatures": min_signatures,
            "signature_rule": signature_rule,
        }
        self.store.create_batch(batch, self.now())
        return self.get_batch(id_)

    def add_student(self, batch_id: str, student_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "grading":
            raise ConflictError("仅评分状态可向批次加入学生")
        if self.store.get_student(student_id) is None:
            raise NotFoundError(f"学生 {student_id} 不存在")
        if not self.store.add_batch_student(batch_id, student_id, self.now()):
            raise ConflictError("学生已在批次中")
        return {"batch_id": batch_id, "student_id": student_id}

    # ================= 评分事件（只追加） =================

    def record_score(
        self, batch_id: str, student_id: str, rater_id: str, value: float,
        as_of: str | None = None, accept_late: bool = False,
        reason: str | None = None,
    ) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "grading":
            raise ConflictError("当前批次状态不接受评分")
        self._enrolled(batch_id, student_id)
        rater = self._rater(rater_id)
        rubric_id = batch["rubric_id"]
        effective_at = as_of or self.now()
        if not self.store.qualification_active(rater_id, rubric_id, effective_at):
            raise ConflictError("评分员在该评分时点不具备此量表版本的资格")
        rubric = self.store.get_rubric(rubric_id)
        if not (rubric["scale_min"] <= float(value) <= rubric["scale_max"]):
            raise ValidationError(
                f"成绩 {value} 超出量表 [{rubric['scale_min']},"
                f" {rubric['scale_max']}]"
            )
        folded = self._fold(batch_id)
        rstate = folded["students"][student_id]["raters"].get(rater_id)
        if rstate and rstate["recused"]:
            raise ConflictError("该评分员已回避此学生，不能评分")
        late = bool(
            batch["grading_deadline"] and effective_at > batch["grading_deadline"]
        )
        if late and not accept_late:
            raise ConflictError("超过评分截止时间；迟到成绩须显式 accept_late")
        kind = "late" if late else "score"
        seq = self.store.insert_event({
            "batch_id": batch_id,
            "window_n": batch["current_window"],
            "student_id": student_id,
            "rater_id": rater_id,
            "party": rater["party"],
            "kind": kind,
            "value": float(value),
            "recorded_at": self.now(),
            "effective_at": effective_at,
            "reason": reason,
        })
        return {"seq": seq, "kind": kind, "student_id": student_id,
                "rater_id": rater_id, "value": float(value)}

    def declare_absence(
        self, batch_id: str, student_id: str, party: str,
        reporter_id: str, reason: str,
    ) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] not in ("grading", "calibration"):
            raise ConflictError("仅评分/校准阶段可登记缺考")
        self._enrolled(batch_id, student_id)
        self._rater(reporter_id)
        self._party_of_batch(batch, party)
        seq = self.store.insert_event({
            "batch_id": batch_id,
            "window_n": batch["current_window"],
            "student_id": student_id,
            "rater_id": reporter_id,
            "party": party,
            "kind": "absent",
            "value": None,
            "recorded_at": self.now(),
            "effective_at": self.now(),
            "reason": reason,
        })
        return {"seq": seq, "kind": "absent", "student_id": student_id,
                "party": party}

    def retract_absence(
        self, batch_id: str, student_id: str, party: str,
        reporter_id: str, reason: str,
    ) -> dict:
        """缺考撤回（如学生参加缓考）：以新事件表达，不删除早先声明。"""
        batch = self._batch(batch_id)
        if batch["status"] not in ("grading", "calibration"):
            raise ConflictError("仅评分/校准阶段可撤回缺考")
        self._enrolled(batch_id, student_id)
        self._rater(reporter_id)
        self._party_of_batch(batch, party)
        seq = self.store.insert_event({
            "batch_id": batch_id,
            "window_n": batch["current_window"],
            "student_id": student_id,
            "rater_id": reporter_id,
            "party": party,
            "kind": "absence_retraction",
            "value": None,
            "recorded_at": self.now(),
            "effective_at": self.now(),
            "reason": reason,
        })
        return {"seq": seq, "kind": "absence_retraction",
                "student_id": student_id, "party": party}

    def mark_recusal(
        self, batch_id: str, student_id: str, rater_id: str, reason: str,
    ) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] not in ("grading", "review"):
            raise ConflictError("仅评分/复核阶段可登记回避")
        self._enrolled(batch_id, student_id)
        rater = self._rater(rater_id)
        seq = self.store.insert_event({
            "batch_id": batch_id,
            "window_n": batch["current_window"],
            "student_id": student_id,
            "rater_id": rater_id,
            "party": rater["party"],
            "kind": "recusal",
            "value": None,
            "recorded_at": self.now(),
            "effective_at": self.now(),
            "reason": reason,
        })
        return {"seq": seq, "kind": "recusal", "student_id": student_id,
                "rater_id": rater_id}

    # ================= 一致性检查与封存 =================

    def run_consistency_check(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] not in ("grading", "review"):
            raise ConflictError("仅评分/复核阶段运行一致性检查")
        window_n = batch["current_window"]
        folded = self._fold(batch_id)
        policies = self._policies(batch)
        findings: list[dict[str, Any]] = []

        for sid in folded["students"]:
            st = folded["students"][sid]
            party_means: dict[str, float] = {}
            for party in policies:
                absent = st["absent"].get(party, False)
                scores = [
                    rs["effective"]["value"]
                    for rid, rs in st["raters"].items()
                    if rs["party"] == party and rs["effective"] is not None
                ]
                if absent and scores:
                    findings.append(self._finding(
                        batch_id, window_n,
                        f"absence_contradiction:{sid}:{party}",
                        "absence", "error", sid,
                        {"message": f"{party} 方同时登记缺考与有效成绩",
                         "party": party, "score_count": len(scores)},
                    ))
                if absent:
                    continue
                if not scores:
                    findings.append(self._finding(
                        batch_id, window_n,
                        f"missing:{sid}:{party}", "coverage", "error", sid,
                        {"message": f"{party} 方既无成绩也无缺考声明",
                         "party": party},
                    ))
                else:
                    party_means[party] = _round4(sum(scores) / len(scores))
            absent_parties = {p for p, v in st["absent"].items() if v}
            scored_parties = set(party_means)
            if absent_parties and scored_parties:
                findings.append(self._finding(
                    batch_id, window_n,
                    f"absence_fact_divergence:{sid}", "absence", "warning", sid,
                    {"message": "一方登记缺考、另一方提交成绩，须讨论确认事实",
                     "absent_parties": sorted(absent_parties),
                     "scored_parties": sorted(scored_parties)},
                ))
            if absent_parties and len(set(policies.values())) > 1:
                findings.append(self._finding(
                    batch_id, window_n,
                    f"policy_divergence:{sid}", "absence", "warning", sid,
                    {"message": "各方缺考口径不一致（零分/排除），须讨论确认",
                     "policies": policies},
                ))
            if len(party_means) == 2:
                (p1, p2) = sorted(party_means)
                gap = abs(party_means[p1] - party_means[p2])
                if gap > batch["gap_threshold"]:
                    findings.append(self._finding(
                        batch_id, window_n,
                        f"score_gap:{sid}", "gap", "warning", sid,
                        {"message": "双方评分差异超阈值", "gap": _round4(gap),
                         "threshold": batch["gap_threshold"],
                         "party_means": party_means},
                    ))
            for rid, rs in st["raters"].items():
                if rs["recused"] and rs["history"]:
                    findings.append(self._finding(
                        batch_id, window_n,
                        f"recusal:{sid}:{rid}", "recusal", "warning", sid,
                        {"message": "评分员回避后其早先成绩被排除（记录保留）",
                         "rater_id": rid, "party": rs["party"]},
                    ))
                if any(h["kind"] == "late" for h in rs["history"]):
                    findings.append(self._finding(
                        batch_id, window_n,
                        f"late:{sid}:{rid}", "late", "warning", sid,
                        {"message": "存在迟到成绩，须讨论确认（未覆盖早先成绩）",
                         "rater_id": rid, "party": rs["party"],
                         "adopted": rs["effective"] is not None
                         and rs["effective"]["kind"] == "late"},
                    ))

        # 本轮已不存在的问题自动关闭
        active_keys = {f["key"] for f in findings}
        open_now = 0
        for d in self.store.list_discussions(batch_id, window_n):
            if d["status"] == "open" and d["key"] not in active_keys:
                self.store.set_discussion_status(d["id"], "auto_closed", self.now())
            if d["key"] in active_keys and d["status"] == "open":
                open_now += 1
        self.store.add_check_run(
            batch_id, window_n, self.now(), findings, open_now,
        )
        return {"window_n": window_n, "findings": findings,
                "open_count": open_now}

    def _finding(
        self, batch_id: str, window_n: int, key: str, category: str,
        severity: str, student_id: str | None, detail: dict,
    ) -> dict:
        did, created = self.store.upsert_discussion({
            "batch_id": batch_id, "window_n": window_n, "key": key,
            "category": category, "severity": severity,
            "student_id": student_id, "detail": detail,
        }, self.now())
        return {"key": key, "category": category, "severity": severity,
                "student_id": student_id, "detail": detail,
                "discussion_id": did, "created": created}

    def seal_calibration(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "grading":
            raise ConflictError("仅评分状态可封存")
        result = self.run_consistency_check(batch_id)
        errors = [f for f in result["findings"] if f["severity"] == "error"]
        if errors:
            raise ConflictError(
                f"存在 {len(errors)} 项阻断性差异，不能封存："
                + "、".join(f["key"] for f in errors)
            )
        snapshot, checksum, max_seq = self._snapshot(batch)
        self.store.add_seal(
            batch_id, batch["current_window"], self.now(),
            _canonical(snapshot), checksum,
        )
        self.store.close_window(batch_id, batch["current_window"], self.now())
        self.store.set_status(batch_id, "calibration", self.now())
        return {"window_n": batch["current_window"], "checksum": checksum,
                "max_seq": max_seq, "warnings": len(
                    [f for f in result["findings"] if f["severity"] == "warning"])}

    def reopen_batch(self, batch_id: str, reason: str) -> dict:
        """重开批次：开启新窗口，早先封存快照保持不变。"""
        batch = self._batch(batch_id)
        if batch["status"] == "published":
            raise ConflictError("已发布批次不能重开")
        if batch["status"] not in ("calibration", "review"):
            raise ConflictError("仅校准/复核阶段可重开")
        new_window = batch["current_window"] + 1
        self.store.open_window(batch_id, new_window, self.now(), reason)
        self.store.set_status(batch_id, "grading", self.now(), new_window)
        return {"window_n": new_window, "status": "grading", "reason": reason}

    # ================= 讨论与复核 =================

    def add_discussion_comment(
        self, discussion_id: int, author_id: str, body: str,
    ) -> dict:
        d = self.store.get_discussion(discussion_id)
        if d is None:
            raise NotFoundError("讨论不存在")
        self._rater(author_id)
        if not body or not body.strip():
            raise ValidationError("评论内容不能为空")
        self.store.add_comment(discussion_id, author_id, body, self.now())
        return {"discussion_id": discussion_id, "author": author_id}

    def acknowledge_discussion(
        self, discussion_id: int, coordinator_id: str, rationale: str,
    ) -> dict:
        d = self.store.get_discussion(discussion_id)
        if d is None:
            raise NotFoundError("讨论不存在")
        rater = self._rater(coordinator_id)
        if rater["role"] != "coordinator":
            raise ConflictError("仅考核协调员可结案讨论")
        if d["status"] not in ("open", "acknowledged"):
            raise ConflictError("讨论已自动关闭，无需结案")
        self.store.add_resolution({
            "batch_id": d["batch_id"], "window_n": d["window_n"],
            "discussion_id": discussion_id, "student_id": d["student_id"],
            "rater_id": None, "decision": "acknowledged",
            "rescore_seq": None, "decided_by": coordinator_id,
            "rationale": rationale, "decided_at": self.now(),
        })
        self.store.set_discussion_status(discussion_id, "acknowledged", self.now())
        return {"discussion_id": discussion_id, "status": "acknowledged"}

    def begin_review(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "calibration":
            raise ConflictError("仅校准状态可进入复核")
        self.store.set_status(batch_id, "review", self.now())
        return {"status": "review"}

    def add_review_sample(
        self, batch_id: str, student_id: str, reason: str,
    ) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "review":
            raise ConflictError("仅复核阶段抽取样本")
        self._enrolled(batch_id, student_id)
        if not self.store.add_review_sample(
            batch_id, batch["current_window"], student_id, reason, self.now(),
        ):
            raise ConflictError("该学生已在复核样本中")
        return {"student_id": student_id, "reason": reason}

    def record_rescore(
        self, batch_id: str, student_id: str, rater_id: str, value: float,
        discussion_id: int, decided_by: str, rationale: str,
    ) -> dict:
        """复核后的复评分：追加新事件，绝不覆盖原始评分。"""
        batch = self._batch(batch_id)
        if batch["status"] != "review":
            raise ConflictError("仅复核阶段可记录复评分")
        self._enrolled(batch_id, student_id)
        rater = self._rater(rater_id)
        decider = self._rater(decided_by)
        if decider["role"] != "coordinator":
            raise ConflictError("复评分须由考核协调员登记")
        d = self.store.get_discussion(discussion_id)
        if d is None or d["batch_id"] != batch_id:
            raise NotFoundError("关联讨论不存在")
        if d["student_id"] is not None and d["student_id"] != student_id:
            raise ValidationError("复评分学生与讨论不一致")
        rubric = self.store.get_rubric(batch["rubric_id"])
        if not (rubric["scale_min"] <= float(value) <= rubric["scale_max"]):
            raise ValidationError("复评分超出量表范围")
        if not self.store.qualification_active(
            rater_id, batch["rubric_id"], self.now(),
        ):
            raise ConflictError("评分员当前不具备此量表资格")
        folded = self._fold(batch_id)
        rstate = folded["students"][student_id]["raters"].get(rater_id)
        if rstate and rstate["recused"]:
            raise ConflictError("该评分员已回避，不能复评")
        seq = self.store.insert_event({
            "batch_id": batch_id,
            "window_n": batch["current_window"],
            "student_id": student_id,
            "rater_id": rater_id,
            "party": rater["party"],
            "kind": "rescore",
            "value": float(value),
            "recorded_at": self.now(),
            "effective_at": self.now(),
            "reason": rationale,
            "discussion_id": discussion_id,
        })
        self.store.add_resolution({
            "batch_id": batch_id, "window_n": batch["current_window"],
            "discussion_id": discussion_id, "student_id": student_id,
            "rater_id": rater_id, "decision": "adopt_rescore",
            "rescore_seq": seq, "decided_by": decided_by,
            "rationale": rationale, "decided_at": self.now(),
        })
        self.store.set_discussion_status(discussion_id, "acknowledged", self.now())
        return {"seq": seq, "kind": "rescore", "student_id": student_id,
                "rater_id": rater_id, "value": float(value)}

    # ================= 会签与发布 =================

    def begin_countersign(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "review":
            raise ConflictError("仅复核状态可进入会签")
        result = self.run_consistency_check(batch_id)
        if result["open_count"]:
            raise ConflictError("复评后仍有未结案差异，不能进入会签")
        self.store.set_status(batch_id, "countersign", self.now())
        return {"status": "countersign"}

    def sign(self, batch_id: str, signer_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] != "countersign":
            raise ConflictError("仅会签阶段可签署")
        rater = self._rater(signer_id)
        if rater["role"] != "coordinator":
            raise ConflictError("仅考核协调员可签署")
        if not self.store.qualification_active(
            signer_id, batch["rubric_id"], self.now(),
        ):
            raise ConflictError("签署人须具备该量表版本资格")
        created = self.store.add_signature(
            batch_id, signer_id, rater["party"], self.now(),
        )
        if not created:
            raise ConflictError("已签署，不能重复签署")
        return self.signature_status(batch_id)

    def signature_status(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        sigs = self.store.list_signatures(batch_id)
        policies = self._policies(batch)
        parties_signed = {s["party"] for s in sigs}
        rule = batch["signature_rule"]
        if rule == "both":
            quorum = (
                len(parties_signed) >= len(policies)
                and len(sigs) >= batch["min_signatures"]
            )
        else:
            quorum = len(sigs) >= batch["min_signatures"]
        return {"rule": rule, "min_signatures": batch["min_signatures"],
                "signature_count": len(sigs),
                "parties_signed": sorted(parties_signed),
                "required_parties": sorted(policies), "quorum": quorum}

    def publish(self, batch_id: str) -> dict:
        batch = self._batch(batch_id)
        if batch["status"] == "published":
            raise ConflictError("批次已发布")
        if batch["status"] != "countersign":
            raise ConflictError("仅会签阶段可发布")
        status = self.signature_status(batch_id)
        if not status["quorum"]:
            raise ConflictError("未达到约定签署数/签署方要求，不能发布")
        results, max_seq = self.compute_results(batch_id)
        rule = {
            "rubric_id": batch["rubric_id"],
            "absence_policies": self._policies(batch),
            "gap_threshold": batch["gap_threshold"],
            "aggregation": "party_mean_then_mean_across_parties",
            "signature_rule": batch["signature_rule"],
            "min_signatures": batch["min_signatures"],
            "as_of_seq": max_seq,
        }
        checksum = _sha256(_canonical({"rule": rule, "results": results}))
        self.store.save_publication(
            batch_id, self.now(), _canonical(rule),
            _canonical(results), checksum,
        )
        self.store.set_status(batch_id, "published", self.now())
        return {"batch_id": batch_id, "checksum": checksum, "rule": rule,
                "results": results, "signatures": status}

    def get_publication(self, batch_id: str) -> dict:
        self._batch(batch_id)
        row = self.store.get_publication(batch_id)
        if row is None:
            raise NotFoundError("批次尚未发布")
        return {
            "batch_id": batch_id,
            "published_at": row["published_at"],
            "rule": json.loads(row["rule_json"]),
            "results": json.loads(row["result_json"]),
            "checksum": row["checksum"],
        }

    def verify_publication(self, batch_id: str) -> dict:
        """沿事件流与采用规则复算，校验发布结果未被篡改。"""
        pub = self.get_publication(batch_id)
        results, max_seq = self.compute_results(
            batch_id, as_of_seq=pub["rule"]["as_of_seq"],
        )
        recomputed = _sha256(
            _canonical({"rule": pub["rule"], "results": results})
        )
        return {"published_checksum": pub["checksum"],
                "recomputed_checksum": recomputed,
                "match": recomputed == pub["checksum"]}

    # ================= 查询、复算与追溯 =================

    def get_batch(self, batch_id: str) -> dict:
        b = self._batch(batch_id)
        return {
            "id": b["id"], "rubric_id": b["rubric_id"],
            "status": b["status"], "status_cn": STATUS_CN[b["status"]],
            "current_window": b["current_window"],
            "absence_policies": self._policies(b),
            "gap_threshold": b["gap_threshold"],
            "grading_deadline": b["grading_deadline"],
            "min_signatures": b["min_signatures"],
            "signature_rule": b["signature_rule"],
        }

    def list_events(self, batch_id: str) -> list[dict]:
        self._batch(batch_id)
        return [dict(e) for e in self.store.list_events(batch_id)]

    def list_discussions(self, batch_id: str) -> list[dict]:
        self._batch(batch_id)
        out = []
        for d in self.store.list_discussions(batch_id):
            item = dict(d)
            item["detail"] = json.loads(item.pop("detail_json"))
            out.append(item)
        return out

    def list_seals(self, batch_id: str) -> list[dict]:
        self._batch(batch_id)
        return [{"window_n": s["window_n"], "sealed_at": s["sealed_at"],
                 "checksum": s["checksum"],
                 "snapshot": json.loads(s["snapshot_json"])}
                for s in self.store.list_seals(batch_id)]

    def compute_results(
        self, batch_id: str, as_of_seq: int | None = None,
    ) -> tuple[dict, int]:
        batch = self._batch(batch_id)
        folded = self._fold(batch_id, as_of_seq=as_of_seq)
        policies = self._policies(batch)
        rows = []
        max_seq = 0
        for sid in folded["students"]:
            st = folded["students"][sid]
            contributions: dict[str, dict] = {}
            excluded_parties: list[str] = []
            missing_parties: list[str] = []
            for party, policy in policies.items():
                if st["absent"].get(party, False):
                    if policy == "zero":
                        contributions[party] = {
                            "status": "absent_zero", "value": 0.0,
                            "rater_ids": [], "policy": policy,
                        }
                    else:
                        excluded_parties.append(party)
                        contributions[party] = {
                            "status": "absent_excluded", "value": None,
                            "rater_ids": [], "policy": policy,
                        }
                    continue
                scores = [
                    {"rater_id": rid, **rs["effective"]}
                    for rid, rs in st["raters"].items()
                    if rs["party"] == party and rs["effective"] is not None
                ]
                if not scores:
                    missing_parties.append(party)
                    contributions[party] = {
                        "status": "missing", "value": None,
                        "rater_ids": [], "policy": None,
                    }
                    continue
                mean = _round4(
                    sum(s["value"] for s in scores) / len(scores)
                )
                contributions[party] = {
                    "status": "scored", "value": mean,
                    "rater_ids": sorted(s["rater_id"] for s in scores),
                    "policy": None,
                }
            present = [c["value"] for c in contributions.values()
                       if c["value"] is not None]
            student = self.store.get_student(sid)
            if not present:
                rows.append({
                    "student_id": sid, "name": student["name"],
                    "final_score": None, "rank": None,
                    "excluded": True,
                    "excluded_parties": excluded_parties,
                    "missing_parties": missing_parties,
                    "parties": contributions,
                })
                continue
            rows.append({
                "student_id": sid, "name": student["name"],
                "final_score": _round4(sum(present) / len(present)),
                "rank": None,
                "excluded": False,
                "excluded_parties": excluded_parties,
                "missing_parties": missing_parties,
                "parties": contributions,
            })
        ranked = sorted(
            (r for r in rows if not r["excluded"]),
            key=lambda r: (-r["final_score"], r["student_id"]),
        )
        for i, r in enumerate(ranked, start=1):
            r["rank"] = i
        rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0,
                                 r["student_id"]))
        if as_of_seq is None:
            seqs = [e["seq"] for e in self.store.list_events(batch_id)]
            max_seq = max(seqs, default=0)
        else:
            max_seq = as_of_seq
        return {"students": rows}, max_seq

    def student_trace(self, batch_id: str, student_id: str) -> dict:
        """沿每位评分员与采用规则给出可复算的完整追溯。"""
        batch = self._batch(batch_id)
        self._enrolled(batch_id, student_id)
        events = [
            dict(e) for e in self.store.list_events(batch_id)
            if e["student_id"] == student_id
        ]
        results, _ = self.compute_results(batch_id)
        current = next(
            (r for r in results["students"] if r["student_id"] == student_id),
            None,
        )
        folded = self._fold(batch_id)
        st = folded["students"].get(student_id, {"raters": {}, "absent": {}})
        rater_lines = {}
        for rid, rs in st["raters"].items():
            rater_lines[rid] = {
                "party": rs["party"],
                "recused": rs["recused"],
                "effective": rs["effective"],
                "history": rs["history"],
            }
        return {
            "batch_id": batch_id, "student_id": student_id,
            "rule": {
                "absence_policies": self._policies(batch),
                "aggregation": "party_mean_then_mean_across_parties",
                "gap_threshold": batch["gap_threshold"],
            },
            "absence_state": dict(sorted(st["absent"].items())),
            "by_rater": rater_lines,
            "events": events,
            "current_result": current,
        }

    # ================= 内部辅助 =================

    def _snapshot(self, batch) -> tuple[dict, str, int]:
        results, max_seq = self.compute_results(batch["id"])
        snapshot = {
            "batch_id": batch["id"], "window_n": batch["current_window"],
            "rubric_id": batch["rubric_id"], "as_of_seq": max_seq,
            "policies": self._policies(batch), "results": results,
        }
        return snapshot, _sha256(_canonical(snapshot)), max_seq

    def _fold(
        self, batch_id: str, as_of_seq: int | None = None,
    ) -> dict:
        """把只追加事件流归约为当前（或指定 seq 时点的）有效状态。"""
        students = {
            s["id"]: {"absent": {}, "raters": {}}
            for s in self.store.list_batch_students(batch_id)
        }
        for e in self.store.list_events(batch_id):
            if as_of_seq is not None and e["seq"] > as_of_seq:
                break
            sid = e["student_id"]
            if sid not in students:
                continue
            st = students[sid]
            kind = e["kind"]
            if kind in ("absent", "absence_retraction"):
                st["absent"][e["party"]] = kind == "absent"
                continue
            rs = st["raters"].get(e["rater_id"])
            if rs is None:
                rs = {"party": e["party"], "recused": False,
                      "effective": None, "history": []}
                st["raters"][e["rater_id"]] = rs
            rs["history"].append({
                "seq": e["seq"], "window_n": e["window_n"],
                "kind": kind, "value": e["value"],
                "recorded_at": e["recorded_at"],
                "effective_at": e["effective_at"],
                "reason": e["reason"],
                "discussion_id": e["discussion_id"],
                "became_effective": False,
            })
            if kind == "recusal":
                rs["recused"] = True
                rs["effective"] = None
            elif kind in ("score", "rescore"):
                if not rs["recused"]:
                    rs["effective"] = {
                        "kind": kind, "value": _round4(e["value"]),
                        "seq": e["seq"], "window_n": e["window_n"],
                    }
                    rs["history"][-1]["became_effective"] = True
            elif kind == "late":
                if not rs["recused"] and rs["effective"] is None:
                    rs["effective"] = {
                        "kind": "late", "value": _round4(e["value"]),
                        "seq": e["seq"], "window_n": e["window_n"],
                    }
                    rs["history"][-1]["became_effective"] = True
        return {"students": students}

    def _policies(self, batch) -> dict[str, str]:
        """读取本批次参与方的缺考口径（按登记顺序）。"""
        return {
            row["party_code"]: row["policy"]
            for row in self.store.list_batch_parties(batch["id"])
        }

    def _party_of_batch(self, batch, party: str) -> None:
        if party not in self._policies(batch):
            raise NotFoundError(f"方 {party} 不在本批次中")

    def _batch(self, batch_id: str):
        b = self.store.get_batch(batch_id)
        if b is None:
            raise NotFoundError(f"批次 {batch_id} 不存在")
        return b

    def _rater(self, rater_id: str):
        r = self.store.get_rater(rater_id)
        if r is None:
            raise NotFoundError(f"评分员 {rater_id} 不存在")
        return r

    def _enrolled(self, batch_id: str, student_id: str) -> None:
        rows = self.store.list_batch_students(batch_id)
        if not any(r["id"] == student_id for r in rows):
            raise NotFoundError(f"学生 {student_id} 不在批次 {batch_id} 中")
