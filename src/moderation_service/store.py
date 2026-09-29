"""SQLite 存储层。

设计要点：

- 评分域（``score_events``）严格只追加：任何修改都以新事件表达
  （缺考撤回、复评分、重开窗口后的新评分），引擎在读取时做归约。
- ``batches`` 表只保存流程状态投影（当前状态、当前窗口）；可复算的结果
  一律不持久化为可变列，发布时整包冻结进 ``publications``。
- 时间戳由引擎注入（ISO-8601 字符串），存储层不调用时钟，便于确定性测试。
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE parties (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE rubric_versions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    scale_min   REAL NOT NULL,
    scale_max   REAL NOT NULL,
    checksum    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE raters (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    party       TEXT NOT NULL REFERENCES parties(code),
    role        TEXT NOT NULL CHECK (role IN ('teacher', 'coordinator')),
    created_at  TEXT NOT NULL
);

CREATE TABLE rater_qualifications (
    rater_id    TEXT NOT NULL REFERENCES raters(id),
    rubric_id   TEXT NOT NULL REFERENCES rubric_versions(id),
    granted_at  TEXT NOT NULL,
    revoked_at  TEXT,
    PRIMARY KEY (rater_id, rubric_id)
);

CREATE TABLE students (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE batches (
    id                  TEXT PRIMARY KEY,
    rubric_id           TEXT NOT NULL REFERENCES rubric_versions(id),
    status              TEXT NOT NULL CHECK (status IN
                        ('grading', 'calibration', 'review',
                         'countersign', 'published')),
    current_window      INTEGER NOT NULL DEFAULT 1,
    gap_threshold       REAL NOT NULL,
    grading_deadline    TEXT,
    min_signatures      INTEGER NOT NULL,
    signature_rule      TEXT NOT NULL CHECK (signature_rule IN ('both', 'any')),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE batch_students (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    student_id  TEXT NOT NULL REFERENCES students(id),
    added_at    TEXT NOT NULL,
    PRIMARY KEY (batch_id, student_id)
);

CREATE TABLE batch_parties (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    party_code  TEXT NOT NULL REFERENCES parties(code),
    policy      TEXT NOT NULL CHECK (policy IN ('zero', 'exclude')),
    ord         INTEGER NOT NULL,
    PRIMARY KEY (batch_id, party_code)
);

CREATE TABLE windows (
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    window_n    INTEGER NOT NULL,
    opened_at   TEXT NOT NULL,
    reason      TEXT,
    closed_at   TEXT,
    PRIMARY KEY (batch_id, window_n)
);

-- 只追加评分事件流。永远不 UPDATE / DELETE。
CREATE TABLE score_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL REFERENCES batches(id),
    window_n     INTEGER NOT NULL,
    student_id   TEXT NOT NULL,
    rater_id     TEXT NOT NULL,
    party        TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN
                 ('score', 'late', 'absent', 'absence_retraction',
                  'recusal', 'rescore')),
    value        REAL,
    recorded_at  TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    reason       TEXT,
    discussion_id INTEGER
);

CREATE TABLE seals (
    batch_id    TEXT NOT NULL,
    window_n    INTEGER NOT NULL,
    sealed_at   TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    PRIMARY KEY (batch_id, window_n)
);

CREATE TABLE discussions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL,
    window_n    INTEGER NOT NULL,
    key         TEXT NOT NULL,
    category    TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('error', 'warning')),
    student_id  TEXT,
    detail_json TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                ('open', 'acknowledged', 'auto_closed')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (batch_id, window_n, key)
);

CREATE TABLE discussion_comments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    discussion_id  INTEGER NOT NULL REFERENCES discussions(id),
    author         TEXT NOT NULL,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE review_resolutions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    window_n      INTEGER NOT NULL,
    discussion_id INTEGER NOT NULL REFERENCES discussions(id),
    student_id    TEXT NOT NULL,
    rater_id      TEXT,
    decision      TEXT NOT NULL CHECK (decision IN
                  ('keep_original', 'adopt_rescore', 'acknowledged')),
    rescore_seq   INTEGER REFERENCES score_events(seq),
    decided_by    TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    decided_at    TEXT NOT NULL
);

CREATE TABLE review_samples (
    batch_id    TEXT NOT NULL,
    window_n    INTEGER NOT NULL,
    student_id  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, window_n, student_id)
);

CREATE TABLE check_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    window_n      INTEGER NOT NULL,
    ran_at        TEXT NOT NULL,
    findings_json TEXT NOT NULL,
    open_count    INTEGER NOT NULL
);

CREATE TABLE signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES batches(id),
    signer_id   TEXT NOT NULL REFERENCES raters(id),
    party       TEXT NOT NULL,
    signed_at   TEXT NOT NULL,
    UNIQUE (batch_id, signer_id)
);

CREATE TABLE publications (
    batch_id     TEXT PRIMARY KEY REFERENCES batches(id),
    published_at TEXT NOT NULL,
    rule_json    TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    checksum     TEXT NOT NULL
);
"""


