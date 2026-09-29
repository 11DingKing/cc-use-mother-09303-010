"""基于标准库 ``http.server`` 的 REST 接口。

集合资源用 POST 创建，批次动作用 POST 子路径，查询用 GET。
错误统一映射为 ``{"error": "..."}``，状态码取自 ``DomainError.status``。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .engine import ModerationService
from .errors import DomainError, NotFoundError
from .store import Store


class Handler(BaseHTTPRequestHandler):
    service: ModerationService  # 由 build_server 注入

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        return

    # ---- HTTP 基础 ------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return body

    def _send(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            body = self._read_json() if method == "POST" else {}
            status, data = self._route(method, self.path, body)
            self._send(status, data)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError) as exc:
            self._send(400, {"error": f"请求参数缺失或不合法：{exc}"})
        except Exception as exc:  # 防御：不向客户端泄漏堆栈细节
            self._send(500, {"error": f"内部错误：{exc}"})

    # ---- 路由 -----------------------------------------------------------

    def _route(self, method: str, path: str, b: dict) -> tuple[int, Any]:
        svc = self.service

        def match(pattern: str) -> tuple | None:
            m = re.fullmatch(pattern, path)
            return m.groups() if m else None

        if method == "POST":
            if path == "/parties":
                return 200, svc.register_party(b["code"], b["name"])
            if path == "/rubrics":
                return 200, svc.register_rubric(
                    b["id"], b["title"], b["scale_min"], b["scale_max"],
                    b.get("checksum"))
            if path == "/raters":
                return 200, svc.register_rater(
                    b["id"], b["name"], b["party"], b.get("role", "teacher"))
            if path == "/students":
                return 200, svc.register_student(b["id"], b["name"])
            if path == "/batches":
                return 200, svc.create_batch(
                    b["id"], b["rubric_id"], b["absence_policies"],
                    b.get("gap_threshold", 10.0), b.get("grading_deadline"),
                    b.get("min_signatures", 2),
                    b.get("signature_rule", "both"))

            g = match(r"^/raters/([^/]+)/qualifications$")
            if g:
                return 200, svc.grant_qualification(g[0], b["rubric_id"])
            g = match(r"^/raters/([^/]+)/qualifications/([^/]+)/revoke$")
            if g:
                return 200, svc.revoke_qualification(g[0], g[1])
            g = match(r"^/batches/([^/]+)/students$")
            if g:
                return 200, svc.add_student(g[0], b["student_id"])
            g = match(r"^/batches/([^/]+)/scores$")
            if g:
                return 200, svc.record_score(
                    g[0], b["student_id"], b["rater_id"], b["value"],
                    b.get("as_of"), bool(b.get("accept_late", False)),
                    b.get("reason"))
            g = match(r"^/batches/([^/]+)/absences$")
            if g:
                return 200, svc.declare_absence(
                    g[0], b["student_id"], b["party"], b["reporter_id"],
                    b["reason"])
            g = match(r"^/batches/([^/]+)/absence-retractions$")
            if g:
                return 200, svc.retract_absence(
                    g[0], b["student_id"], b["party"], b["reporter_id"],
                    b["reason"])
            g = match(r"^/batches/([^/]+)/recusals$")
            if g:
                return 200, svc.mark_recusal(
                    g[0], b["student_id"], b["rater_id"], b["reason"])
            g = match(r"^/batches/([^/]+)/checks$")
            if g:
                return 200, svc.run_consistency_check(g[0])
            g = match(r"^/batches/([^/]+)/seal$")
            if g:
                return 200, svc.seal_calibration(g[0])
            g = match(r"^/batches/([^/]+)/reopen$")
            if g:
                return 200, svc.reopen_batch(g[0], b["reason"])
            g = match(r"^/batches/([^/]+)/review$")
            if g:
                return 200, svc.begin_review(g[0])
            g = match(r"^/batches/([^/]+)/review-samples$")
            if g:
                return 200, svc.add_review_sample(
                    g[0], b["student_id"], b["reason"])
            g = match(r"^/batches/([^/]+)/rescores$")
            if g:
                return 200, svc.record_rescore(
                    g[0], b["student_id"], b["rater_id"], b["value"],
                    b["discussion_id"], b["decided_by"], b["rationale"])
            g = match(r"^/batches/([^/]+)/countersign$")
            if g:
                return 200, svc.begin_countersign(g[0])
            g = match(r"^/batches/([^/]+)/signatures$")
            if g:
                return 200, svc.sign(g[0], b["signer_id"])
            g = match(r"^/batches/([^/]+)/publish$")
            if g:
                return 200, svc.publish(g[0])
            g = match(r"^/batches/([^/]+)/verify$")
            if g:
                return 200, svc.verify_publication(g[0])
            g = match(r"^/discussions/(\d+)/comments$")
            if g:
                return 200, svc.add_discussion_comment(
                    int(g[0]), b["author_id"], b["body"])
            g = match(r"^/discussions/(\d+)/acknowledge$")
            if g:
                return 200, svc.acknowledge_discussion(
                    int(g[0]), b["coordinator_id"], b["rationale"])

        if method == "GET":
            g = match(r"^/batches/([^/]+)/students/([^/]+)/trace$")
            if g:
                return 200, svc.student_trace(g[0], g[1])
            g = match(r"^/batches/([^/]+)/publication$")
            if g:
                return 200, svc.get_publication(g[0])
            g = match(r"^/batches/([^/]+)/results$")
            if g:
                results, max_seq = svc.compute_results(g[0])
                return 200, {"as_of_seq": max_seq, **results}
            g = match(r"^/batches/([^/]+)/events$")
            if g:
                return 200, {"events": svc.list_events(g[0])}
            g = match(r"^/batches/([^/]+)/discussions$")
            if g:
                return 200, {"discussions": svc.list_discussions(g[0])}
            g = match(r"^/batches/([^/]+)/seals$")
            if g:
                return 200, {"seals": svc.list_seals(g[0])}
            g = match(r"^/batches/([^/]+)$")
            if g:
                return 200, svc.get_batch(g[0])

        raise NotFoundError(f"无此路由：{method} {path}")


def build_server(
    db_path: str = ":memory:", host: str = "127.0.0.1", port: int = 8080,
) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = ModerationService(store)

    class _BoundHandler(Handler):
        pass

    _BoundHandler.service = service
    httpd = ThreadingHTTPServer((host, port), _BoundHandler)
    httpd.service = service  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd
