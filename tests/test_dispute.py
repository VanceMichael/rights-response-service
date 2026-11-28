"""异议：有效性检查两日、继续维持五日、未送达不计时、无效列明缺项、复核独立性。"""
import unittest

from app.errors import Forbidden, StateConflict
from app.timeutil import parse_dt
from tests.helpers import (Harness, T_DEC2, T_DEC7, T_DEC8_09, T_DEC15_18,
                           T_DEC15_1830, T_DEC16)


def build_approved_dispute(h: Harness) -> str:
    """构造：n-B 已核准 + 上传者已提出异议，返回 dispute_id。"""
    h.make_case()
    h.submit("n-B", "agent-B", "mandate-B", "post-A",
             ["south"], (30000, 60000), T_DEC2)
    h.approve("n-B", ["south"], (30000, 60000), T_DEC2)
    h.svc.create_dispute("liaison-A", {
        "dispute_id": "disp-1", "notice_id": "n-B",
        "uploader_id": "uploader-A", "regions": ["south"],
        "media": ["streaming"], "range_ms": [30000, 60000],
        "license_sha256": "cccc"}, h.clock(T_DEC7))
    return "disp-1"


class DisputeCalendarTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_validity_window_two_workdays(self):
        build_approved_dispute(self.h)
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-1", "valid": True, "note": "材料齐全"},
            self.h.clock(T_DEC7))
        dispute = self.h.svc._dispute("disp-1")
        # 12-07（周一）后两个工作日：08、10（09 假期），截止 10 日 18:00
        self.assertEqual(parse_dt(dispute["validity_due_at"]),
                         parse_dt("2026-12-10T18:00:00+08:00"))

    def test_invalid_must_state_missing_and_not_start_clock(self):
        build_approved_dispute(self.h)
        with self.assertRaises(Exception):
            self.h.svc.check_dispute_validity("reviewer-A", {
                "dispute_id": "disp-1", "valid": False}, self.h.clock(T_DEC7))
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-1", "valid": False,
            "missing_items": ["许可文件不清晰"]}, self.h.clock(T_DEC7))
        # 无效异议不能送达，不开始倒计时
        with self.assertRaises(StateConflict):
            self.h.svc.serve_dispute("liaison-A", {
                "dispute_id": "disp-1", "recipient_id": "agent-B"},
                self.h.clock(T_DEC7))
        # 补齐后重新进入待送达
        self.h.svc.supplement_dispute("uploader-A", {
            "dispute_id": "disp-1",
            "materials": {"license_sha256": "cccc"},
            "missing_items_remaining": []}, self.h.clock(T_DEC7))
        self.assertEqual(self.h.svc._dispute("disp-1")["status"],
                         "awaiting_service")

    def test_five_workday_lapse_and_unserved_no_clock(self):
        build_approved_dispute(self.h)
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-1", "valid": True}, self.h.clock(T_DEC7))
        # 送达前即使超过五日也不到期
        self.h.svc.evaluate_timeouts(self.h.clock(T_DEC16))
        self.assertEqual(self.h.svc._dispute("disp-1")["status"],
                         "awaiting_service")
        # 12-08 09:00 有效送达
        self.h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-1", "recipient_id": "agent-B",
            "served_at": T_DEC8_09}, self.h.clock(T_DEC8_09))
        dispute = self.h.svc._dispute("disp-1")
        # 五个工作日：09 假期跳过 → 10、11、12（补班）、14、15，截止 15 日 18:00
        self.assertEqual(parse_dt(dispute["lapse_due_at"]),
                         parse_dt(T_DEC15_18))
        # 截止前一刻不到期
        self.assertEqual(self.h.svc.evaluate_timeouts(
            self.h.clock(T_DEC15_18)), [])
        # 截止后到期，通知失去继续限制依据
        self.h.svc.evaluate_timeouts(self.h.clock(T_DEC15_1830))
        self.assertEqual(self.h.svc._dispute("disp-1")["status"], "lapsed")
        self.assertIsNone(self.h.svc.notice_effective_scope(
            "n-B", self.h.clock(T_DEC15_1830)))

    def test_continuation_extends_deadline(self):
        build_approved_dispute(self.h)
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-1", "valid": True}, self.h.clock(T_DEC7))
        self.h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-1", "recipient_id": "agent-B",
            "served_at": T_DEC8_09}, self.h.clock(T_DEC8_09))
        # 原申请人在期限内补交继续维持，顺延五个工作日
        self.h.svc.record_continuation("agent-B", {
            "dispute_id": "disp-1",
            "materials": {"continuation_file_sha256": "keep"}},
            self.h.clock("2026-12-14T10:00:00+08:00"))
        # 自 12-14（周一）起，计数日为次日起的五个工作日：
        # 15（二）、16（三）、17（四）、18（五）、21（一，跳过 19/20 周末）
        self.assertEqual(
            parse_dt(self.h.svc._dispute("disp-1")["lapse_due_at"]),
            parse_dt("2026-12-21T18:00:00+08:00"))
        # 原截止时刻不再到期
        self.assertEqual(self.h.svc.evaluate_timeouts(
            self.h.clock(T_DEC15_1830)), [])
        self.assertIsNotNone(self.h.svc.notice_effective_scope(
            "n-B", self.h.clock(T_DEC15_1830)))

    def test_reviewer_must_be_independent_of_approver(self):
        build_approved_dispute(self.h)
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-1", "valid": True}, self.h.clock(T_DEC7))
        self.h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-1", "recipient_id": "agent-B",
            "served_at": T_DEC8_09}, self.h.clock(T_DEC8_09))
        # legal-A 参与了原核准，不能复核
        with self.assertRaises(Forbidden):
            self.h.svc.review_dispute("legal-A", {
                "dispute_id": "disp-1", "decision": "lift"},
                self.h.clock(T_DEC16))
        # 独立复核员可以
        self.h.svc.review_dispute("reviewer-A", {
            "dispute_id": "disp-1", "decision": "maintain",
            "material_version": "initial"}, self.h.clock(
            "2026-12-10T10:00:00+08:00"))
        self.assertEqual(self.h.svc._dispute("disp-1")["status"], "ended")
        # 已结束异议不能重新计时/改判
        with self.assertRaises(StateConflict):
            self.h.svc.review_dispute("reviewer-A", {
                "dispute_id": "disp-1", "decision": "lift"},
                self.h.clock(T_DEC16))

    def test_single_dispute_scoped_to_claimed_content(self):
        # 单项异议只影响被主张范围，不动同案其他发布项
        self.h.make_case()
        self.h.submit("n-B", "agent-B", "mandate-B", "post-A",
                      ["south"], (30000, 60000), T_DEC2)
        self.h.approve("n-B", ["south"], (30000, 60000), T_DEC2)
        # 构造一个更窄异议
        self.h.svc.create_dispute("liaison-A", {
            "dispute_id": "disp-narrow", "notice_id": "n-B",
            "uploader_id": "uploader-A", "regions": ["south"],
            "media": ["streaming"], "range_ms": [30000, 45000],
            "license_sha256": "cccc"}, self.h.clock(T_DEC7))
        self.h.svc.check_dispute_validity("reviewer-A", {
            "dispute_id": "disp-narrow", "valid": True}, self.h.clock(T_DEC7))
        self.h.svc.serve_dispute("liaison-A", {
            "dispute_id": "disp-narrow", "recipient_id": "agent-B",
            "served_at": T_DEC8_09}, self.h.clock(T_DEC8_09))
        self.h.svc.evaluate_timeouts(self.h.clock(T_DEC15_1830))
        scope = self.h.svc.notice_effective_scope(
            "n-B", self.h.clock(T_DEC15_1830))
        # 仅 [30000,45000) 被削减，[45000,60000) 仍是有效依据
        self.assertEqual(scope.ranges, ((45000, 60000),))


if __name__ == "__main__":
    unittest.main()
