"""持久化 HTTP 能力：真实环回服务、角色头、注入时钟、重启后状态保留。"""
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.client import Client
from app.http_app import build_server
from app.seed import seed
from app.storage import Store
from tests.helpers import (MATERIALS, T_DEC2, T_DEC3, T_DEC8_10, T_DEC10,
                           T_DEC15_1830)


class HttpServerThread:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> str:
        store = Store(self.db_path)
        seed(store, Path(__file__).resolve().parents[1] / "data" / "catalog.json")
        self.httpd = build_server("127.0.0.1", 0, store)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd.store.close()


class HttpFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "http.sqlite3")
        self.server = HttpServerThread(self.db)
        self.base = self.server.start()

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def test_health_and_role_enforcement(self):
        c = Client(self.base)
        self.assertEqual(c.get("/health")[0], 200)
        # 缺操作人头
        status, body = c.post("/cases", {"case_id": "x"})
        self.assertEqual(status, 400)
        # 联络人不能代行法务
        status, body = Client(self.base, "liaison-A").post(
            "/legal/decisions", {"notice_id": "nope", "decision": "approved"})
        self.assertEqual(status, 403)

    def test_persisted_full_flow_survives_restart(self):
        agent_a = Client(self.base, "agent-A")
        legal = Client(self.base, "legal-A")
        liaison = Client(self.base, "liaison-A")
        # 建案 + 提交 + 核准
        self.assertEqual(agent_a.at(T_DEC2).post("/cases", {
            "case_id": "case-http", "work_id": "film-A", "title": "HTTP 案",
            "calendar_code": "calendar-A"})[0], 200)
        status, notice = agent_a.at(T_DEC2).post("/notices", {
            "case_id": "case-http", "notice_id": "n-A",
            "publication_id": "post-A", "mandate_id": "mandate-A",
            "regions": ["east", "south"], "media": ["streaming"],
            "range_ms": [0, 60000], "materials": dict(MATERIALS)})
        self.assertEqual(status, 200)
        self.assertEqual(notice["status"], "submitted")
        status, decision = legal.at(T_DEC2).post("/legal/decisions", {
            "notice_id": "n-A", "decision": "approved",
            "regions": ["east"], "media": ["streaming"],
            "range_ms": [0, 60000]})
        self.assertEqual(status, 200)

        # 送达 → 受理 → 中断 → 查询到受限
        self.assertEqual(liaison.at(T_DEC3).post("/deliveries", {
            "notice_id": "n-A", "delivery_id": "d-A"})[0], 200)
        self.assertEqual(liaison.at(T_DEC3).post("/deliveries/accept", {
            "delivery_id": "d-A"})[0], 200)
        self.assertEqual(liaison.at(T_DEC3).post("/deliveries/unknown", {
            "delivery_id": "d-A"})[0], 200)
        self.assertEqual(liaison.at(T_DEC3).post("/deliveries/observe", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 12})[0], 200)

        # 撤销委托
        self.assertEqual(legal.at(T_DEC8_10).post("/mandates/revoke", {
            "mandate_id": "mandate-A"})[0], 200)
        # 迟到回执
        status, delivery = liaison.at(T_DEC10).post("/deliveries/observe", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 14})
        self.assertEqual(status, 200)
        self.assertEqual(delivery["state"], "restricted_no_basis")

        # 重启服务（同一状态文件），数据保留
        self.server.stop()
        self.server = HttpServerThread(self.db)
        new_base = self.server.start()
        c = Client(new_base)
        status, board = c.at(T_DEC10).get("/publications/post-A/board")
        self.assertEqual(status, 200)
        self.assertIsNone(board["required_scope"])
        self.assertIsNotNone(board["platform_actual_scope"])
        # 历史事件仍可追溯
        status, trace = c.get("/notices/n-A/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["notice"]["id"], "n-A")
        self.assertTrue(trace["deliveries"][0]["events"])

    def test_clock_header_drives_deadline_and_timeout(self):
        agent_b = Client(self.base, "agent-B")
        legal = Client(self.base, "legal-A")
        liaison = Client(self.base, "liaison-A")
        reviewer = Client(self.base, "reviewer-A")
        agent_b.at(T_DEC2).post("/cases", {
            "case_id": "case-c", "work_id": "film-A", "title": "c",
            "calendar_code": "calendar-A"})
        agent_b.at(T_DEC2).post("/notices", {
            "case_id": "case-c", "notice_id": "n-B",
            "publication_id": "post-A", "mandate_id": "mandate-B",
            "regions": ["south"], "media": ["streaming"],
            "range_ms": [30000, 60000], "materials": dict(MATERIALS)})
        legal.at(T_DEC2).post("/legal/decisions", {
            "notice_id": "n-B", "decision": "approved",
            "regions": ["south"], "media": ["streaming"],
            "range_ms": [30000, 60000]})
        liaison.at("2026-12-07T10:00:00+08:00").post("/disputes", {
            "dispute_id": "disp-B", "notice_id": "n-B",
            "uploader_id": "uploader-A", "regions": ["south"],
            "media": ["streaming"], "range_ms": [30000, 60000],
            "license_sha256": "cccc"})
        reviewer.at("2026-12-07T11:00:00+08:00").post("/disputes/validity", {
            "dispute_id": "disp-B", "valid": True})
        liaison.at("2026-12-08T09:00:00+08:00").post("/disputes/serve", {
            "dispute_id": "disp-B", "recipient_id": "agent-B",
            "served_at": "2026-12-08T09:00:00+08:00"})
        # 截止前未到期
        status, evaluated = Client(
            self.base, "system", "2026-12-15T17:59:00+08:00").post(
            "/timeouts/evaluate", {})
        self.assertEqual(evaluated["evaluated"], [])
        # 截止后到期
        status, evaluated = Client(
            self.base, "system", T_DEC15_1830).post(
            "/timeouts/evaluate", {})
        self.assertEqual(len(evaluated["evaluated"]), 1)
        status, dispute = c_get = Client(self.base).get("/disputes/disp-B")
        self.assertEqual(dispute["status"], "lapsed")


if __name__ == "__main__":
    unittest.main()
