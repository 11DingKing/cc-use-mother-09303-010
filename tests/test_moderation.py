"""端到端领域规则测试。"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from moderation import ModerationService, Store, ConflictError, NotFoundError, ValidationError
from moderation.api import build_server

DIMENSIONS = [
    {"key": "content", "title": "内容", "min": 0, "max": 10},
    {"key": "delivery", "title": "表达", "min": 0, "max": 10},
]


def bootstrap(db=":memory:", policy="exclude", required=2, min_graders=2):
    """搭建：两方联考委、两名学生、量表 v1、两名评分员（各持资格）、一个批次。"""
    store = Store(db)
    svc = ModerationService(store)
    svc.register_party("party-a", "甲方大学", "coord")
    svc.register_party("party-b", "乙方大学", "coord")
    svc.register_student("s1", "学生甲", "coord")
    svc.register_student("s2", "学生乙", "coord")
    svc.create_rubric_version("rubric-x", "v1", "联合答辩量表", DIMENSIONS, "party-a", "coord")
    svc.register_grader("g1", "party-a", "甲校王老师", "coord")
    svc.register_grader("g2", "party-b", "乙校李老师", "coord")
    svc.set_qualification("g1", "rubric-x", "v1", True, "coord")
    svc.set_qualification("g2", "rubric-x", "v1", True, "coord")
    svc.create_batch(
        "b1", "2026 春季联合答辩", "rubric-x", "v1", policy, required, min_graders,
        "coord", student_ids=["s1", "s2"],
    )
    return store, svc


def score_both_graders(svc, student, c1, d1, c2, d2, actor="coord"):
    svc.record_score("b1", student, "g1", "content", c1, actor)
    svc.record_score("b1", student, "g1", "delivery", d1, actor)
    svc.record_score("b1", student, "g2", "content", c2, actor)
    svc.record_score("b1", student, "g2", "delivery", d2, actor)


class HappyPathTest(unittest.TestCase):
    def test_full_flow_and_recompute(self) -> None:
        store, svc = bootstrap()
        # s1: g1 总 18，g2 总 16 → 17；s2: g1 总 12，g2 总 14 → 13
        score_both_graders(svc, "s1", 9, 9, 8, 8)
        score_both_graders(svc, "s2", 6, 6, 7, 7)

        report = svc.run_consistency_check("b1", "coord")
        self.assertEqual(report["fresh"], 0)

        seal = svc.seal_batch("b1", "coord")
        self.assertEqual(seal["status"], "sealed")

        # 签署不足不能发布
        svc.sign("b1", "party-a", "party-a-dean")
        with self.assertRaises(ConflictError):
            svc.publish("b1", "coord")
        svc.sign("b1", "party-b", "party-b-dean")

        out = svc.publish("b1", "coord")
        self.assertEqual(out["status"], "published")
        students = {s["student_id"]: s for s in out["report"]["students"]}
        self.assertEqual(students["s1"]["final_score"], 17.0)
        self.assertEqual(students["s1"]["rank"], 1)
        self.assertEqual(students["s2"]["final_score"], 13.0)
        self.assertEqual(students["s2"]["rank"], 2)
        # 溯源链完整：每个分数都能沿评分员复算
        rules = {step["rule"] for s in out["report"]["students"] for step in s["trace"]}
        self.assertIn("grader_total", rules)
        self.assertIn("mean_of_graders", rules)
        # 发布后只读
        with self.assertRaises(ConflictError):
            svc.record_score("b1", "s1", "g1", "content", 10, "coord")
        with self.assertRaises(ConflictError):
            svc.reopen_batch("b1", "coord", "published is final")
        # 已发布报告可独立取出
        self.assertEqual(svc.get_published_report("b1")["batch_id"], "b1")
        store.close()


class QualificationTest(unittest.TestCase):
    def test_unqualified_and_revoked_rejected(self) -> None:
        store, svc = bootstrap()
        svc.register_grader("g3", "party-a", "实习助教", "coord")
        # g3 未获资格，录入被拒
        with self.assertRaises(ConflictError):
            svc.record_score("b1", "s1", "g3", "content", 9, "coord")
        # g1 已授予资格可以录；撤销后新录入被拒
        svc.record_score("b1", "s1", "g1", "content", 9, "coord")
        svc.set_qualification("g1", "rubric-x", "v1", False, "coord")
        with self.assertRaises(ConflictError):
            svc.record_score("b1", "s1", "g1", "delivery", 9, "coord")
        # 早先记录仍在
        rows = store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()
        self.assertEqual(rows["c"], 1)
        store.close()

    def test_score_out_of_range_rejected(self) -> None:
        store, svc = bootstrap()
        with self.assertRaises(ValidationError):
            svc.record_score("b1", "s1", "g1", "content", 99, "coord")
        with self.assertRaises(ValidationError):
            svc.record_score("b1", "s1", "g1", "unknown", 5, "coord")
        store.close()


class RescoreImmutabilityTest(unittest.TestCase):
    def test_rerescore_goes_new_round_never_overwrites(self) -> None:
        store, svc = bootstrap()
        id1 = svc.record_score("b1", "s1", "g1", "content", 6, "coord")
        id2 = svc.record_score("b1", "s1", "g1", "content", 9, "coord")
        self.assertNotEqual(id1, id2)
        rows = store.conn.execute(
            "SELECT round, score FROM scores WHERE student_id='s1' ORDER BY round"
        ).fetchall()
        self.assertEqual([(r["round"], r["score"]) for r in rows], [(1, 6.0), (2, 9.0)])

        # 复算采用最大轮次，且 trace 记录被取代的早先记录
        report = svc.compute_report("b1")
        s1 = next(s for s in report["students"] if s["student_id"] == "s1")
        step = next(t for t in s1["trace"] if t["rule"] == "latest_round_wins")
        self.assertEqual(step["adopted_round"], 2)
        self.assertEqual(step["adopted_value"], 9.0)
        self.assertEqual(step["superseded"], [{"score_id": id1, "round": 1, "value": 6.0}])

        # 数据库层触发器拒绝改写历史
        with self.assertRaises(sqlite3.Error):
            store.conn.execute("UPDATE scores SET score = 0 WHERE id = ?", (id1,))
        with self.assertRaises(sqlite3.Error):
            store.conn.execute("DELETE FROM scores WHERE id = ?", (id1,))
        store.close()


class RecusalTest(unittest.TestCase):
    def test_recusal_blocks_and_excludes_from_aggregation(self) -> None:
        store, svc = bootstrap()
        # g1 先录了一条，随后回避
        svc.record_score("b1", "s1", "g1", "content", 9, "coord")
        svc.record_recusal("b1", "s1", "g1", "与学生有亲属关系", "coord")
        # 回避后再录成绩一律拒收
        with self.assertRaises(ConflictError):
            svc.record_score("b1", "s1", "g1", "delivery", 9, "coord")
        # 回避不可重复登记
        with self.assertRaises(ConflictError):
            svc.record_recusal("b1", "s1", "g1", "again", "coord")
        # 一致性检查巡检到 D7（回避者名下仍有历史成绩）
        report = svc.run_consistency_check("b1", "coord")
        codes = {d["code"] for d in report["discrepancies"]}
        self.assertIn("D7", codes)
        sample = next(s for s in svc.list_review_samples("b1")
                      if s["reason"].startswith("D7"))
        svc.discuss(sample["sample_id"], "party-a", "该成绩作废，不进入复算", "coord")
        svc.resolve_sample(sample["sample_id"], "排除 g1 全部历史成绩", "coord")
        # g1 回避后评分员不足，委员会安排甲校替补评分员 g3
        svc.register_grader("g3", "party-a", "甲校赵老师", "coord")
        svc.set_qualification("g3", "rubric-x", "v1", True, "coord")
        # g2 补齐 s1
        svc.record_score("b1", "s1", "g2", "content", 8, "coord")
        svc.record_score("b1", "s1", "g2", "delivery", 8, "coord")
        svc.record_score("b1", "s1", "g3", "content", 8, "coord")
        svc.record_score("b1", "s1", "g3", "delivery", 8, "coord")
        score_both_graders(svc, "s2", 7, 7, 7, 7)
        report2 = svc.run_consistency_check("b1", "coord")
        self.assertEqual(report2["fresh"], 0)
        # 复算 s1 采用 g2/g3（回避者 g1 被排除）
        calc = svc.compute_report("b1")
        s1 = next(s for s in calc["students"] if s["student_id"] == "s1")
        self.assertEqual(s1["final_score"], 16.0)
        self.assertEqual(set(s1["grader_totals"]), {"g2", "g3"})
        store.close()


class AbsencePolicyTest(unittest.TestCase):
    def test_divergent_absence_treatment_must_be_discussed(self) -> None:
        store, svc = bootstrap(policy="zero")
        score_both_graders(svc, "s1", 8, 8, 8, 8)
        # s2 缺考：甲方记零分，乙方排除 —— 直接合并会造成排名偏差
        svc.mark_absence("b1", "s2", "party-a", "zero", "party-a")
        svc.mark_absence("b1", "s2", "party-b", "exclude", "party-b")

        report = svc.run_consistency_check("b1", "coord")
        d1 = [d for d in report["discrepancies"] if d["code"] == "D1"]
        self.assertEqual(len(d1), 1)
        # 有未决议差异，封存被拒
        with self.assertRaises(ConflictError):
            svc.seal_batch("b1", "coord")

        sample = next(s for s in svc.list_review_samples("b1")
                      if s["reason"].startswith("D1"))
        svc.discuss(sample["sample_id"], "party-a", "该生有进场记录，建议计零分", "party-a")
        svc.discuss(sample["sample_id"], "party-b", "我方系统漏登，认可计零分", "party-b")
        svc.resolve_absence("b1", "s2", "zero", "讨论后按计零分处理", "coord")
        svc.resolve_sample(sample["sample_id"], "采用 zero", "coord")
        self.assertEqual(svc.run_consistency_check("b1", "coord")["fresh"], 0)

        svc.seal_batch("b1", "coord")
        svc.sign("b1", "party-a", "dean-a")
        svc.sign("b1", "party-b", "dean-b")
        out = svc.publish("b1", "coord")
        students = {s["student_id"]: s for s in out["report"]["students"]}
        self.assertEqual(students["s2"]["treatment"], "zero")
        self.assertEqual(students["s2"]["final_score"], 0.0)
        self.assertEqual(students["s2"]["rank"], 2)
        self.assertEqual(students["s1"]["rank"], 1)
        store.close()

    def test_exclude_policy_keeps_student_out_of_ranking(self) -> None:
        store, svc = bootstrap(policy="exclude")
        score_both_graders(svc, "s1", 5, 5, 5, 5)
        svc.mark_absence("b1", "s2", "party-a", "exclude", "party-a")
        svc.mark_absence("b1", "s2", "party-b", "exclude", "party-b")
        self.assertEqual(svc.run_consistency_check("b1", "coord")["fresh"], 0)
        svc.seal_batch("b1", "coord")
        svc.sign("b1", "party-a", "dean-a")
        svc.sign("b1", "party-b", "dean-b")
        out = svc.publish("b1", "coord")
        s2 = next(s for s in out["report"]["students"] if s["student_id"] == "s2")
        self.assertEqual(s2["treatment"], "exclude")
        self.assertIsNone(s2["final_score"])
        self.assertIsNone(s2["rank"])
        store.close()


class LateScoreAndReopenTest(unittest.TestCase):
    def test_late_score_reopens_and_invalidates_signatures(self) -> None:
        store, svc = bootstrap()
        score_both_graders(svc, "s1", 8, 8, 8, 8)
        score_both_graders(svc, "s2", 5, 5, 5, 5)
        svc.seal_batch("b1", "coord")
        svc.sign("b1", "party-a", "dean-a")
        svc.sign("b1", "party-b", "dean-b")
        self.assertEqual(svc.batch_status("b1")["signature_count"], 2)

        # 封存后迟到的复议成绩到达：自动重开，早先封存与签署记录保留但失效
        svc.record_score("b1", "s2", "g1", "content", 9, "coord", is_late=True)
        status = svc.batch_status("b1")
        self.assertEqual(status["status"], "open")
        self.assertEqual(status["reopen_count"], 1)
        self.assertEqual(status["signature_count"], 0)
        with self.assertRaises(ConflictError):
            svc.publish("b1", "coord")  # 未重新封存

        # 补齐 g1 delivery 与 g2（g1 两个维度都需要采用值；s2 原 g1 content 5 被 9 取代）
        svc.record_score("b1", "s2", "g1", "delivery", 9, "coord")
        svc.run_consistency_check("b1", "coord")
        svc.seal_batch("b1", "coord")
        svc.sign("b1", "party-a", "dean-a")
        svc.sign("b1", "party-b", "dean-b")
        out = svc.publish("b1", "coord")
        s2 = next(s for s in out["report"]["students"] if s["student_id"] == "s2")
        # g1 总 18（采用第二轮 content=9），g2 总 10 → 14；s1 为 16，故 s2 排名第 2
        self.assertEqual(s2["final_score"], 14.0)
        self.assertEqual(s2["rank"], 2)
        # 历史痕迹：两轮 content 都在事件流里
        evs = store.conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type='score.recorded'"
        ).fetchone()["c"]
        self.assertGreaterEqual(evs, 9)
        store.close()

    def test_manual_reopen_keeps_history(self) -> None:
        store, svc = bootstrap()
        score_both_graders(svc, "s1", 8, 8, 8, 8)
        score_both_graders(svc, "s2", 5, 5, 5, 5)
        svc.seal_batch("b1", "coord")
        svc.sign("b1", "party-a", "dean-a")
        svc.reopen_batch("b1", "coord", "委员会抽查要求复核")
        self.assertEqual(svc.batch_status("b1")["status"], "open")
        self.assertEqual(svc.batch_status("b1")["signature_count"], 0)
        # 早先封存事件仍可审计
        sealed = store.conn.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type='batch.sealed'"
        ).fetchone()["c"]
        self.assertEqual(sealed, 1)
        store.close()


class DiscrepancyTest(unittest.TestCase):
    def test_score_spread_flagged_and_resolved(self) -> None:
        store, svc = bootstrap()
        # content: g1=9, g2=2，极差 7 > 阈值 4
        svc.record_score("b1", "s1", "g1", "content", 9, "coord")
        svc.record_score("b1", "s1", "g1", "delivery", 8, "coord")
        svc.record_score("b1", "s1", "g2", "content", 2, "coord")
        svc.record_score("b1", "s1", "g2", "delivery", 8, "coord")
        score_both_graders(svc, "s2", 7, 7, 7, 7)
        report = svc.run_consistency_check("b1", "coord")
        self.assertIn("D5", {d["code"] for d in report["discrepancies"]})
        sample = next(s for s in svc.list_review_samples("b1") if s["reason"].startswith("D5"))
        svc.discuss(sample["sample_id"], "party-a", "g2 误看了题号", "party-a")
        svc.resolve_sample(sample["sample_id"], "保留原分，已在复评轮次更正", "coord")
        # 复评进入新轮次而非覆盖
        svc.record_score("b1", "s1", "g2", "content", 8, "coord")
        self.assertEqual(svc.run_consistency_check("b1", "coord")["fresh"], 0)
        store.close()


class PolicyChangeTest(unittest.TestCase):
    def test_policy_change_is_appended(self) -> None:
        store, svc = bootstrap(policy="exclude")
        svc.change_absence_policy("b1", "zero", "coord", "委员会年会上修订口径")
        self.assertEqual(svc.batch_status("b1")["absence_policy"], "zero")
        rows = store.conn.execute(
            "SELECT COUNT(*) AS c FROM batch_policy_changes"
        ).fetchone()["c"]
        self.assertEqual(rows, 1)
        with self.assertRaises(ValidationError):
            svc.change_absence_policy("b1", "bogus", "coord", "x")
        store.close()


class ApiTest(unittest.TestCase):
    def test_http_roundtrip(self) -> None:
        httpd = build_server(":memory:", "127.0.0.1", 0)
        port = httpd.server_address[1]
        import threading

        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            def post(path: str, body: dict):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            status, body = post("/parties", {"party_id": "pa", "name": "甲方", "actor": "coord"})
            self.assertEqual(status, 201)
            status, body = post("/batches/missing/status", {})
            # 该路径是 GET；POST 应得到 404（无路由）
            self.assertEqual(status, 404)

            # GET 404 形状
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/batches/x/status")
            self.assertEqual(cm.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            httpd.store.close()


if __name__ == "__main__":
    unittest.main()
