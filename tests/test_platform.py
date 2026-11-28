"""平台能力差异、整项限制、链接换版本与历史不可改写。"""
import unittest

from app.errors import PlatformCapability, StateConflict
from tests.helpers import Harness, T_DEC2, T_DEC3, T_DEC10, T_DEC16


class PlatformAndVersionTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.make_case()

    def tearDown(self):
        self.h.close()

    def test_whole_item_platform_cannot_partial_restrict(self):
        h = self.h
        # post-B 在平台乙（只支持整项）；部分范围请求获核准后送达须待协商
        h.submit("n-C", "agent-A", "mandate-A", "post-B",
                 ["east"], (0, 60000), T_DEC2)
        h.approve("n-C", ["east"], (0, 60000), T_DEC2)
        with self.assertRaises(PlatformCapability):
            h.svc.request_delivery("liaison-A", {
                "notice_id": "n-C", "delivery_id": "d-C"}, h.clock(T_DEC3))
        # 登记待协商并在看板暴露
        h.svc.register_negotiation("liaison-A", {
            "notice_id": "n-C", "delivery_id": "d-C"}, h.clock(T_DEC3))
        board = h.svc.publication_board("post-B", h.clock(T_DEC3))
        self.assertTrue(any(g["type"] == "capability_negotiation"
                            for g in board["differences"]))

    def test_same_link_new_content_version_does_not_inherit_approval(self):
        h = self.h
        h.submit("n-A", "agent-A", "mandate-A", "post-A",
                 ["east"], (0, 60000), T_DEC2)
        h.approve("n-A", ["east"], (0, 60000), T_DEC2)
        # 同链接、不同内容摘要 → 新版本发布项
        h.svc.create_publication("legal-A", "post-A-v2", "film-A",
                                 "platform-A", "local://clips/alpha",
                                 "dddddddddddddddddddddddddddddddd"
                                 "dddddddddddddddddddddddddddddddd",
                                 [0, 60000], h.clock(T_DEC10))
        # 原请求绑定 post-A，对新版本无有效依据，不能直接送达
        with self.assertRaises(StateConflict):
            h.svc.request_delivery("liaison-A", {
                "notice_id": "n-A", "delivery_id": "d-v2",
                "publication_id": "post-A-v2"}, h.clock(T_DEC10))
        # 即使不显式指定版本，原请求依据在新版本看板上也不存在
        board = h.svc.publication_board("post-A-v2", h.clock(T_DEC10))
        self.assertIsNone(board["required_scope"])

    def test_history_preserved_after_correction(self):
        h = self.h
        h.submit("n-A", "agent-A", "mandate-A", "post-A",
                 ["east"], (0, 60000), T_DEC2)
        h.approve("n-A", ["east"], (0, 60000), T_DEC2)
        h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "delivery_id": "d-A"}, h.clock(T_DEC3))
        h.svc.record_acceptance("liaison-A", {"delivery_id": "d-A"},
                                h.clock(T_DEC3))
        h.svc.record_observation("liaison-A", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 12}, h.clock(T_DEC3))
        # 撤销后迟到回执改写当前状态，但历史事件仍保留“曾限制”
        h.svc.revoke_mandate("legal-A", "mandate-A",
                             h.clock("2026-12-08T10:00:00+08:00"))
        h.svc.record_observation("liaison-A", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 14}, h.clock(T_DEC10))
        events = h.store.history("delivery", "d-A")
        states = [e["payload"]["state"] for e in events
                  if "state" in e["payload"]]
        self.assertIn("restricted", states)
        self.assertIn("restricted_no_basis", states)
        # 观测事件按时间追加，顺序不可变
        self.assertEqual(states[0], "accepted")
        # 追溯链完整
        trace = h.svc.notice_trace("n-A")
        self.assertEqual(trace["legal_decisions"][0]["decision"], "approved")
        self.assertEqual(trace["deliveries"][0]["id"], "d-A")

    def test_accepted_is_not_effective_restriction(self):
        h = self.h
        h.submit("n-A", "agent-A", "mandate-A", "post-A",
                 ["east"], (0, 60000), T_DEC2)
        h.approve("n-A", ["east"], (0, 60000), T_DEC2)
        h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "delivery_id": "d-A"}, h.clock(T_DEC3))
        h.svc.record_acceptance("liaison-A", {"delivery_id": "d-A"},
                                h.clock(T_DEC3))
        # 仅受理：实际范围仍为空，看板提示继续跟进
        board = h.svc.publication_board("post-A", h.clock(T_DEC3))
        self.assertIsNone(board["platform_actual_scope"])
        self.assertTrue(any(g["type"] == "under_restricted"
                            for g in board["differences"]))


if __name__ == "__main__":
    unittest.main()
