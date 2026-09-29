"""SQLite 存储层。

设计要点：
- 成绩、回避、缺考标记、缺考口径决定、签署等均为**只追加**记录，永不更新/删除；
- 批次、评分员资格等少量"当前状态"由追加记录派生或在变更时同步写入事件流；
- events 表是全局审计流，seq 单调递增，所有状态变更在同一事务内落事件；
- scores/events 等核心表通过触发器拒绝 UPDATE/DELETE，从结构上保证"复评分、回避、
  迟到成绩与批次重开不能覆盖早先记录"。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rubric_versions (
    rubric_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    created_party TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    PRIMARY KEY (rubric_id, version)
);

CREATE TABLE IF NOT EXISTS graders (
    grader_id TEXT PRIMARY KEY,
    party_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS grader_qualifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grader_id TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    version TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('granted', 'revoked')),
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rubric_id TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    absence_policy TEXT NOT NULL CHECK (absence_policy IN ('zero', 'exclude')),
    required_signatures INTEGER NOT NULL,
    min_graders INTEGER NOT NULL,
    deadline TEXT,
    status TEXT NOT NULL CHECK (status IN ('open', 'sealed', 'published')),
    reopen_count INTEGER NOT NULL DEFAULT 0,
    created_seq INTEGER NOT NULL,
    sealed_seq INTEGER,
    published_seq INTEGER
);

CREATE TABLE IF NOT EXISTS batch_policy_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    absence_policy TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT,
    recorded_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS enrollments (
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL,
    PRIMARY KEY (batch_id, student_id)
);

-- 事件流：只追加
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 原始评分：(批次, 学生, 评分员, 轮次, 维度) 唯一且不可变；复评必须走新一轮次
CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    grader_id TEXT NOT NULL,
    round INTEGER NOT NULL CHECK (round >= 1),
    dimension TEXT NOT NULL,
    score REAL NOT NULL,
    rubric_id TEXT NOT NULL,
    rubric_version TEXT NOT NULL,
    is_late INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL,
    UNIQUE (batch_id, student_id, grader_id, round, dimension)
);

-- 回避记录：只追加；同一(批次,学生,评分员)存在回避后不允许再录成绩
CREATE TABLE IF NOT EXISTS recusals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    grader_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL,
    UNIQUE (batch_id, student_id, grader_id, recorded_seq)
);

-- 各联考方对缺考的原始口径标记：只追加，当前值取最新一条
CREATE TABLE IF NOT EXISTS absence_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    treatment TEXT NOT NULL CHECK (treatment IN ('present', 'zero', 'exclude')),
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL
);

-- 委员会经讨论后采用的缺考口径决定：只追加，当前值取最新一条
CREATE TABLE IF NOT EXISTS enrollment_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    treatment TEXT NOT NULL CHECK (treatment IN ('present', 'zero', 'exclude')),
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS consistency_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    status TEXT NOT NULL,
    discrepancies_json TEXT NOT NULL,
    ran_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    discrepancy_key TEXT,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    decision TEXT,
    created_seq INTEGER NOT NULL,
    resolved_seq INTEGER
);

CREATE TABLE IF NOT EXISTS review_discussions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL,
    party_id TEXT NOT NULL,
    comment TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL
);

-- 签署动作流：sign / unsign 只追加；批次重开会派生清空
CREATE TABLE IF NOT EXISTS signature_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    party_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('sign', 'unsign')),
    actor TEXT NOT NULL,
    recorded_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS published_results (
    batch_id TEXT PRIMARY KEY,
    report_json TEXT NOT NULL,
    published_seq INTEGER NOT NULL
);
"""

