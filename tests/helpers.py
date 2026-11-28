"""自动检查公共辅助：独立临时库 + 已播种的合成资料 + 标准演练时钟。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.seed import seed
from app.service import Service
from app.storage import Store
from app.timeutil import Clock

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog.json"

MATERIALS = {
    "rights_file_sha256": "rights-file-1",
    "authorization_file_sha256": "auth-file-1",
    "statement_sha256": "statement-1",
}

# 关键时间点（均带时区，日历见 calendar-A：12-09 假期，12-12 补班）
T_DEC2 = "2026-12-02T10:00:00+08:00"
T_DEC3 = "2026-12-03T10:00:00+08:00"
T_DEC7 = "2026-12-07T10:00:00+08:00"
T_DEC8_09 = "2026-12-08T09:00:00+08:00"
T_DEC8_10 = "2026-12-08T10:00:00+08:00"   # mandate-A 撤销时刻
T_DEC10 = "2026-12-10T12:00:00+08:00"
T_DEC15_18 = "2026-12-15T18:00:00+08:00"  # 五日期限截止当地时刻
T_DEC15_1830 = "2026-12-15T18:30:00+08:00"
T_DEC16 = "2026-12-16T10:00:00+08:00"


class Harness:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "business.sqlite3")
        self.store = Store(self.db_path)
        seed(self.store, CATALOG)
        self.svc = Service(self.store)

    def close(self):
        self.store.close()
        self.tmp.cleanup()

    def clock(self, when: str = T_DEC2) -> Clock:
        return Clock(when)

    def make_case(self, case_id: str = "case-1", when: str = T_DEC2):
        return self.svc.create_case(
            "agent-A", case_id, "film-A", "演练案", "calendar-A",
            self.clock(when))

    def submit(self, notice_id: str, agent: str, mandate: str, publication: str,
               regions, range_ms, when: str, materials=None, media=("streaming",)):
        return self.svc.submit_notice(agent, {
            "case_id": "case-1", "notice_id": notice_id,
            "publication_id": publication, "mandate_id": mandate,
            "regions": list(regions), "media": list(media),
            "range_ms": list(range_ms),
            "materials": materials or dict(MATERIALS),
        }, self.clock(when))

    def approve(self, notice_id: str, regions, range_ms, when: str,
                legal: str = "legal-A", media=("streaming",)):
        return self.svc.legal_decision(legal, {
            "notice_id": notice_id, "decision": "approved",
            "regions": list(regions), "media": list(media),
            "range_ms": list(range_ms),
        }, self.clock(when))
