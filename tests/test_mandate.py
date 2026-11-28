"""授权、委托与提交规则：部分权利不等于全球下架、材料补件、冲突交法务、不能自批。"""
import unittest

from app.errors import (Conflict, Forbidden, IncompleteMaterials,
                        ScopeOutOfMandate, StateConflict)
from tests.helpers import Harness, MATERIALS, T_DEC2, T_DEC8_10


class MandateScopeTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.h.make_case()

    def tearDown(self):
        self.h.close()

    def test_partial_rights_cannot_cover_world(self):
        # mandate-A 仅 east/south 的 streaming；申请 north 或全媒介须转冲突
        notice = self.h.submit(
            "n1", "agent-A", "mandate-A", "post-A",
            ["east", "north"], (0, 60000), T_DEC2)
        self.assertEqual(notice["status"], "conflict_pending")
        self.assertIn("委托", notice["conflict_note"])

    def test_in_scope_submission_is_ready(self):
        notice = self.h.submit(
            "n1", "agent-A", "mandate-A", "post-A",
            ["east", "south"], (0, 60000), T_DEC2)
        self.assertEqual(notice["status"], "submitted")

    def test_missing_materials_go_to_supplement(self):
        notice = self.h.submit(
            "n1", "agent-A", "mandate-A", "post-A",
            ["east"], (0, 60000), T_DEC2,
            materials={"rights_file_sha256": "x"})
        self.assertEqual(notice["status"], "pending_supplement")
        # 材料不全不能核准
        with self.assertRaises(IncompleteMaterials):
            self.h.approve("n1", ["east"], (0, 60000), T_DEC2)
        # 补件后进入待核准
        self.h.svc.supplement_notice("agent-A", "n1", {
            "materials": {"authorization_file_sha256": "a",
                          "statement_sha256": "s"}}, self.h.clock(T_DEC2))
        self.assertEqual(self.h.svc._notice("n1")["status"], "submitted")
    def test_agent_cannot_self_approve(self):
        self.h.submit("n1", "agent-A", "mandate-A", "post-A",
                      ["east"], (0, 60000), T_DEC2)
        with self.assertRaises(Forbidden):
            self.h.svc.legal_decision("agent-A", {
                "notice_id": "n1", "decision": "approved",
                "regions": ["east"], "media": ["streaming"],
                "range_ms": [0, 60000]}, self.h.clock(T_DEC2))

    def test_approval_cannot_expand_beyond_request(self):
        self.h.submit("n1", "agent-A", "mandate-A", "post-A",
                      ["east"], (0, 30000), T_DEC2)
        with self.assertRaises(ScopeOutOfMandate):
            # 预告片片段不得扩展为整部/更大范围
            self.h.approve("n1", ["east"], (0, 60000), T_DEC2)

    def test_agent_may_only_use_own_mandate(self):
        with self.assertRaises(Forbidden):
            self.h.submit("n1", "agent-A", "mandate-B", "post-A",
                          ["south"], (30000, 60000), T_DEC2)

    def test_revoked_mandate_blocks_effective_scope(self):
        self.h.submit("n1", "agent-A", "mandate-A", "post-A",
                      ["east"], (0, 60000), T_DEC2)
        self.h.approve("n1", ["east"], (0, 60000), T_DEC2)
        self.assertIsNotNone(
            self.h.svc.notice_effective_scope("n1", self.h.clock(T_DEC2)))
        self.h.svc.revoke_mandate("legal-A", "mandate-A",
                                  self.h.clock(T_DEC8_10))
        self.assertIsNone(
            self.h.svc.notice_effective_scope("n1", self.h.clock(T_DEC8_10)))
        # 撤销前的历史时点仍可复算为有效
        self.assertIsNotNone(
            self.h.svc.notice_effective_scope("n1", self.h.clock(T_DEC2)))

    def test_duplicate_revoke_rejected(self):
        self.h.svc.revoke_mandate("legal-A", "mandate-A",
                                  self.h.clock(T_DEC8_10))
        with self.assertRaises(StateConflict):
            self.h.svc.revoke_mandate("legal-A", "mandate-A",
                                      self.h.clock(T_DEC8_10))


if __name__ == "__main__":
    unittest.main()
