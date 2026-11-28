"""从 data/catalog.json 引导合成主数据与演练身份。

只载入资料中的事实（人员、作品、权利、委托、平台、发布项、日历），
不替业务作出任何审批结论；refs 仅表达资料关联。幂等：已存在记录跳过。
平台能力与协作方回放地址来自本地协议约定（见 docs/contracts.md）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .service import Service
from .storage import Store
from .timeutil import Clock

# 协作方协议差异：平台甲支持地区+片段；平台乙只支持整项
PLATFORM_CAPABILITIES = {
    "platform-A": {
        "name": "协作平台甲",
        "granularity": "region_and_segment",
        "supported_regions": ["east", "south", "north", "west"],
        "supported_media": ["streaming"],
    },
    "platform-B": {
        "name": "协作平台乙",
        "granularity": "whole_item",
        "supported_regions": ["east", "south", "north", "west"],
        "supported_media": ["streaming"],
    },
}

ROLE_BY_PRINCIPAL = {
    "agent": "agent",
    "legal": "legal",
    "platform": "liaison",
    "independent-review": "reviewer",
}


def seed(store: Store, catalog_path: str | Path,
         replay_base_url: str | None = None,
         now: str = "2026-12-01T00:00:00+08:00") -> dict:
    svc = Service(store)
    clock = Clock(now)
    catalog = json.loads(Path(catalog_path).read_text())
    records = {r["id"]: r for r in catalog["records"]}
    principals = catalog.get("principals", {})
    actor = "bootstrap"
    created = {"users": 0, "works": 0, "rights": 0, "mandates": 0,
               "platforms": 0, "publications": 0, "calendars": 0}

    # 引导身份（admin），便于首次写入主数据
    if not store.get("SELECT 1 FROM users WHERE id='bootstrap'"):
        store.execute(
            "INSERT INTO users(id,role,display_name,created_at) VALUES(?,?,?,?)",
            ("bootstrap", "admin", "引导账号（演练）", now))
        created["users"] += 1
    # 系统身份：到期自动判定的记录人（不是业务复核员，不参与人工决定）
    if not store.get("SELECT 1 FROM users WHERE id='system'"):
        store.execute(
            "INSERT INTO users(id,role,display_name,created_at) VALUES(?,?,?,?)",
            ("system", "admin", "系统期限判定（演练）", now))
        created["users"] += 1

    # 演练身份：从资料中的委托/通知/异议记录反推角色
    uploader_ids = {r["uploader"] for r in catalog["records"]
                    if r["kind"] == "objection_input"}
    agent_ids = {r["agent"] for r in catalog["records"]
                 if r["kind"] == "mandate"}
    agent_ids |= {r["submitted_by"] for r in catalog["records"]
                  if r["kind"] == "notice_input"}
    for principal_id, role_key in principals.items():
        if store.get("SELECT 1 FROM users WHERE id=?", (principal_id,)):
            continue
        if principal_id in uploader_ids:
            role = "uploader"
        elif principal_id in agent_ids or principal_id.startswith("agent"):
            role = "agent"
        else:
            role = ROLE_BY_PRINCIPAL.get(role_key, "uploader")
        svc.create_user(actor, principal_id, role, principal_id, clock)
        created["users"] += 1

    for rec in catalog["records"]:
        kind = rec["kind"]
        if kind == "work":
            if store.get("SELECT 1 FROM works WHERE id=?", (rec["id"],)):
                continue
            svc.create_work(actor, rec["id"], rec["duration_ms"], clock)
            created["works"] += 1
        elif kind == "rights":
            if store.get("SELECT 1 FROM rights_records WHERE id=?",
                         (rec["id"],)):
                continue
            svc.create_rights(actor, rec["id"], rec["refs"][0], rec["owner"],
                              rec["regions"], rec["media"], rec["start"],
                              rec["end"], clock)
            created["rights"] += 1
        elif kind == "mandate":
            if store.get("SELECT 1 FROM mandates WHERE id=?", (rec["id"],)):
                continue
            svc.create_mandate(actor, rec["id"], rec["refs"][0],
                               rec["agent"], clock)
            created["mandates"] += 1
        elif kind == "calendar":
            svc.upsert_calendar(actor, rec["id"], rec["timezone"],
                                rec["holidays"], rec["make_up_days"], clock)
            created["calendars"] += 1

    # 平台（资料只给了 platform-A/B 标识，能力按协议约定补入）
    for platform_id, caps in PLATFORM_CAPABILITIES.items():
        if store.get("SELECT 1 FROM platforms WHERE id=?", (platform_id,)):
            continue
        svc.create_platform(actor, platform_id, caps["name"],
                            {k: v for k, v in caps.items() if k != "name"},
                            replay_base_url, clock)
        created["platforms"] += 1

    for rec in catalog["records"]:
        if rec["kind"] != "publication":
            continue
        if store.get("SELECT 1 FROM publications WHERE id=?", (rec["id"],)):
            continue
        svc.create_publication(actor, rec["id"], rec["refs"][0],
                               rec["platform"], rec["link"],
                               rec["content_sha256"], rec["range_ms"], clock)
        created["publications"] += 1

    store.commit()
    return created