class Store:
    """封装全部 SQL；引擎不直接写 SQL。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- 基础主数据 -----------------------------------------------------

    def create_party(self, code: str, name: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO parties(code, name, created_at) VALUES (?,?,?)",
            (code, name, now),
        )
        self.conn.commit()

    def get_party(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM parties WHERE code=?", (code,)
        ).fetchone()

    def create_rubric(
        self, id_: str, title: str, scale_min: float, scale_max: float,
        checksum: str, now: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO rubric_versions(id, title, scale_min, scale_max,"
            " checksum, created_at) VALUES (?,?,?,?,?,?)",
            (id_, title, scale_min, scale_max, checksum, now),
        )
        self.conn.commit()

    def get_rubric(self, id_: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rubric_versions WHERE id=?", (id_,)
        ).fetchone()

    def create_rater(
        self, id_: str, name: str, party: str, role: str, now: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO raters(id, name, party, role, created_at)"
            " VALUES (?,?,?,?,?)",
            (id_, name, party, role, now),
        )
        self.conn.commit()

    def get_rater(self, id_: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM raters WHERE id=?", (id_,)
        ).fetchone()

    def grant_qualification(
        self, rater_id: str, rubric_id: str, now: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO rater_qualifications(rater_id, rubric_id, granted_at)"
            " VALUES (?,?,?)",
            (rater_id, rubric_id, now),
        )
        self.conn.commit()

    def revoke_qualification(
        self, rater_id: str, rubric_id: str, now: str,
    ) -> None:
        self.conn.execute(
            "UPDATE rater_qualifications SET revoked_at=? "
            "WHERE rater_id=? AND rubric_id=? AND revoked_at IS NULL",
            (now, rater_id, rubric_id),
        )
        self.conn.commit()

    def qualification_active(
        self, rater_id: str, rubric_id: str, at: str,
    ) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM rater_qualifications WHERE rater_id=? AND"
            " rubric_id=? AND granted_at<=? AND (revoked_at IS NULL"
            " OR revoked_at>?)",
            (rater_id, rubric_id, at, at),
        ).fetchone()
        return row is not None

    def create_student(self, id_: str, name: str, now: str) -> None:
        self.conn.execute(
            "INSERT INTO students(id, name, created_at) VALUES (?,?,?)",
            (id_, name, now),
        )
        self.conn.commit()

    def get_student(self, id_: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM students WHERE id=?", (id_,)
        ).fetchone()

    # ---- 批次与窗口 -----------------------------------------------------

    def create_batch(self, b: dict[str, Any], now: str) -> None:
        self.conn.execute(
            "INSERT INTO batches(id, rubric_id, status, current_window,"
            " gap_threshold, grading_deadline, min_signatures,"
            " signature_rule, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (b["id"], b["rubric_id"], "grading", 1,
             b["gap_threshold"], b.get("grading_deadline"),
             b["min_signatures"], b["signature_rule"], now, now),
        )
        for ord_, (code, policy) in enumerate(b["parties"], start=1):
            self.conn.execute(
                "INSERT INTO batch_parties(batch_id, party_code, policy, ord)"
                " VALUES (?,?,?,?)",
                (b["id"], code, policy, ord_),
            )
        self.conn.execute(
            "INSERT INTO windows(batch_id, window_n, opened_at, reason)"
            " VALUES (?,1,?,NULL)",
            (b["id"], now),
        )
        self.conn.commit()

    def list_batch_parties(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM batch_parties WHERE batch_id=? ORDER BY ord",
            (batch_id,),
        ))

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()

    def set_status(
        self, batch_id: str, status: str, now: str,
        window: int | None = None,
    ) -> None:
        if window is None:
            self.conn.execute(
                "UPDATE batches SET status=?, updated_at=? WHERE id=?",
                (status, now, batch_id),
            )
        else:
            self.conn.execute(
                "UPDATE batches SET status=?, current_window=?, updated_at=?"
                " WHERE id=?",
                (status, window, now, batch_id),
            )
        self.conn.commit()

    def add_batch_student(
        self, batch_id: str, student_id: str, now: str,
    ) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO batch_students(batch_id, student_id, added_at)"
            " VALUES (?,?,?)",
            (batch_id, student_id, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_batch_students(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT s.* FROM students s JOIN batch_students bs"
            " ON s.id=bs.student_id WHERE bs.batch_id=? ORDER BY s.id",
            (batch_id,),
        ))

    def open_window(self, batch_id: str, n: int, now: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO windows(batch_id, window_n, opened_at, reason)"
            " VALUES (?,?,?,?)",
            (batch_id, n, now, reason),
        )
        self.conn.commit()

    def close_window(self, batch_id: str, n: int, now: str) -> None:
        self.conn.execute(
            "UPDATE windows SET closed_at=? WHERE batch_id=? AND window_n=?",
            (now, batch_id, n),
        )
        self.conn.commit()

    def list_seals(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM seals WHERE batch_id=? ORDER BY window_n",
            (batch_id,),
        ))

    def add_seal(
        self, batch_id: str, n: int, now: str,
        snapshot_json: str, checksum: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO seals(batch_id, window_n, sealed_at, snapshot_json,"
            " checksum) VALUES (?,?,?,?,?)",
            (batch_id, n, now, snapshot_json, checksum),
        )
        self.conn.commit()

    # ---- 只追加事件 -----------------------------------------------------

    def insert_event(self, e: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO score_events(batch_id, window_n, student_id, rater_id,"
            " party, kind, value, recorded_at, effective_at, reason,"
            " discussion_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (e["batch_id"], e["window_n"], e["student_id"], e["rater_id"],
             e["party"], e["kind"], e.get("value"), e["recorded_at"],
             e["effective_at"], e.get("reason"), e.get("discussion_id")),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_events(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM score_events WHERE batch_id=? ORDER BY seq",
            (batch_id,),
        ))

    def get_event(self, seq: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM score_events WHERE seq=?", (seq,)
        ).fetchone()

    # ---- 讨论 / 复核 ----------------------------------------------------

    def upsert_discussion(self, d: dict[str, Any], now: str) -> tuple[int, bool]:
        """返回 (id, created)。

        - 已由协调员结案（acknowledged）的讨论不自动重开，仅刷新详情；
          委员会接受过的结论保持有效，需要时可显式登记新讨论。
        - 曾因问题消失而自动关闭（auto_closed）的讨论在问题重现时重开。
        """
        row = self.conn.execute(
            "SELECT id, status FROM discussions WHERE batch_id=? AND window_n=?"
            " AND key=?",
            (d["batch_id"], d["window_n"], d["key"]),
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO discussions(batch_id, window_n, key, category,"
                " severity, student_id, detail_json, status, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,'open',?,?)",
                (d["batch_id"], d["window_n"], d["key"], d["category"],
                 d["severity"], d.get("student_id"),
                 json.dumps(d["detail"], ensure_ascii=False, sort_keys=True),
                 now, now),
            )
            self.conn.commit()
            return int(cur.lastrowid), True
        self.conn.execute(
            "UPDATE discussions SET detail_json=?, updated_at=? WHERE id=?",
            (json.dumps(d["detail"], ensure_ascii=False, sort_keys=True),
             now, row["id"]),
        )
        if row["status"] == "auto_closed":
            self.conn.execute(
                "UPDATE discussions SET status='open' WHERE id=?",
                (row["id"],),
            )
        self.conn.commit()
        return int(row["id"]), False

    def set_discussion_status(
        self, discussion_id: int, status: str, now: str,
    ) -> None:
        self.conn.execute(
            "UPDATE discussions SET status=?, updated_at=? WHERE id=?",
            (status, now, discussion_id),
        )
        self.conn.commit()

    def list_discussions(
        self, batch_id: str, window_n: int | None = None,
    ) -> list[sqlite3.Row]:
        if window_n is None:
            return list(self.conn.execute(
                "SELECT * FROM discussions WHERE batch_id=?"
                " ORDER BY window_n, id",
                (batch_id,),
            ))
        return list(self.conn.execute(
            "SELECT * FROM discussions WHERE batch_id=? AND window_n=?"
            " ORDER BY id",
            (batch_id, window_n),
        ))

    def get_discussion(self, discussion_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM discussions WHERE id=?", (discussion_id,)
        ).fetchone()

    def add_comment(
        self, discussion_id: int, author: str, body: str, now: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO discussion_comments(discussion_id, author, body,"
            " created_at) VALUES (?,?,?,?)",
            (discussion_id, author, body, now),
        )
        self.conn.commit()

    def list_comments(self, discussion_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM discussion_comments WHERE discussion_id=?"
            " ORDER BY id",
            (discussion_id,),
        ))

    def add_resolution(self, r: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO review_resolutions(batch_id, window_n, discussion_id,"
            " student_id, rater_id, decision, rescore_seq, decided_by,"
            " rationale, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["batch_id"], r["window_n"], r["discussion_id"], r["student_id"],
             r.get("rater_id"), r["decision"], r.get("rescore_seq"),
             r["decided_by"], r["rationale"], r["decided_at"]),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_resolutions(self, batch_id: str, window_n: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM review_resolutions WHERE batch_id=? AND window_n=?"
            " ORDER BY id",
            (batch_id, window_n),
        ))

    def add_review_sample(
        self, batch_id: str, window_n: int, student_id: str,
        reason: str, now: str,
    ) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO review_samples(batch_id, window_n,"
            " student_id, reason, selected_at) VALUES (?,?,?,?,?)",
            (batch_id, window_n, student_id, reason, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_review_samples(self, batch_id: str, window_n: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM review_samples WHERE batch_id=? AND window_n=?",
            (batch_id, window_n),
        ))

    def add_check_run(
        self, batch_id: str, window_n: int, now: str,
        findings: list[dict[str, Any]], open_count: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO check_runs(batch_id, window_n, ran_at, findings_json,"
            " open_count) VALUES (?,?,?,?,?)",
            (batch_id, window_n, now,
             json.dumps(findings, ensure_ascii=False, sort_keys=True),
             open_count),
        )
        self.conn.commit()

    # ---- 会签与发布 -----------------------------------------------------

    def add_signature(
        self, batch_id: str, signer_id: str, party: str, now: str,
    ) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO signatures(batch_id, signer_id, party,"
            " signed_at) VALUES (?,?,?,?)",
            (batch_id, signer_id, party, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_signatures(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM signatures WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ))

    def save_publication(
        self, batch_id: str, now: str, rule_json: str,
        result_json: str, checksum: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO publications(batch_id, published_at, rule_json,"
            " result_json, checksum) VALUES (?,?,?,?,?)",
            (batch_id, now, rule_json, result_json, checksum),
        )
        self.conn.commit()

    def get_publication(self, batch_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM publications WHERE batch_id=?", (batch_id,)
        ).fetchone()

    def close(self) -> None:
        self.conn.close()
