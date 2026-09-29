"""moderation 服务端领域规则回归测试。

覆盖题目关键场景：
- 缺考口径分歧（零分/排除）在封存前被一致性检查发现并送入讨论；
- 迟到成绩、复评分、回避、批次重开均不覆盖早先记录（只追加）；
- 封存快照不可变；
- 评分员资格（含撤销的时间点语义）；
- 会签法定人数不足不能发布；
- 结果可沿每位评分员与采用规则复算，发布校验和可重验。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moderation_service.engine import ModerationService  # noqa: E402
from moderation_service.errors import (  # noqa: E402
    ConflictError, NotFoundError, ValidationError,
)
from moderation_service.server import build_server  # noqa: E402
from moderation_service.store import Store  # noqa: E402

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.t = 0

    def now(self) -> str:
        return (BASE + timedelta(seconds=self.t)).isoformat()

    def advance(self, seconds: int) -> None:
        self.t += seconds


class ModerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.svc = ModerationService(Store(":memory:"), self.clock.now)
        self._bootstrap()

    def _bootstrap(self) -> None:
        s = self.svc
        s.register_party("CN", "中方")
        s.register_party("FR", "法方")
        s.register_rubric("R-2026", "联合考核量表 v1", 0, 100)
        for rid, name, party, role in [
            ("cn-t", "王老师", "CN", "teacher"),
            ("fr-t", "Dupont", "FR", "teacher"),
            ("cn-c", "李协调员", "CN", "coordinator"),
            ("fr-c", "Martin 协调员", "FR", "coordinator"),
        ]:
            s.register_rater(rid, name, party, role)
            s.grant_qualification(rid, "R-2026")
        s.register_student("s1", "学生甲")
        s.register_student("s2", "学生乙")
        # 中方对缺考记零分，法方排除缺考——题面分歧口径。
        s.create_batch(
            "B1", "R-2026", {"CN": "zero", "FR": "exclude"},
            gap_threshold=10,
            grading_deadline=(BASE + timedelta(days=7)).isoformat(),
            min_signatures=2, signature_rule="both",
        )
        s.add_student("B1", "s1")
        s.add_student("B1", "s2")

    # ---- 主数据校验 -----------------------------------------------------

    def test_rubric_and_qualification(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.register_rubric("bad", "x", 100, 0)
        with self.assertRaises(NotFoundError):
            self.svc.register_rater("x", "x", "XX")
        # 未授权量表不能评分
        self.svc.register_rater("intern", "实习生", "CN")
        with self.assertRaises(ConflictError):
            self.svc.record_score("B1", "s1", "intern", 70)

    def test_score_out_of_scale_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.record_score("B1", "s1", "cn-t", 150)

    # ---- 缺考口径：题面核心事故 ----------------------------------------

    def test_absence_disagreement_blocks_and_rank_is_rule_based(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.record_score("B1", "s1", "fr-t", 95)  # 差 15 > 阈值 10
        # s2：仅中方登记缺考；法方尚未登记任何信息
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")

        result = s.run_consistency_check("B1")
        cats = {f["category"] for f in result["findings"]}
        self.assertIn("coverage", cats)   # 法方无成绩也无缺考声明
        self.assertIn("gap", cats)        # s1 双方分差超阈值
        with self.assertRaises(ConflictError):
            s.seal_calibration("B1")      # 阻断性差异禁止封存

        # 法方补登缺考（按法方口径排除）后，只剩告警类差异
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考，排除")
        result = s.run_consistency_check("B1")
        self.assertTrue(all(f["severity"] == "warning"
                            for f in result["findings"]))
        keys = {f["key"] for f in result["findings"]}
        self.assertIn("policy_divergence:s2", keys)

        seal = s.seal_calibration("B1")
        self.assertEqual(seal["window_n"], 1)

        results, _ = s.compute_results("B1")
        by_id = {r["student_id"]: r for r in results["students"]}
        # s1 = (80+95)/2；s2 中方零分、法方排除 => 只剩 [0] => 0
        self.assertEqual(by_id["s1"]["final_score"], 87.5)
        self.assertEqual(by_id["s2"]["final_score"], 0.0)
        self.assertEqual(by_id["s2"]["excluded_parties"], ["FR"])
        self.assertEqual(by_id["s1"]["rank"], 1)
        self.assertEqual(by_id["s2"]["rank"], 2)
        self.assertEqual(
            by_id["s2"]["parties"]["CN"]["status"], "absent_zero")
        self.assertEqual(
            by_id["s2"]["parties"]["FR"]["status"], "absent_excluded")

    def test_within_party_absence_contradiction_blocks(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.declare_absence("B1", "s1", "CN", "cn-c", "误报缺考")
        s.record_score("B1", "s1", "fr-t", 90)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        result = s.run_consistency_check("B1")
        self.assertIn("absence_contradiction:s1:CN",
                      {f["key"] for f in result["findings"]})
        with self.assertRaises(ConflictError):
            s.seal_calibration("B1")
        # 撤回缺考后矛盾解除
        s.retract_absence("B1", "s1", "CN", "cn-c", "学生正常参考")
        result = s.run_consistency_check("B1")
        self.assertNotIn("absence_contradiction:s1:CN",
                         {f["key"] for f in result["findings"]})

    # ---- 迟到成绩不覆盖早先记录 ----------------------------------------

    def test_late_score_never_overwrites(self) -> None:
        s = self.svc
        self.clock.advance(3600)
        s.record_score("B1", "s1", "cn-t", 80)  # 截止前
        self.clock.t = 9 * 86400                 # 截止后
        late = s.record_score(
            "B1", "s1", "cn-t", 55,
            as_of=(BASE + timedelta(days=9)).isoformat(),
            accept_late=True, reason="系统故障补传",
        )
        self.assertEqual(late["kind"], "late")
        trace = s.student_trace("B1", "s1")
        line = trace["by_rater"]["cn-t"]
        self.assertEqual(line["effective"]["value"], 80)  # 早先成绩保留
        self.assertEqual(line["history"][1]["kind"], "late")
        self.assertFalse(line["history"][1]["became_effective"])
        # 迟到成绩仍产生讨论项
        check = s.run_consistency_check("B1")
        self.assertIn(f"late:s1:cn-t", {f["key"] for f in check["findings"]})

        # 原本无成绩的评分员，迟到成绩可以生效
        late2 = s.record_score(
            "B1", "s2", "cn-t", 70,
            as_of=(BASE + timedelta(days=9)).isoformat(),
            accept_late=True)
        self.assertEqual(late2["kind"], "late")
        line2 = s.student_trace("B1", "s2")["by_rater"]["cn-t"]
        self.assertEqual(line2["effective"]["value"], 70)

    def test_late_without_flag_rejected(self) -> None:
        self.clock.t = 9 * 86400
        with self.assertRaises(ConflictError):
            self.svc.record_score(
                "B1", "s1", "cn-t", 55,
                as_of=(BASE + timedelta(days=9)).isoformat())

    # ---- 复评分不覆盖原始评分；封存快照不可变 --------------------------

    def test_rescore_keeps_original_and_seal_is_immutable(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.record_score("B1", "s1", "fr-t", 95)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        s.seal_calibration("B1")
        snapshot1 = s.list_seals("B1")[0]
        self.assertEqual(
            snapshot1["snapshot"]["results"]["students"][0]["final_score"],
            87.5)

        s.begin_review("B1")
        gap = next(d for d in s.list_discussions("B1")
                   if d["key"] == "score_gap:s1")
        rescore = s.record_rescore(
            "B1", "s1", "fr-t", 86, gap["id"],
            decided_by="fr-c", rationale="校准后采纳复评分")
        self.assertEqual(rescore["kind"], "rescore")
        trace = s.student_trace("B1", "s1")
        history = trace["by_rater"]["fr-t"]["history"]
        self.assertEqual([h["kind"] for h in history], ["score", "rescore"])
        self.assertEqual(history[0]["value"], 95)       # 原始记录仍在
        self.assertEqual(trace["by_rater"]["fr-t"]["effective"]["value"], 86)

        # 早先封存快照不随后续事件变化
        self.assertEqual(s.list_seals("B1")[0]["checksum"],
                         snapshot1["checksum"])
        results, _ = s.compute_results("B1")
        s1 = next(r for r in results["students"] if r["student_id"] == "s1")
        self.assertEqual(s1["final_score"], 83.0)

    # ---- 回避：排除效力但保留记录 --------------------------------------

    def test_recusal_excludes_rater_but_keeps_history(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.mark_recusal("B1", "s1", "cn-t", "利益冲突")
        trace = s.student_trace("B1", "s1")
        line = trace["by_rater"]["cn-t"]
        self.assertTrue(line["recused"])
        self.assertIsNone(line["effective"])
        self.assertEqual(line["history"][0]["value"], 80)  # 记录保留
        with self.assertRaises(ConflictError):
            s.record_score("B1", "s1", "cn-t", 70)  # 回避后不得再评

    # ---- 批次重开不覆盖早先记录 ----------------------------------------

    def test_reopen_starts_new_window_without_touching_seal(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.record_score("B1", "s1", "fr-t", 82)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        s.seal_calibration("B1")
        old = s.list_seals("B1")[0]
        reopened = s.reopen_batch("B1", "补录遗漏成绩")
        self.assertEqual(reopened["window_n"], 2)
        self.clock.advance(60)
        s.record_score("B1", "s2", "cn-t", 77)  # s2 实际参加缓考
        s.retract_absence("B1", "s2", "CN", "cn-c", "缓考到场")
        s.declare_absence("B1", "s2", "FR", "fr-c", "仍缺考")  # 重登以消除告警
        # 窗口 1 的封存纹丝不动
        self.assertEqual(s.list_seals("B1")[0], old)
        events = s.list_events("B1")
        self.assertEqual({e["window_n"] for e in events}, {1, 2})

    # ---- 资格撤销的时间点语义 ------------------------------------------

    def test_qualification_revocation_is_point_in_time(self) -> None:
        s = self.svc
        s.revoke_qualification("cn-t", "R-2026")
        with self.assertRaises(ConflictError):
            s.record_score("B1", "s1", "cn-t", 80)
        with self.assertRaises(ConflictError):
            s.revoke_qualification("cn-t", "R-2026")

    # ---- 会签：法定签署数 ----------------------------------------------

    def _drive_to_countersign(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.record_score("B1", "s1", "fr-t", 82)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        s.seal_calibration("B1")
        # 口径分歧告警须经讨论结案
        s.begin_review("B1")
        div = next(d for d in s.list_discussions("B1")
                   if d["key"] == "policy_divergence:s2")
        s.add_discussion_comment(div["id"], "cn-c", "双方确认按各自口径处理")
        s.acknowledge_discussion(div["id"], "cn-c", "委员会确认口径分歧")
        s.add_review_sample("B1", "s1", "随机抽样 10%")
        s.begin_countersign("B1")

    def test_publish_requires_quorum_and_is_verifiable(self) -> None:
        s = self.svc
        self._drive_to_countersign()
        with self.assertRaises(ConflictError):
            s.publish("B1")                      # 零签署
        s.sign("B1", "cn-c")
        with self.assertRaises(ConflictError):
            s.publish("B1")                      # 仅一方，rule=both
        with self.assertRaises(ConflictError):
            s.sign("B1", "cn-t")                 # 教师不能签署
        with self.assertRaises(ConflictError):
            s.sign("B1", "cn-c")                 # 不可重复签署
        s.sign("B1", "fr-c")
        pub = s.publish("B1")
        self.assertEqual(pub["results"]["students"][0]["rank"], 1)

        verification = s.verify_publication("B1")
        self.assertTrue(verification["match"])

        # 发布后冻结
        with self.assertRaises(ConflictError):
            s.reopen_batch("B1", "x")
        with self.assertRaises(ConflictError):
            s.record_score("B1", "s1", "cn-t", 90)

        got = s.get_publication("B1")
        self.assertEqual(got["rule"]["absence_policies"],
                         {"CN": "zero", "FR": "exclude"})

    def test_any_party_signature_rule(self) -> None:
        s = self.svc
        s.create_batch(
            "B2", "R-2026", {"CN": "zero", "FR": "exclude"},
            min_signatures=1, signature_rule="any")
        s.add_student("B2", "s1")
        s.record_score("B2", "s1", "cn-t", 70)
        s.record_score("B2", "s1", "fr-t", 72)
        s.seal_calibration("B2")
        s.begin_review("B2")
        s.begin_countersign("B2")
        s.sign("B2", "cn-c")
        pub = s.publish("B2")
        self.assertIn("checksum", pub)

    # ---- 讨论与状态机 ---------------------------------------------------

    def test_discussion_lifecycle(self) -> None:
        s = self.svc
        s.record_score("B1", "s1", "cn-t", 80)
        s.run_consistency_check("B1")  # s1 法方缺成绩 => error
        with self.assertRaises(ConflictError):
            s.seal_calibration("B1")
        s.record_score("B1", "s1", "fr-t", 82)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        s.run_consistency_check("B1")
        # 原阻断问题消失后自动关闭
        missing = next(d for d in s.list_discussions("B1")
                       if d["category"] == "coverage")
        self.assertEqual(missing["status"], "auto_closed")
        with self.assertRaises(ConflictError):
            s.acknowledge_discussion(missing["id"], "cn-c", "x")
        with self.assertRaises(NotFoundError):
            s.acknowledge_discussion(9999, "cn-t", "x")

    def test_state_machine_guards(self) -> None:
        s = self.svc
        with self.assertRaises(ConflictError):
            s.begin_review("B1")          # 还在评分
        s.record_score("B1", "s1", "cn-t", 80)
        s.record_score("B1", "s1", "fr-t", 82)
        s.declare_absence("B1", "s2", "CN", "cn-c", "缺考")
        s.declare_absence("B1", "s2", "FR", "fr-c", "缺考")
        s.seal_calibration("B1")
        with self.assertRaises(ConflictError):
            s.add_student("B1", "s1")     # 封存后不能加学生


class HttpSmokeTest(unittest.TestCase):
    """端到端 HTTP 冒烟：线程内起真实服务，走 urllib。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = build_server(":memory:", "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_full_flow_over_http(self) -> None:
        c = self._call
        c("POST", "/parties", {"code": "CN", "name": "中方"})
        c("POST", "/parties", {"code": "FR", "name": "法方"})
        c("POST", "/rubrics", {"id": "R1", "title": "v1",
                               "scale_min": 0, "scale_max": 100})
        c("POST", "/raters", {"id": "t1", "name": "王", "party": "CN"})
        c("POST", "/raters", {"id": "t2", "name": "Dupont", "party": "FR"})
        c("POST", "/raters", {"id": "c1", "name": "李", "party": "CN",
                              "role": "coordinator"})
        c("POST", "/raters", {"id": "c2", "name": "Martin", "party": "FR",
                              "role": "coordinator"})
        for rid in ("t1", "t2", "c1", "c2"):
            self.assertEqual(c("POST", f"/raters/{rid}/qualifications",
                               {"rubric_id": "R1"})[0], 200)
        c("POST", "/students", {"id": "s1", "name": "甲"})
        st, body = c("POST", "/batches", {
            "id": "B9", "rubric_id": "R1",
            "absence_policies": {"CN": "zero", "FR": "exclude"}})
        self.assertEqual(st, 200)
        c("POST", "/batches/B9/students", {"student_id": "s1"})
        c("POST", "/batches/B9/scores",
          {"student_id": "s1", "rater_id": "t1", "value": 80})
        c("POST", "/batches/B9/scores",
          {"student_id": "s1", "rater_id": "t2", "value": 82})
        st, seal = c("POST", "/batches/B9/seal")
        self.assertEqual(st, 200)
        c("POST", "/batches/B9/review")
        c("POST", "/batches/B9/countersign")
        c("POST", "/batches/B9/signatures", {"signer_id": "c1"})
        c("POST", "/batches/B9/signatures", {"signer_id": "c2"})
        st, pub = c("POST", "/batches/B9/publish")
        self.assertEqual(st, 200)
        st, verify = c("POST", "/batches/B9/verify")
        self.assertTrue(verify["match"])

        st, trace = c("GET", "/batches/B9/students/s1/trace")
        self.assertEqual(st, 200)
        self.assertEqual(trace["current_result"]["final_score"], 81.0)

        st, err = c("GET", "/nope")
        self.assertEqual(st, 404)
        self.assertIn("error", err)


if __name__ == "__main__":
    unittest.main()