# 这些表只追加：通过触发器在数据库层拒绝任何改写。
_IMMUTABLE_TABLES = [
    "events",
    "scores",
    "recusals",
    "absence_marks",
    "enrollment_decisions",
    "rubric_versions",
    "published_results",
    "grader_qualifications",
    "signature_events",
    "review_discussions",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """薄薄一层 SQLite 访问；事务与领域规则由 service 层负责。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # HTTP 层为多线程 + 全局锁，故允许连接跨线程使用；所有访问串行化。
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(SCHEMA)
        for table in _IMMUTABLE_TABLES:
            cur.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update "
                f"BEFORE UPDATE ON {table} BEGIN "
                f"SELECT RAISE(FAIL, '{table} 是只追加不可变记录'); END"
            )
            cur.execute(
                f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete "
                f"BEFORE DELETE ON {table} BEGIN "
                f"SELECT RAISE(FAIL, '{table} 是只追加不可变记录'); END"
            )
        self.conn.commit()

    # -- 基础 -----------------------------------------------------------------

    def append_event(self, event_type: str, payload: dict, actor: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (event_type, payload_json, actor, created_at) "
            "VALUES (?, ?, ?, ?)",
            (event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor, _utcnow()),
        )
        return int(cur.lastrowid)

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # -- 读取辅助 -------------------------------------------------------------

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()

    def get_rubric(self, rubric_id: str, version: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rubric_versions WHERE rubric_id = ? AND version = ?",
            (rubric_id, version),
        ).fetchone()

    def get_grader(self, grader_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM graders WHERE grader_id = ?", (grader_id,)
        ).fetchone()

    def get_party(self, party_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM parties WHERE party_id = ?", (party_id,)
        ).fetchone()

    def get_student(self, student_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM students WHERE student_id = ?", (student_id,)
        ).fetchone()

    def is_enrolled(self, batch_id: str, student_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM enrollments WHERE batch_id = ? AND student_id = ?",
            (batch_id, student_id),
        ).fetchone() is not None

    def list_enrolled_students(self, batch_id: str) -> list[str]:
        return [
            row["student_id"]
            for row in self.conn.execute(
                "SELECT student_id FROM enrollments WHERE batch_id = ? ORDER BY student_id",
                (batch_id,),
            )
        ]

    def grader_is_qualified(self, grader_id: str, rubric_id: str, version: str) -> bool:
        row = self.conn.execute(
            "SELECT action FROM grader_qualifications "
            "WHERE grader_id = ? AND rubric_id = ? AND version = ? "
            "ORDER BY recorded_seq DESC, id DESC LIMIT 1",
            (grader_id, rubric_id, version),
        ).fetchone()
        return row is not None and row["action"] == "granted"

    def has_recusal(self, batch_id: str, student_id: str, grader_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM recusals WHERE batch_id = ? AND student_id = ? AND grader_id = ? LIMIT 1",
            (batch_id, student_id, grader_id),
        ).fetchone() is not None

    def list_recusals(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM recusals WHERE batch_id = ? ORDER BY recorded_seq",
            (batch_id,),
        ).fetchall()

    def latest_round(self, batch_id: str, student_id: str, grader_id: str, dimension: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(round) AS m FROM scores "
            "WHERE batch_id=? AND student_id=? AND grader_id=? AND dimension=?",
            (batch_id, student_id, grader_id, dimension),
        ).fetchone()
        return int(row["m"] or 0)

    def list_scores(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scores WHERE batch_id = ? "
            "ORDER BY student_id, grader_id, round, dimension",
            (batch_id,),
        ).fetchall()

    def current_marks(self, batch_id: str) -> dict[tuple[str, str], sqlite3.Row]:
        """各 (学生, 联考方) 最新一条缺考口径标记。"""
        rows = self.conn.execute(
            "SELECT m.* FROM absence_marks m JOIN ("
            "  SELECT student_id, party_id, MAX(recorded_seq) AS max_seq "
            "  FROM absence_marks WHERE batch_id = ? GROUP BY student_id, party_id"
            ") t ON m.student_id = t.student_id AND m.party_id = t.party_id "
            "AND m.recorded_seq = t.max_seq WHERE m.batch_id = ?",
            (batch_id, batch_id),
        ).fetchall()
        return {(r["student_id"], r["party_id"]): r for r in rows}

    def current_decision(self, batch_id: str, student_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM enrollment_decisions WHERE batch_id = ? AND student_id = ? "
            "ORDER BY recorded_seq DESC, id DESC LIMIT 1",
            (batch_id, student_id),
        ).fetchone()

    def current_absence_policy(self, batch: sqlite3.Row) -> str:
        row = self.conn.execute(
            "SELECT absence_policy FROM batch_policy_changes WHERE batch_id = ? "
            "ORDER BY recorded_seq DESC, id DESC LIMIT 1",
            (batch["batch_id"],),
        ).fetchone()
        return row["absence_policy"] if row else batch["absence_policy"]

    def list_open_samples(self, batch_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM review_samples WHERE batch_id = ? AND status = 'open' ORDER BY sample_id",
            (batch_id,),
        ).fetchall()

    def get_sample(self, sample_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM review_samples WHERE sample_id = ?", (sample_id,)
        ).fetchone()

    def list_discussions(self, sample_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM review_discussions WHERE sample_id = ? ORDER BY recorded_seq",
            (sample_id,),
        ).fetchall()

    def resolved_discrepancy_keys(self, batch_id: str) -> set[str]:
        return {
            r["discrepancy_key"]
            for r in self.conn.execute(
                "SELECT discrepancy_key FROM review_samples "
                "WHERE batch_id = ? AND status = 'resolved' AND discrepancy_key IS NOT NULL",
                (batch_id,),
            ).fetchall()
        }

    def current_signatures(self, batch_id: str) -> dict[str, str]:
        """派生当前签署状态：重开之后的签署全部失效。"""
        reopened = self.conn.execute(
            "SELECT MAX(seq) AS m FROM events "
            "WHERE event_type = 'batch.reopened' AND json_extract(payload_json, '$.batch_id') = ?",
            (batch_id,),
        ).fetchone()["m"]
        reopened = reopened or 0
        result: dict[str, str] = {}
        rows = self.conn.execute(
            "SELECT party_id, action, recorded_seq FROM signature_events "
            "WHERE batch_id = ? AND recorded_seq > ? ORDER BY recorded_seq, id",
            (batch_id, reopened),
        ).fetchall()
        for row in rows:
            result[row["party_id"]] = row["action"]
        return {p: a for p, a in result.items() if a == "sign"}

    def list_published(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM published_results WHERE batch_id = ?", (batch_id,)
        ).fetchone()

    def list_events(self, batch_id: str | None = None) -> list[sqlite3.Row]:
        if batch_id is None:
            return self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return self.conn.execute(
            "SELECT * FROM events WHERE json_extract(payload_json, '$.batch_id') = ? ORDER BY seq",
            (batch_id,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
