"""持久化 HTTP 能力：标准库 ThreadingHTTPServer + JSON 接口。

约定：
- X-User-ID 标识操作人（演练身份，见 catalog.principals，非认证凭据）。
- X-Current-Time 可注入“现在”（带时区 ISO-8601），用于期限与迟到回执演练；
  缺省使用服务器墙钟。所有事件按该时刻持久化。
- 业务错误返回 4xx 与中文错误体；5xx 仅用于服务器自身故障。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .errors import DomainError
from .service import Service
from .storage import Store
from .timeutil import Clock, parse_dt


def _ok_default_time() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "SyntheticProtection/1.0"

    # 由 server 注入
    service: Service
    store: Store

    def log_message(self, *args):  # 静默常规访问日志
        pass

    # --- 工具 -----------------------------------------------------------
    def _send(self, status: int, value) -> None:
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    def _clock(self) -> Clock:
        value = self.headers.get("X-Current-Time")
        if value:
            return Clock(parse_dt(value))
        return Clock(parse_dt(_ok_default_time()))

    def _actor(self) -> str:
        actor = self.headers.get("X-User-ID")
        if not actor:
            raise DomainError("缺少 X-User-ID 操作人头")
        return actor

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        with self.store.begin():
            try:
                if path == "/health":
                    return self._send(200, {"status": "ok",
                                            "service": "synthetic-protection"})
                if method == "GET":
                    return self._handle_get(path, query)
                body = self._read_body()
                actor = self._actor()
                clock = self._clock()
                self._handle_post(path, body, actor, clock)
            except DomainError as exc:
                self._send(exc.status, exc.to_body())
            except KeyError as exc:
                self._send(422, {"error": f"缺少必填字段: {exc.args[0]}",
                                 "code": "MissingField"})
            except sqlite3.IntegrityError as exc:
                # 唯一约束等：同 ID 重复写入属冲突，不暴露为 500
                self._send(409, {"error": f"数据冲突: {exc}",
                                 "code": "IntegrityConflict"})
            except (ValueError, TypeError) as exc:
                self._send(422, {"error": str(exc), "code": "ValidationFailed"})
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": f"服务器内部错误: {exc}",
                                 "code": "InternalError"})

    # --- GET ------------------------------------------------------------
    def _handle_get(self, path: str, query: dict) -> None:
        s = self.service
        single = {
            "/users": ("user", "SELECT * FROM users ORDER BY id"),
            "/works": ("work", "SELECT * FROM works ORDER BY id"),
            "/platforms": ("platform", None),
            "/calendars": ("calendar", None),
            "/cases": ("case", "SELECT * FROM cases ORDER BY id"),
        }
        if path in single:
            kind, sql = single[path]
            if sql:
                rows = [dict(r) for r in self.store.all(sql)]
            elif kind == "platform":
                rows = [dict(r) for r in self.store.all(
                    "SELECT * FROM platforms ORDER BY id")]
                for r in rows:
                    r["capabilities"] = json.loads(r["capabilities"])
            else:
                rows = [dict(r) for r in self.store.all(
                    "SELECT * FROM calendars ORDER BY code")]
                for r in rows:
                    r["holidays"] = json.loads(r["holidays"])
                    r["make_up_days"] = json.loads(r["make_up_days"])
            return self._send(200, {kind + "s": rows})

        if path.startswith("/notices/") and path.endswith("/trace"):
            notice_id = path.split("/")[2]
            return self._send(200, s.notice_trace(notice_id))
        if path.startswith("/publications/") and path.endswith("/board"):
            publication_id = path.split("/")[2]
            return self._send(200, s.publication_board(
                publication_id, self._clock()))
        if path.startswith("/notices/"):
            return self._send(200, s._notice(path.split("/")[2]))
        if path.startswith("/disputes/"):
            return self._send(200, s._dispute(path.split("/")[2]))
        if path.startswith("/deliveries/"):
            return self._send(200, s._delivery(path.split("/")[2]))
        if path == "/events":
            entity_type = query.get("entity_type", [None])[0]
            entity_id = query.get("entity_id", [None])[0]
            if entity_type and entity_id:
                return self._send(200, {"events": self.store.history(
                    entity_type, entity_id)})
            return self._send(200, {"events": [
                dict(r) | {"payload_json": json.loads(r["payload_json"])}
                for r in self.store.all(
                    "SELECT * FROM events ORDER BY id DESC LIMIT 200")]})
        self._send(404, {"error": f"接口不存在: {path}", "code": "NotFound"})

    # --- POST -----------------------------------------------------------
    def _handle_post(self, path: str, body: dict, actor: str,
                     clock: Clock) -> None:
        s = self.service
        routes = {
            "/users": lambda: s.create_user(
                actor, body["user_id"], body["role"], body["display_name"],
                clock),
            "/works": lambda: s.create_work(
                actor, body["work_id"], body["duration_ms"], clock),
            "/rights": lambda: s.create_rights(
                actor, body["rights_id"], body["work_id"], body["owner"],
                body["regions"], body["media"], body["start_at"],
                body["end_at"], clock),
            "/mandates": lambda: s.create_mandate(
                actor, body["mandate_id"], body["rights_id"],
                body["agent_id"], clock),
            "/mandates/revoke": lambda: s.revoke_mandate(
                actor, body["mandate_id"], clock, body.get("reason", "")),
            "/platforms": lambda: s.create_platform(
                actor, body["platform_id"], body["name"],
                body["capabilities"], body.get("replay_base_url"), clock),
            "/publications": lambda: s.create_publication(
                actor, body["publication_id"], body["work_id"],
                body["platform_id"], body["link"], body["content_sha256"],
                body["range_ms"], clock),
            "/calendars": lambda: s.upsert_calendar(
                actor, body["code"], body["timezone"], body["holidays"],
                body["make_up_days"], clock),
            "/cases": lambda: s.create_case(
                actor, body["case_id"], body["work_id"], body["title"],
                body["calendar_code"], clock),
            "/notices": lambda: s.submit_notice(actor, body, clock),
            "/notices/supplement": lambda: s.supplement_notice(
                actor, body["notice_id"], body, clock),
            "/notices/withdraw": lambda: s.withdraw_notice(
                actor, body["notice_id"], clock, body.get("reason", "")),
            "/legal/decisions": lambda: s.legal_decision(actor, body, clock),
            "/disputes": lambda: s.create_dispute(actor, body, clock),
            "/disputes/validity": lambda: s.check_dispute_validity(
                actor, body, clock),
            "/disputes/supplement": lambda: s.supplement_dispute(
                actor, body, clock),
            "/disputes/serve": lambda: s.serve_dispute(actor, body, clock),
            "/disputes/continuation": lambda: s.record_continuation(
                actor, body, clock),
            "/disputes/review": lambda: s.review_dispute(actor, body, clock),
            "/timeouts/evaluate": lambda: {"evaluated": s.evaluate_timeouts(
                clock)},
            "/deliveries": lambda: s.request_delivery(actor, body, clock),
            "/deliveries/negotiation": lambda: s.register_negotiation(
                actor, body, clock),
            "/deliveries/accept": lambda: s.record_acceptance(
                actor, body, clock),
            "/deliveries/unknown": lambda: s.record_unknown(
                actor, body, clock),
            "/deliveries/observe": lambda: s.record_observation(
                actor, body, clock),
            "/deliveries/event": lambda: s.record_manual_event(
                actor, body, clock),
        }
        handler = routes.get(path)
        if not handler:
            return self._send(404, {"error": f"接口不存在: {path}",
                                    "code": "NotFound"})
        result = handler()
        self.store.commit()
        self._send(200, result)


def build_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    service = Service(store)

    class _Handler(Handler):
        pass

    _Handler.service = service
    _Handler.store = store
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.service = service
    httpd.store = store
    return httpd
