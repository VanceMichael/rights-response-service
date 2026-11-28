"""测试与脚本用的极简 JSON HTTP 客户端（标准库）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class Client:
    def __init__(self, base: str, user_id: str | None = None,
                 current_time: str | None = None):
        self.base = base.rstrip("/")
        self.user_id = user_id
        self.current_time = current_time

    def as_user(self, user_id: str, current_time: str | None = None) -> "Client":
        return Client(self.base, user_id, current_time or self.current_time)

    def at(self, current_time: str) -> "Client":
        return Client(self.base, self.user_id, current_time)

    def _request(self, method: str, path: str, body=None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.user_id:
            headers["X-User-ID"] = self.user_id
        if self.current_time:
            headers["X-Current-Time"] = self.current_time
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") \
            if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, body: dict):
        return self._request("POST", path, body)
