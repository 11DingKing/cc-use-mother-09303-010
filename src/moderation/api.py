"""HTTP API：仅依赖标准库。

路由概览：
  基础数据
    POST /parties /students /rubrics /graders
    POST /graders/qualifications
  批次
    POST /batches                         GET  /batches/{id}/status
    POST /batches/{id}/students
    POST /batches/{id}/policy
    POST /batches/{id}/scores             POST /batches/{id}/recusals
    POST /batches/{id}/absence-marks      POST /batches/{id}/absence-decisions
    POST /batches/{id}/checks             GET  /batches/{id}/samples
    POST /samples/{sid}/discussions       POST /samples/{sid}/resolve
    POST /batches/{id}/seal               POST /batches/{id}/reopen
    POST /batches/{id}/signatures         POST /batches/{id}/unsign
    POST /batches/{id}/publish            GET  /batches/{id}/report
    GET  /batches/{id}/recompute          GET  /events
所有写操作的操作者取 body.actor 或 X-Actor 头。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .errors import ConflictError, ModerationError, NotFoundError, ValidationError
from .service import ModerationService
from .store import Store


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class ModerationHandler(BaseHTTPRequestHandler):
    service: ModerationService = None  # type: ignore[assignment]
    lock: threading.Lock = None  # type: ignore[assignment]

    server_version = "ModerationServer/1.0"

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        return

    # -- 工具 -----------------------------------------------------------------

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        if "actor" not in value:
            header_actor = self.headers.get("X-Actor")
            if header_actor:
                value["actor"] = header_actor
        return value

    def _actor(self, body: dict, default: str = "system") -> str:
        return str(body.get("actor") or self.headers.get("X-Actor") or default)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        body = self._read_json() if method == "POST" else {}
        try:
            with self.lock:
                result, status = self._route(method, path, query, body)
            _json_response(self, status, result)
        except ModerationError as exc:
            status = {
                ValidationError: 400,
                NotFoundError: 404,
                ConflictError: 409,
            }[type(exc)]
            _json_response(self, status, {"error": type(exc).__name__, "message": str(exc)})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # -- 路由 -----------------------------------------------------------------

    def _route(self, method: str, path: str, query: dict, body: dict):
        svc = self.service
        actor = self._actor(body)

        if method == "POST":
            if path == "/parties":
                svc.register_party(body["party_id"], body["name"], actor)
                return {"ok": True}, 201
            if path == "/students":
                svc.register_student(body["student_id"], body["name"], actor)
                return {"ok": True}, 201
            if path == "/rubrics":
                svc.create_rubric_version(
                    body["rubric_id"], body["version"], body["title"],
                    body["dimensions"], body["party_id"], actor,
                )
                return {"ok": True}, 201
            if path == "/graders":
                svc.register_grader(body["grader_id"], body["party_id"], body["name"], actor)
                return {"ok": True}, 201
            if path == "/graders/qualifications":
                svc.set_qualification(
                    body["grader_id"], body["rubric_id"], body["version"],
                    bool(body["qualified"]), actor,
                )
                return {"ok": True}, 200

            m = re.fullmatch(r"/batches/([^/]+)", path)
            if m:
                svc.create_batch(
                    m.group(1), body["title"], body["rubric_id"], body["rubric_version"],
                    body["absence_policy"], int(body["required_signatures"]),
                    int(body["min_graders"]), actor,
                    student_ids=body.get("student_ids"), deadline=body.get("deadline"),
                )
                return {"ok": True}, 201

            m = re.fullmatch(r"/batches/([^/]+)/students", path)
            if m:
                svc.enroll_student(m.group(1), body["student_id"], actor)
                return {"ok": True}, 201
            m = re.fullmatch(r"/batches/([^/]+)/policy", path)
            if m:
                svc.change_absence_policy(m.group(1), body["policy"], actor, body.get("reason", ""))
                return {"ok": True}, 200
            m = re.fullmatch(r"/batches/([^/]+)/scores", path)
            if m:
                sid = svc.record_score(
                    m.group(1), body["student_id"], body["grader_id"], body["dimension"],
                    float(body["score"]), actor, is_late=bool(body.get("is_late", False)),
                )
                return {"ok": True, "score_id": sid}, 201
            m = re.fullmatch(r"/batches/([^/]+)/recusals", path)
            if m:
                svc.record_recusal(
                    m.group(1), body["student_id"], body["grader_id"],
                    body.get("reason", ""), actor,
                )
                return {"ok": True}, 201
            m = re.fullmatch(r"/batches/([^/]+)/absence-marks", path)
            if m:
                svc.mark_absence(
                    m.group(1), body["student_id"], body["party_id"], body["treatment"], actor
                )
                return {"ok": True}, 201
            m = re.fullmatch(r"/batches/([^/]+)/absence-decisions", path)
            if m:
                svc.resolve_absence(
                    m.group(1), body["student_id"], body["treatment"],
                    body.get("reason", ""), actor,
                )
                return {"ok": True}, 201
            m = re.fullmatch(r"/batches/([^/]+)/checks", path)
            if m:
                return svc.run_consistency_check(m.group(1), actor), 200
            m = re.fullmatch(r"/samples/(\d+)/discussions", path)
            if m:
                svc.discuss(int(m.group(1)), body["party_id"], body["comment"], actor)
                return {"ok": True}, 201
            m = re.fullmatch(r"/samples/(\d+)/resolve", path)
            if m:
                svc.resolve_sample(int(m.group(1)), body["decision"], actor)
                return {"ok": True}, 200
            m = re.fullmatch(r"/batches/([^/]+)/seal", path)
            if m:
                return svc.seal_batch(m.group(1), actor), 200
            m = re.fullmatch(r"/batches/([^/]+)/reopen", path)
            if m:
                svc.reopen_batch(m.group(1), actor, body.get("reason", ""))
                return {"ok": True}, 200
            m = re.fullmatch(r"/batches/([^/]+)/signatures", path)
            if m:
                svc.sign(m.group(1), body["party_id"], actor)
                return {"ok": True}, 201
            m = re.fullmatch(r"/batches/([^/]+)/unsign", path)
            if m:
                svc.unsign(m.group(1), body["party_id"], actor)
                return {"ok": True}, 200
            m = re.fullmatch(r"/batches/([^/]+)/publish", path)
            if m:
                return svc.publish(m.group(1), actor), 200

        if method == "GET":
            if path == "/events":
                rows = svc.store.list_events(query.get("batch_id", [None])[0])
                return {"events": [dict(r) for r in rows]}, 200
            m = re.fullmatch(r"/batches/([^/]+)/status", path)
            if m:
                return svc.batch_status(m.group(1)), 200
            m = re.fullmatch(r"/batches/([^/]+)/samples", path)
            if m:
                return {"samples": svc.list_review_samples(m.group(1))}, 200
            m = re.fullmatch(r"/batches/([^/]+)/report", path)
            if m:
                return svc.get_published_report(m.group(1)), 200
            m = re.fullmatch(r"/batches/([^/]+)/recompute", path)
            if m:
                return svc.compute_report(m.group(1)), 200

        raise NotFoundError(f"没有对应路由：{method} {path}")


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    store = Store(db_path)
    service = ModerationService(store)
    lock = threading.Lock()

    handler = type("BoundHandler", (ModerationHandler,), {"service": service, "lock": lock})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.service = service  # type: ignore[attr-defined]
    httpd.store = store  # type: ignore[attr-defined]
    return httpd
