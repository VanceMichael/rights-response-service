"""核心汇合场景：授权撤销、异议期限与迟到平台回执在同一发布项相遇。

时间线（post-A，日历 calendar-A）：
- 12-02 两权利方请求均获核准：n-A（owner-A，east 全段）、n-B（owner-B，south 30-60k）
- 12-03 n-A 送达平台甲，受理后 504 中断 → unknown，只能查原流水
- 12-07 查到原流水 restricted（此时依据仍有效，正常计入实际限制）
- 12-07 n-B 被提异议，12-08 09:00 有效送达，五日截止 12-15 18:00
- 12-08 10:00 mandate-A 撤销 → n-A 依据失效，n-B 不受影响
- 12-10 平台甲迟到 restricted 回执 → 已无依据，标记 restricted_no_basis
- 12-15 18:30 n-B 异议到期未获继续维持 → n-B 依据失效
- 此后应能判明：无有效依据、平台仍限制、需恢复且恢复落实前不得显示已恢复
"""
import unittest

from app.errors import StateConflict, UnknownDelivery
from tests.helpers import (Harness, T_DEC2, T_DEC3, T_DEC7, T_DEC8_09,
                           T_DEC8_10, T_DEC10, T_DEC15_1830, T_DEC16)


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        h = self.h
        h.make_case()
        # 两份不同权利方通知，落在同一发布项 post-A
        h.submit("n-A", "agent-A", "mandate-A", "post-A",
                 ["east", "south"], (0, 60000), T_DEC2)
        h.submit("n-B", "agent-B", "mandate-B", "post-A",
                 ["south"], (30000, 60000), T_DEC2)
        h.approve("n-A", ["east"], (0, 60000), T_DEC2)
        h.approve("n-B", ["south"], (30000, 60000), T_DEC2)

    def tearDown(self):
        self.h.close()

    def _restrict_flow_a(self):
        h = self.h
        h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "delivery_id": "d-A",
            "command_id": "restrict-A"}, h.clock(T_DEC3))
        h.svc.record_acceptance("liaison-A", {"delivery_id": "d-A"},
                                h.clock(T_DEC3))
        h.svc.record_unknown("liaison-A", {"delivery_id": "d-A"},
                             h.clock(T_DEC3))

    def test_unknown_flow_blocks_opposite_request(self):
        self._restrict_flow_a()
        with self.assertRaises(UnknownDelivery):
            self.h.svc.request_delivery("liaison-A", {
                "notice_id": "n-A", "direction": "restore"},
                self.h.clock(T_DEC3))

    def test_full_convergence_on_same_publication(self):
        h = self.h
        self._restrict_flow_a()

        # 12-07 查询原流水：restricted 且依据仍有效
        h.svc.record_observation("liaison-A", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 12}, h.clock(T_DEC7))
        self.assertEqual(h.svc._delivery("d-A")["state"], "restricted")
        board = h.svc.publication_board("post-A", h.clock(T_DEC7))
        self.assertIsNotNone(board["platform_actual_scope"])

        # 异议 n-B：12-07 收到、有效性通过、12-08 09:00 送达
        h.svc.create_dispute("liaison-A", {
            "dispute_id": "disp-B", "notice_id": "n-B",
            "uploader_id": "uploader-A", "regions": ["south"],
            "media": ["streaming"], "range_ms": [30000, 60000],
            "license_sha256": "cccc"}, h.clock(T_DEC7))
        h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-B", "valid": True}, h.clock(T_DEC7))
        h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-B", "recipient_id": "agent-B",
            "served_at": T_DEC8_09}, h.clock(T_DEC8_09))

        # 12-08 10:00 撤销 mandate-A：仅 n-A 失效，n-B（不同权利方）仍有效
        h.svc.revoke_mandate("legal-A", "mandate-A", h.clock(T_DEC8_10))
        self.assertIsNone(h.svc.notice_effective_scope(
            "n-A", h.clock(T_DEC8_10)))
        scope_b = h.svc.notice_effective_scope("n-B", h.clock(T_DEC8_10))
        self.assertIsNotNone(scope_b)
        self.assertEqual(scope_b.regions, frozenset({"south"}))
        board = h.svc.publication_board("post-A", h.clock(T_DEC8_10))
        owners = {b["owner"] for b in board["active_bases"]}
        self.assertEqual(owners, {"owner-B"})
        # 平台实际仍限制 east，应限制只剩 south → 同时存在不足与过度
        gap_types = {g["type"] for g in board["differences"]}
        self.assertIn("over_restricted", gap_types)

        # 12-10 迟到 restricted 回执：不能重新形成限制
        h.svc.record_observation("liaison-A", {
            "delivery_id": "d-A", "native_state": "restricted",
            "platform_revision": 14}, h.clock(T_DEC10))
        self.assertEqual(h.svc._delivery("d-A")["state"],
                         "restricted_no_basis")
        # 历史事实保留：12-07 时点该流水仍是 restricted
        board_hist = h.svc.publication_board("post-A", h.clock(T_DEC7))
        self.assertIsNotNone(board_hist["platform_actual_scope"])

        # 12-15 18:30 异议到期：n-B 也失去依据
        h.svc.evaluate_timeouts(h.clock(T_DEC15_1830))
        self.assertIsNone(h.svc.notice_effective_scope(
            "n-B", h.clock(T_DEC15_1830)))
        board = h.svc.publication_board("post-A", h.clock(T_DEC15_1830))
        self.assertIsNone(board["required_scope"])
        self.assertIsNotNone(board["platform_actual_scope"])
        self.assertTrue(any(g["type"] == "over_restricted"
                            for g in board["differences"]))

        # 恢复请求落实前不得显示已恢复
        h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "direction": "restore",
            "delivery_id": "d-A-r",
            "scope_json": {"regions": ["east"], "media": ["streaming"],
                           "ranges_ms": [[0, 60000]]}}, h.clock(T_DEC16))
        h.svc.record_acceptance("liaison-A", {"delivery_id": "d-A-r"},
                                h.clock(T_DEC16))
        board = h.svc.publication_board("post-A", h.clock(T_DEC16))
        self.assertIsNotNone(board["platform_actual_scope"])
        self.assertTrue(any(g["type"] == "restore_not_confirmed"
                            for g in board["differences"]))
        # 落实 restored 后实际范围清空、无差异
        h.svc.record_observation("liaison-A", {
            "delivery_id": "d-A-r", "native_state": "restored",
            "platform_revision": 13}, h.clock(T_DEC16))
        board = h.svc.publication_board(
            "post-A", h.clock("2026-12-16T12:30:00+08:00"))
        self.assertIsNone(board["platform_actual_scope"])
        self.assertEqual(board["differences"], [])

    def test_one_owner_withdrawal_cannot_restore_other_owner_scope(self):
        h = self.h
        # n-A 与 n-B 在 south 30-60k 重叠；撤销 A 后，重叠部分仍受 B 保护
        h.svc.revoke_mandate("legal-A", "mandate-A", h.clock(T_DEC8_10))
        with self.assertRaises(StateConflict):
            h.svc.request_delivery("liaison-A", {
                "notice_id": "n-A", "direction": "restore",
                "scope_json": {"regions": ["south"], "media": ["streaming"],
                               "ranges_ms": [[30000, 60000]]}},
                h.clock(T_DEC8_10))
        # 不与 B 重叠的 east 可恢复
        d = h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "direction": "restore",
            "delivery_id": "d-east",
            "scope_json": {"regions": ["east"], "media": ["streaming"],
                           "ranges_ms": [[0, 60000]]}}, h.clock(T_DEC8_10))
        self.assertEqual(d["direction"], "restore")

    def test_no_delivery_without_effective_basis(self):
        h = self.h
        h.svc.revoke_mandate("legal-A", "mandate-A", h.clock(T_DEC8_10))
        with self.assertRaises(StateConflict):
            h.svc.request_delivery("liaison-A", {
                "notice_id": "n-A", "delivery_id": "d-x"},
                h.clock(T_DEC8_10))

    def test_partial_lift_allows_only_lifted_part_restore(self):
        h = self.h
        # 仅保留 n-A：east 全段核准
        h.svc.withdraw_notice("agent-B", "n-B", h.clock(T_DEC2))
        # 上传者就 east 的 [30000,60000) 提异议并被复核解除
        h.svc.create_dispute("liaison-A", {
            "dispute_id": "disp-part", "notice_id": "n-A",
            "uploader_id": "uploader-A", "regions": ["east"],
            "media": ["streaming"], "range_ms": [30000, 60000],
            "license_sha256": "cccc"}, h.clock(T_DEC7))
        h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-part", "valid": True}, h.clock(T_DEC7))
        h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-part", "recipient_id": "agent-A",
            "served_at": T_DEC8_09}, h.clock(T_DEC8_09))
        h.svc.review_dispute("reviewer-A", {
            "dispute_id": "disp-part", "decision": "lift",
            "material_version": "initial"},
            h.clock("2026-12-10T10:00:00+08:00"))
        scope = h.svc.notice_effective_scope(
            "n-A", h.clock("2026-12-10T12:00:00+08:00"))
        # 仍有效部分只剩 [0,30000)
        self.assertEqual(scope.ranges, ((0, 30000),))
        # 恢复仍有效部分被拒
        with self.assertRaises(StateConflict):
            h.svc.request_delivery("liaison-A", {
                "notice_id": "n-A", "direction": "restore",
                "scope_json": {"regions": ["east"], "media": ["streaming"],
                               "ranges_ms": [[0, 30000]]}},
                h.clock("2026-12-10T12:00:00+08:00"))
        # 恢复已解除的 [30000,60000) 允许
        d = h.svc.request_delivery("liaison-A", {
            "notice_id": "n-A", "direction": "restore",
            "delivery_id": "d-part",
            "scope_json": {"regions": ["east"], "media": ["streaming"],
                           "ranges_ms": [[30000, 60000]]}},
            h.clock("2026-12-10T12:00:00+08:00"))
        self.assertEqual(d["direction"], "restore")


if __name__ == "__main__":
    unittest.main()
