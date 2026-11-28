"""领域服务：提交、核准、送达跟进、补件、复核与状态查询。

本模块只依据已持久化的本地合成资料作业务状态判断；不抓取真实网站、
不识别侵权内容、不代替法务作法律结论。授权冲突交法务确认，提交人不能自批。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .errors import (
    Conflict,
    Forbidden,
    IncompleteMaterials,
    NotFound,
    PlatformCapability,
    ScopeOutOfMandate,
    StateConflict,
    UnknownDelivery,
    ValidationFailed,
)
from .scopes import Scope, subtract_ranges, union_scopes
from .storage import Store
from .timeutil import Clock, WorkingCalendar, format_dt, parse_dt, to_local

NOTICE_STATUSES = (
    "pending_supplement",  # 材料不全，等待补件
    "submitted",           # 已提交待核准
    "conflict_pending",    # 授权冲突，待法务确认
    "approved",            # 法务已按限定范围核准
    "rejected",            # 法务驳回
    "withdrawn",           # 通知撤回
    "returned",            # 法务退件补正
)

DISPUTE_STATUSES = (
    "pending_validity",    # 待有效性检查（2 个工作日内）
    "invalid",             # 无效异议，须列明缺项，未启动期限
    "awaiting_service",    # 有效，待送达原申请人
    "served",              # 已有效送达，5 个工作日计时中
    "lapsed",              # 逾期未收到继续维持确认，通知失去继续限制依据
    "reviewed",            # 复核员已作决定
    "ended",               # 已结束，重复通知不得重新计时
)

DELIVERY_STATES = (
    "draft",
    "accepted",            # 平台受理（仅证明收到）
    "unknown",             # 受理后连接中断/结果不明，只能查原流水
    "pending",             # 平台处理中
    "restricted",          # 已限制（存在有效依据）
    "restricted_no_basis", # 平台限制回执到达时已无有效依据（迟到回执）
    "partially_restricted",
    "rejected",            # 平台拒绝
    "negotiation",         # 平台能力不支持该粒度，保留待协商
    "restored",            # 已恢复
    "restore_pending",     # 恢复请求已发但尚未落实
    "failed",
)

# 协作方回放外部状态 -> 本服务规范状态；协议差异予以保留并另存 native_state
NATIVE_MAP = {
    "pending": "pending",
    "restricted": "restricted",
    "rejected": "rejected",
    "restored": "restored",
}

ROLE_LEGAL = "legal"
ROLE_AGENT = "agent"
ROLE_LIAISON = "liaison"
ROLE_REVIEWER = "reviewer"
ROLE_UPLOADER = "uploader"
ROLE_ADMIN = "admin"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_dates(values: list[str]):
    from datetime import date
    return [date.fromisoformat(v) for v in values]


def _row(row) -> dict:
    return dict(row) if row is not None else None


def _json_row(row, *json_cols: str) -> dict:
    data = dict(row)
    for col in json_cols:
        if data.get(col) is not None:
            data[col] = json.loads(data[col])
    return data


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _user(self, user_id: str) -> dict:
        row = self.store.get("SELECT * FROM users WHERE id=?", (user_id,))
        if not row:
            raise NotFound(f"用户不存在: {user_id}")
        return _row(row)

    def _require_role(self, user_id: str, *roles: str) -> dict:
        user = self._user(user_id)
        if roles and user["role"] not in roles:
            raise Forbidden(
                f"角色 {user['role']} 无权执行该操作，需要 {'/'.join(roles)}"
            )
        return user

    def _calendar(self, code: str) -> WorkingCalendar:
        row = self.store.get("SELECT * FROM calendars WHERE code=?", (code,))
        if not row:
            raise NotFound(f"工作日历不存在: {code}")
        return WorkingCalendar(
            code=row["code"],
            timezone=row["timezone"],
            holidays=frozenset(_parse_dates(json.loads(row["holidays"]))),
            make_up_days=frozenset(_parse_dates(json.loads(row["make_up_days"]))),
        )

    def _notice(self, notice_id: str) -> dict:
        row = self.store.get("SELECT * FROM notices WHERE id=?", (notice_id,))
        if not row:
            raise NotFound(f"保护请求不存在: {notice_id}")
        return _json_row(row, "scope_json", "materials_json")

    def _dispute(self, dispute_id: str) -> dict:
        row = self.store.get("SELECT * FROM disputes WHERE id=?", (dispute_id,))
        if not row:
            raise NotFound(f"异议不存在: {dispute_id}")
        return _json_row(row, "scope_json")

    def _delivery(self, delivery_id: str) -> dict:
        row = self.store.get("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not row:
            raise NotFound(f"送达流水不存在: {delivery_id}")
        return _json_row(row, "scope_json")

    def _publication(self, publication_id: str) -> dict:
        row = self.store.get("SELECT * FROM publications WHERE id=?", (publication_id,))
        if not row:
            raise NotFound(f"发布项不存在: {publication_id}")
        data = _json_row(row, "range_ms")
        return data

    def _mandate_with_rights(self, mandate_id: str) -> dict:
        row = self.store.get(
            """
            SELECT m.*, r.work_id, r.owner, r.regions AS r_regions, r.media AS r_media,
                   r.start_at AS r_start, r.end_at AS r_end
            FROM mandates m JOIN rights_records r ON r.id = m.rights_id
            WHERE m.id=?
            """,
            (mandate_id,),
        )
        if not row:
            raise NotFound(f"委托不存在: {mandate_id}")
        data = _row(row)
        data["r_regions"] = json.loads(data["r_regions"])
        data["r_media"] = json.loads(data["r_media"])
        return data

    def mandate_scope(self, mandate_id: str, at: Clock) -> Scope | None:
        """委托在 at 时刻仍然有效的授权范围；委托撤销或授权到期则为 None。"""
        m = self._mandate_with_rights(mandate_id)
        now = at.now
        if m["revoked_at"] and parse_dt(m["revoked_at"]) <= now:
            return None
        if not (parse_dt(m["r_start"]) <= now < parse_dt(m["r_end"])):
            return None
        return Scope.create(m["r_regions"], m["r_media"], [0, 10**12])

    # ==================================================================
    # 主数据：人员、作品、权利、委托、平台、发布项、日历
    # ==================================================================
    def create_user(self, actor_id: str, user_id: str, role: str, display_name: str,
                    clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        if self.store.get("SELECT 1 FROM users WHERE id=?", (user_id,)):
            raise Conflict(f"用户已存在: {user_id}")
        self.store.execute(
            "INSERT INTO users(id,role,display_name,created_at) VALUES(?,?,?,?)",
            (user_id, role, display_name, format_dt(clock.now)),
        )
        self.store.event("user_created", "user", user_id, actor_id,
                         format_dt(clock.now), {"role": role})
        return self._user(user_id)

    def create_work(self, actor_id: str, work_id: str, duration_ms: int,
                    clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        if duration_ms <= 0:
            raise ValidationFailed("作品时长必须为正")
        self.store.execute(
            "INSERT INTO works(id,duration_ms,created_at) VALUES(?,?,?)",
            (work_id, duration_ms, format_dt(clock.now)),
        )
        self.store.event("work_created", "work", work_id, actor_id,
                         format_dt(clock.now), {"duration_ms": duration_ms})
        return _row(self.store.get("SELECT * FROM works WHERE id=?", (work_id,)))

    def create_rights(self, actor_id: str, rights_id: str, work_id: str, owner: str,
                      regions: list[str], media: list[str], start_at: str, end_at: str,
                      clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        if not self.store.get("SELECT 1 FROM works WHERE id=?", (work_id,)):
            raise NotFound(f"作品不存在: {work_id}")
        if parse_dt(end_at) <= parse_dt(start_at):
            raise ValidationFailed("授权适用期起点须早于终点（起点含终点不含）")
        self.store.execute(
            "INSERT INTO rights_records(id,work_id,owner,regions,media,start_at,end_at,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (rights_id, work_id, owner, json.dumps(sorted(regions), ensure_ascii=False),
             json.dumps(sorted(media), ensure_ascii=False),
             format_dt(parse_dt(start_at)), format_dt(parse_dt(end_at)),
             format_dt(clock.now)),
        )
        self.store.event("rights_created", "rights", rights_id, actor_id,
                         format_dt(clock.now),
                         {"work_id": work_id, "owner": owner, "regions": regions,
                          "media": media})
        return _json_row(self.store.get(
            "SELECT * FROM rights_records WHERE id=?", (rights_id,)), "regions", "media")

    def create_mandate(self, actor_id: str, mandate_id: str, rights_id: str,
                       agent_id: str, clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        if not self.store.get("SELECT 1 FROM rights_records WHERE id=?", (rights_id,)):
            raise NotFound(f"权利记录不存在: {rights_id}")
        agent = self._user(agent_id)
        if agent["role"] != ROLE_AGENT:
            raise ValidationFailed("受托方必须是代理角色")
        self.store.execute(
            "INSERT INTO mandates(id,rights_id,agent_id,revoked_at) VALUES(?,?,?,NULL)",
            (mandate_id, rights_id, agent_id),
        )
        self.store.event("mandate_created", "mandate", mandate_id, actor_id,
                         format_dt(clock.now),
                         {"rights_id": rights_id, "agent_id": agent_id})
        return _row(self.store.get("SELECT * FROM mandates WHERE id=?", (mandate_id,)))

    def revoke_mandate(self, actor_id: str, mandate_id: str, clock: Clock,
                       reason: str = "") -> dict:
        """撤销委托：历史决定留痕，撤销时刻后该委托不再提供有效依据。"""
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        mandate = self.store.get("SELECT * FROM mandates WHERE id=?", (mandate_id,))
        if not mandate:
            raise NotFound(f"委托不存在: {mandate_id}")
        if mandate["revoked_at"]:
            raise StateConflict("委托已撤销，不能重复撤销")
        self.store.execute(
            "UPDATE mandates SET revoked_at=? WHERE id=?",
            (format_dt(clock.now), mandate_id),
        )
        self.store.event("mandate_revoked", "mandate", mandate_id, actor_id,
                         format_dt(clock.now), {"reason": reason})
        return _row(self.store.get("SELECT * FROM mandates WHERE id=?", (mandate_id,)))

    def create_platform(self, actor_id: str, platform_id: str, name: str,
                        capabilities: dict, replay_base_url: str | None,
                        clock: Clock) -> dict:
        """capabilities 例：{"granularity": "region_and_segment" | "whole_item",
        "supported_regions": [...], "supported_media": [...]}。"""
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        self.store.execute(
            "INSERT INTO platforms(id,name,capabilities,replay_base_url) VALUES(?,?,?,?)",
            (platform_id, name, json.dumps(capabilities, ensure_ascii=False),
             replay_base_url),
        )
        self.store.event("platform_created", "platform", platform_id, actor_id,
                         format_dt(clock.now), capabilities)
        return _json_row(
            self.store.get("SELECT * FROM platforms WHERE id=?", (platform_id,)),
            "capabilities")

    def create_publication(self, actor_id: str, publication_id: str, work_id: str,
                           platform_id: str, link: str, content_sha256: str,
                           range_ms: list[int], clock: Clock) -> dict:
        """发布项由平台、链接标识和内容摘要共同识别。

        相同链接换了内容版本（content_sha256 不同）会生成新的发布项版本，
        原批准不能无条件沿用到新版本。
        """
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL, ROLE_AGENT)
        if not self.store.get("SELECT 1 FROM works WHERE id=?", (work_id,)):
            raise NotFound(f"作品不存在: {work_id}")
        if not self.store.get("SELECT 1 FROM platforms WHERE id=?", (platform_id,)):
            raise NotFound(f"平台不存在: {platform_id}")
        existing = self.store.get(
            "SELECT * FROM publications WHERE platform_id=? AND link=?",
            (platform_id, link),
        )
        version = 1
        if existing:
            if existing["content_sha256"] == content_sha256:
                raise Conflict("相同平台/链接/内容摘要的发布项已存在")
            version = existing["version"] + 1
        self.store.execute(
            "INSERT INTO publications(id,work_id,platform_id,link,content_sha256,range_ms,"
            "version,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (publication_id, work_id, platform_id, link, content_sha256,
             json.dumps(list(range_ms)), version, format_dt(clock.now)),
        )
        self.store.event("publication_created", "publication", publication_id, actor_id,
                         format_dt(clock.now),
                         {"platform_id": platform_id, "link": link,
                          "content_sha256": content_sha256, "version": version})
        return self._publication(publication_id)

    def upsert_calendar(self, actor_id: str, code: str, timezone: str,
                        holidays: list[str], make_up_days: list[str],
                        clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_ADMIN, ROLE_LEGAL)
        WorkingCalendar(code, timezone, frozenset(_parse_dates(holidays)),
                        frozenset(_parse_dates(make_up_days)))
        self.store.execute(
            "INSERT INTO calendars(code,timezone,holidays,make_up_days) VALUES(?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET timezone=excluded.timezone,"
            " holidays=excluded.holidays, make_up_days=excluded.make_up_days",
            (code, timezone, json.dumps(sorted(holidays)),
             json.dumps(sorted(make_up_days))),
        )
        self.store.event("calendar_upserted", "calendar", code, actor_id,
                         format_dt(clock.now),
                         {"timezone": timezone, "holidays": holidays,
                          "make_up_days": make_up_days})
        return self._calendar(code).to_dict()

    def create_case(self, actor_id: str, case_id: str, work_id: str, title: str,
                    calendar_code: str, clock: Clock) -> dict:
        self._require_role(actor_id, ROLE_AGENT, ROLE_ADMIN, ROLE_LEGAL)
        if not self.store.get("SELECT 1 FROM works WHERE id=?", (work_id,)):
            raise NotFound(f"作品不存在: {work_id}")
        self._calendar(calendar_code)
        self.store.execute(
            "INSERT INTO cases(id,work_id,title,calendar_code,created_by,created_at,status)"
            " VALUES(?,?,?,?,?,?,?)",
            (case_id, work_id, title, calendar_code, actor_id,
             format_dt(clock.now), "open"),
        )
        self.store.event("case_created", "case", case_id, actor_id,
                         format_dt(clock.now),
                         {"work_id": work_id, "title": title,
                          "calendar_code": calendar_code})
        return _row(self.store.get("SELECT * FROM cases WHERE id=?", (case_id,)))

    # ==================================================================
    # 提交、补件、撤回与法务核准
    # ==================================================================
    REQUIRED_MATERIAL_KEYS = ("rights_file_sha256", "authorization_file_sha256",
                              "statement_sha256")

    def _check_materials(self, materials: dict) -> list[str]:
        missing = []
        for key in self.REQUIRED_MATERIAL_KEYS:
            value = materials.get(key)
            if not isinstance(value, str) or not value.strip():
                missing.append(key)
        return missing

    def submit_notice(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """权利代理提交保护请求。

        一个投诉可含多个发布项（逐发布项建立请求）；同一发布项可收到不同
        权利方的通知。材料不全先补件；授权冲突交法务确认；提交人不能自批。
        """
        actor = self._require_role(actor_id, ROLE_AGENT)
        case_id = body["case_id"]
        publication_id = body["publication_id"]
        mandate_id = body["mandate_id"]
        case = self.store.get("SELECT * FROM cases WHERE id=?", (case_id,))
        if not case:
            raise NotFound(f"投诉不存在: {case_id}")
        publication = self._publication(publication_id)
        mandate = self._mandate_with_rights(mandate_id)
        if mandate["agent_id"] != actor_id:
            raise Forbidden("代理只能使用委托给本人的授权提交请求")
        if mandate["work_id"] != publication["work_id"]:
            raise ValidationFailed("委托权利所属作品与发布项作品不一致")

        scope = Scope.create(body["regions"], body["media"], body["range_ms"])
        pub_range = tuple(publication["range_ms"])
        # 申请的每个片段都须落在发布项片段范围内
        overshoot = [
            rng for rng in scope.ranges
            if subtract_ranges([rng], [pub_range])]
        if overshoot:
            raise ScopeOutOfMandate("申请片段超出该发布项的片段范围")

        materials = body.get("materials", {}) or {}
        missing = self._check_materials(materials)
        mandate_scope = self.mandate_scope(mandate_id, clock)
        conflict_note = None
        conflict_pending = 0
        if mandate_scope is None:
            conflict_pending = 1
            conflict_note = "委托已撤销或授权不在适用期内，须法务确认"
        elif not mandate_scope.contains(scope):
            conflict_pending = 1
            conflict_note = (
                "申请的地区/媒介/时间超出有效委托范围，须法务确认；"
                "片方拥有部分权利不意味着可要求全球全媒介下架"
            )

        if missing:
            status = "pending_supplement"
        elif conflict_pending:
            status = "conflict_pending"
        else:
            status = "submitted"

        notice_id = body.get("notice_id") or _new_id("notice")
        self.store.execute(
            "INSERT INTO notices(id,case_id,publication_id,mandate_id,submitter_id,"
            "scope_json,status,materials_json,conflict_pending,conflict_note,"
            "withdrawn_at,current_decision_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)",
            (notice_id, case_id, publication_id, mandate_id, actor_id,
             json.dumps(scope.to_dict(), ensure_ascii=False), status,
             json.dumps(materials, ensure_ascii=False), conflict_pending,
             conflict_note, format_dt(clock.now), format_dt(clock.now)),
        )
        self.store.event("notice_submitted", "notice", notice_id, actor_id,
                         format_dt(clock.now),
                         {"case_id": case_id, "publication_id": publication_id,
                          "mandate_id": mandate_id, "scope": scope.to_dict(),
                          "missing_materials": missing,
                          "conflict_pending": bool(conflict_pending),
                          "conflict_note": conflict_note})
        return self._notice(notice_id)

    def supplement_notice(self, actor_id: str, notice_id: str, body: dict,
                          clock: Clock) -> dict:
        """补件：材料补齐后重新校验，仍按实际提交时刻判断委托效力。"""
        actor = self._require_role(actor_id, ROLE_AGENT)
        notice = self._notice(notice_id)
        if notice["submitter_id"] != actor_id:
            raise Forbidden("只有原提交代理可以补件")
        if notice["status"] not in ("pending_supplement", "returned"):
            raise StateConflict(f"请求当前状态 {notice['status']} 不允许补件")
        materials = {**notice["materials_json"], **(body.get("materials", {}) or {})}
        note = body.get("note", "")
        missing = self._check_materials(materials)
        self.store.execute(
            "INSERT INTO notice_supplements(id,notice_id,materials_json,note,"
            "submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
            (_new_id("supp"), notice_id,
             json.dumps(materials, ensure_ascii=False), note, actor_id,
             format_dt(clock.now)),
        )
        self.store.event("notice_supplemented", "notice", notice_id, actor_id,
                         format_dt(clock.now),
                         {"materials": materials, "note": note})
        if missing:
            new_status = "pending_supplement"
        else:
            mandate_scope = self.mandate_scope(notice["mandate_id"], clock)
            scope = _scope_from_json(notice["scope_json"])
            if notice["conflict_pending"] or mandate_scope is None or \
                    not mandate_scope.contains(scope):
                new_status = "conflict_pending"
            else:
                new_status = "submitted"
        self._update_notice_status(notice_id, new_status, clock, materials=materials)
        return self._notice(notice_id)

    def withdraw_notice(self, actor_id: str, notice_id: str, clock: Clock,
                        reason: str = "") -> dict:
        """撤回通知；已限制内容是否可恢复需另行判断是否仍有独立有效依据。"""
        actor = self._require_role(actor_id, ROLE_AGENT, ROLE_LEGAL)
        notice = self._notice(notice_id)
        if actor["role"] == ROLE_AGENT and notice["submitter_id"] != actor_id:
            raise Forbidden("代理只能撤回本人提交的请求")
        if notice["status"] == "withdrawn":
            raise StateConflict("请求已撤回")
        self.store.execute(
            "UPDATE notices SET withdrawn_at=?, status='withdrawn', updated_at=? WHERE id=?",
            (format_dt(clock.now), format_dt(clock.now), notice_id),
        )
        self.store.event("notice_withdrawn", "notice", notice_id, actor_id,
                         format_dt(clock.now), {"reason": reason})
        return self._notice(notice_id)

    def legal_decision(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """法务按具体发布项及限制范围核准；提交人不能自批。"""
        actor = self._require_role(actor_id, ROLE_LEGAL)
        notice_id = body["notice_id"]
        notice = self._notice(notice_id)
        if notice["submitter_id"] == actor_id:
            raise Forbidden("提交人不能自批自己的保护请求")
        decision = body["decision"]
        if decision not in ("approved", "rejected", "returned", "conflict_resolved"):
            raise ValidationFailed("未知法务决定类型")
        if decision == "approved":
            if notice["status"] not in ("submitted", "conflict_pending",
                                       "pending_supplement", "returned"):
                raise StateConflict(f"请求当前状态 {notice['status']} 不能核准")
            if notice["status"] == "pending_supplement":
                raise IncompleteMaterials("材料尚未补齐，不能核准")
            approved_scope = Scope.create(body["regions"], body["media"],
                                          body["range_ms"])
            requested = _scope_from_json(notice["scope_json"])
            if not requested.contains(approved_scope):
                raise ScopeOutOfMandate(
                    "核准范围不得超出请求范围：不能把预告片片段核准扩展为整部作品"
                )
            # 冲突待确认时，法务必须显式确认缩小后的有效范围
            mandate_scope = self.mandate_scope(notice["mandate_id"], clock)
            if mandate_scope is not None and not mandate_scope.contains(approved_scope):
                raise ScopeOutOfMandate(
                    "法务核准范围仍超出当前有效委托；如确认另有依据请先走冲突确认"
                )
            scope_json = json.dumps(approved_scope.to_dict(), ensure_ascii=False)
            new_status = "approved"
            conflict_ruling = None
        elif decision == "rejected":
            scope_json = None
            new_status = "rejected"
            conflict_ruling = None
        elif decision == "returned":
            scope_json = None
            new_status = "returned"
            conflict_ruling = None
        else:  # conflict_resolved
            if not notice["conflict_pending"]:
                raise StateConflict("该请求不存在待确认的授权冲突")
            ruling = body.get("conflict_ruling")
            if ruling not in ("uphold_limited", "deny"):
                raise ValidationFailed(
                    "冲突确认须给出 conflict_ruling: uphold_limited 或 deny"
                )
            if ruling == "uphold_limited":
                approved_scope = Scope.create(body["regions"], body["media"],
                                              body["range_ms"])
                requested = _scope_from_json(notice["scope_json"])
                if not requested.contains(approved_scope):
                    raise ScopeOutOfMandate("冲突确认的范围不得超出原请求范围")
                scope_json = json.dumps(approved_scope.to_dict(), ensure_ascii=False)
                new_status = "approved"
            else:
                scope_json = None
                new_status = "rejected"
            conflict_ruling = ruling

        decision_id = _new_id("ldec")
        self.store.execute(
            "INSERT INTO legal_decisions(id,notice_id,decision,scope_json,"
            "conflict_ruling,decided_by,decided_at,note) VALUES(?,?,?,?,?,?,?,?)",
            (decision_id, notice_id, decision, scope_json, conflict_ruling,
             actor_id, format_dt(clock.now), body.get("note", "")),
        )
        self.store.execute(
            "UPDATE notices SET status=?, conflict_pending=0, conflict_note=NULL,"
            " current_decision_id=?, updated_at=? WHERE id=?",
            (new_status, decision_id, format_dt(clock.now), notice_id),
        )
        self.store.event("legal_decided", "notice", notice_id, actor_id,
                         format_dt(clock.now),
                         {"decision_id": decision_id, "decision": decision,
                          "scope": json.loads(scope_json) if scope_json else None,
                          "conflict_ruling": conflict_ruling,
                          "note": body.get("note", "")})
        return _json_row(
            self.store.get("SELECT * FROM legal_decisions WHERE id=?",
                           (decision_id,)),
            "scope_json")

    def _update_notice_status(self, notice_id: str, status: str, clock: Clock,
                              materials: dict | None = None) -> None:
        if materials is not None:
            self.store.execute(
                "UPDATE notices SET status=?, materials_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(materials, ensure_ascii=False),
                 format_dt(clock.now), notice_id),
            )
        else:
            self.store.execute(
                "UPDATE notices SET status=?, updated_at=? WHERE id=?",
                (status, format_dt(clock.now), notice_id),
            )

    # ==================================================================
    # 上传者异议：接收、有效性检查、送达、补件、复核、期限
    # ==================================================================
    def create_dispute(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """联络人只能接收材料；是否中止/维持/解除由独立复核员决定。"""
        actor = self._require_role(actor_id, ROLE_LIAISON, ROLE_UPLOADER)
        notice_id = body["notice_id"]
        notice = self._notice(notice_id)
        if notice["status"] != "approved":
            raise StateConflict("只有已核准的请求才能被提出异议")
        uploader_id = body.get("uploader_id") or actor_id
        uploader = self._user(uploader_id)
        if uploader["role"] != ROLE_UPLOADER:
            raise ValidationFailed("异议主体必须是上传者身份")
        scope = Scope.create(body["regions"], body.get("media", _scope_from_json(
            notice["scope_json"]).media and sorted(_scope_from_json(
            notice["scope_json"]).media)), body["range_ms"])
        notice_scope = _scope_from_json(notice["scope_json"])
        if not notice_scope.contains(scope):
            raise ScopeOutOfMandate(
                "异议范围不得超出被异议请求的核准范围；单项异议只影响被主张的"
                "内容和地区，不能撤掉同案其他发布项"
            )
        case = self.store.get("SELECT * FROM cases WHERE id=?",
                              (notice["case_id"],))
        cal = self._calendar(case["calendar_code"])
        dispute_id = body.get("dispute_id") or _new_id("disp")
        self.store.execute(
            "INSERT INTO disputes(id,notice_id,uploader_id,scope_json,license_sha256,"
            "status,received_at,calendar_code,validity_due_at,validity_note,"
            "validity_checked_by,valid_served_at,lapse_due_at,lapse_at,created_at,ended_at)"
            " VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,?,NULL)",
            (dispute_id, notice_id, uploader_id,
             json.dumps(scope.to_dict(), ensure_ascii=False),
             body.get("license_sha256"), "pending_validity",
             format_dt(clock.now), cal.code, format_dt(clock.now)),
        )
        self.store.event("dispute_received", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"notice_id": notice_id, "uploader_id": uploader_id,
                          "scope": scope.to_dict(),
                          "license_sha256": body.get("license_sha256")})
        return self._dispute(dispute_id)

    def check_dispute_validity(self, actor_id: str, body: dict,
                               clock: Clock) -> dict:
        """两个工作日内完成材料有效性检查；无效异议列明缺项，不启动期限。"""
        actor = self._require_role(actor_id, ROLE_REVIEWER)
        dispute_id = body["dispute_id"]
        dispute = self._dispute(dispute_id)
        if dispute["status"] not in ("pending_validity", "invalid"):
            raise StateConflict("该异议已完成有效性检查")
        cal = self._calendar(dispute["calendar_code"])
        validity_due = cal.deadline(parse_dt(dispute["received_at"]), 2)
        valid = bool(body.get("valid"))
        note = body.get("note", "")
        if valid:
            missing: list[str] = []
            status = "awaiting_service"
        else:
            missing = list(body.get("missing_items") or [])
            if not missing:
                raise ValidationFailed("无效异议必须说明缺项 missing_items")
            status = "invalid"
        self.store.execute(
            "UPDATE disputes SET status=?, validity_note=?, validity_checked_by=?,"
            " validity_due_at=? WHERE id=?",
            (status, note if valid else json.dumps(
                {"note": note, "missing_items": missing}, ensure_ascii=False),
             actor_id, format_dt(validity_due), dispute_id),
        )
        self.store.event("dispute_validity_checked", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"valid": valid, "missing_items": missing,
                          "validity_due_at": format_dt(validity_due),
                          "overdue": clock.now > validity_due, "note": note})
        return self._dispute(dispute_id)

    def supplement_dispute(self, actor_id: str, body: dict,
                           clock: Clock) -> dict:
        """补齐材料后重新按实际有效送达计算；此前无效不视为已启动期限。"""
        actor = self._require_role(actor_id, ROLE_UPLOADER, ROLE_LIAISON)
        dispute_id = body["dispute_id"]
        dispute = self._dispute(dispute_id)
        if dispute["status"] not in ("invalid", "pending_validity"):
            raise StateConflict("仅待有效性检查或被标记无效的异议可以补件")
        materials = body.get("materials", {}) or {}
        missing = list(body.get("missing_items_remaining") or [])
        self.store.execute(
            "INSERT INTO dispute_supplements(id,dispute_id,materials_json,"
            "missing_items,submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
            (_new_id("dsup"), dispute_id,
             json.dumps(materials, ensure_ascii=False),
             json.dumps(missing, ensure_ascii=False), actor_id,
             format_dt(clock.now)),
        )
        self.store.event("dispute_supplemented", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"materials": materials, "missing_items": missing})
        if not missing:
            # 补齐后重新进入待送达，有效期从后续实际送达重新起算
            self.store.execute(
                "UPDATE disputes SET status='awaiting_service' WHERE id=?",
                (dispute_id,),
            )
        return self._dispute(dispute_id)

    def serve_dispute(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """有效异议送达原申请人；未送达不能开始倒计时。"""
        actor = self._require_role(actor_id, ROLE_LIAISON)
        dispute_id = body["dispute_id"]
        dispute = self._dispute(dispute_id)
        if dispute["status"] not in ("awaiting_service", "served"):
            raise StateConflict("只有通过有效性检查的异议可以送达")
        recipient_id = body["recipient_id"]
        notice = self._notice(dispute["notice_id"])
        if recipient_id != notice["submitter_id"]:
            raise ValidationFailed("有效异议应送达原申请人（通知提交代理）")
        served_ok = bool(body.get("served", True))
        service_id = _new_id("dsvc")
        if not served_ok:
            self.store.execute(
                "INSERT INTO dispute_services(id,dispute_id,recipient_id,served_at,"
                "attempted_at,ok) VALUES(?,?,?,NULL,?,0)",
                (service_id, dispute_id, recipient_id, format_dt(clock.now)),
            )
            self.store.event("dispute_service_attempted", "dispute", dispute_id,
                             actor_id, format_dt(clock.now),
                             {"recipient_id": recipient_id, "served": False})
            return self._dispute(dispute_id)
        if dispute["status"] == "served":
            raise StateConflict("异议已有效送达，不能重复送达重新计时")
        cal = self._calendar(dispute["calendar_code"])
        served_at = parse_dt(body.get("served_at") or clock.now)
        lapse_due = cal.deadline(served_at, 5)
        self.store.execute(
            "INSERT INTO dispute_services(id,dispute_id,recipient_id,served_at,"
            "attempted_at,ok) VALUES(?,?,?,?,?,1)",
            (service_id, dispute_id, recipient_id, format_dt(served_at),
             format_dt(clock.now)),
        )
        self.store.execute(
            "UPDATE disputes SET status='served', valid_served_at=?, lapse_due_at=?"
            " WHERE id=?",
            (format_dt(served_at), format_dt(lapse_due), dispute_id),
        )
        self.store.event("dispute_served", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"recipient_id": recipient_id,
                          "served_at": format_dt(served_at),
                          "lapse_due_at": format_dt(lapse_due)})
        return self._dispute(dispute_id)

    def record_continuation(self, actor_id: str, body: dict,
                            clock: Clock) -> dict:
        """原申请人补交继续维持文件；每次有效补交顺延五个工作日。"""
        actor = self._require_role(actor_id, ROLE_AGENT, ROLE_LIAISON)
        dispute_id = body["dispute_id"]
        dispute = self._dispute(dispute_id)
        if dispute["status"] != "served":
            raise StateConflict("只有已有效送达且计时中的异议可以补交继续维持文件")
        notice = self._notice(dispute["notice_id"])
        if actor["role"] == ROLE_AGENT and notice["submitter_id"] != actor_id:
            raise Forbidden("继续维持文件须由原申请人提交")
        materials = body.get("materials", {}) or {}
        if not materials:
            raise ValidationFailed("继续维持文件内容不能为空")
        cal = self._calendar(dispute["calendar_code"])
        new_due = cal.deadline(clock.now, 5)
        cont_id = _new_id("cont")
        self.store.execute(
            "INSERT INTO continuations(id,dispute_id,materials_json,submitted_by,"
            "received_at,extends_lapse_due_to) VALUES(?,?,?,?,?,?)",
            (cont_id, dispute_id, json.dumps(materials, ensure_ascii=False),
             actor_id, format_dt(clock.now), format_dt(new_due)),
        )
        self.store.execute(
            "UPDATE disputes SET lapse_due_at=? WHERE id=?",
            (format_dt(new_due), dispute_id),
        )
        self.store.event("continuation_recorded", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"continuation_id": cont_id,
                          "extends_lapse_due_to": format_dt(new_due),
                          "materials": materials})
        return _json_row(
            self.store.get("SELECT * FROM continuations WHERE id=?", (cont_id,)),
            "materials_json")

    def review_dispute(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """没有参与原核准的复核员独立决定中止、维持或解除限制。"""
        actor = self._require_role(actor_id, ROLE_REVIEWER)
        dispute_id = body["dispute_id"]
        dispute = self._dispute(dispute_id)
        if dispute["status"] not in ("served", "lapsed"):
            raise StateConflict("该异议状态不允许复核决定")
        if dispute["status"] == "lapsed":
            # 到期后复核员可确认到期事实或作进一步处理；已结束异议不得改判
            pass
        notice = self._notice(dispute["notice_id"])
        approvers = {
            r["decided_by"] for r in self.store.all(
                "SELECT decided_by FROM legal_decisions WHERE notice_id=?",
                (notice["id"],))
        }
        if actor_id in approvers:
            raise Forbidden("参与过原核准的人员不得担任该异议复核员")
        decision = body["decision"]
        if decision not in ("maintain", "suspend", "lift", "restore"):
            raise ValidationFailed("复核决定须为 maintain/suspend/lift/restore")
        material_version = body.get("material_version", "initial")
        review_scope = None
        if body.get("regions") and body.get("range_ms"):
            review_scope = Scope.create(body["regions"], body.get(
                "media", sorted(_scope_from_json(dispute["scope_json"]).media)),
                body["range_ms"])
            dispute_scope = _scope_from_json(dispute["scope_json"])
            if not dispute_scope.contains(review_scope):
                raise ScopeOutOfMandate("复核范围不得超出异议主张范围")
        review_id = _new_id("rdec")
        self.store.execute(
            "INSERT INTO review_decisions(id,dispute_id,decision,scope_json,"
            "decided_by,decided_at,material_version,note) VALUES(?,?,?,?,?,?,?,?)",
            (review_id, dispute_id, decision,
             json.dumps(review_scope.to_dict(), ensure_ascii=False)
             if review_scope else None, actor_id, format_dt(clock.now),
             material_version, body.get("note", "")),
        )
        self.store.execute(
            "UPDATE disputes SET status='ended', ended_at=? WHERE id=?",
            (format_dt(clock.now), dispute_id),
        )
        self.store.event("dispute_reviewed", "dispute", dispute_id, actor_id,
                         format_dt(clock.now),
                         {"review_id": review_id, "decision": decision,
                          "scope": review_scope.to_dict() if review_scope else None,
                          "material_version": material_version,
                          "note": body.get("note", "")})
        return _json_row(
            self.store.get("SELECT * FROM review_decisions WHERE id=?",
                           (review_id,)),
            "scope_json")

    def evaluate_timeouts(self, clock: Clock) -> list[dict]:
        """到期自动判定：五日期限内未收到继续维持确认，通知失去继续限制依据。

        原申请人在期限到达前刚补交文件的，以 continuations 顺延的期限为准。
        """
        results = []
        rows = self.store.all(
            "SELECT * FROM disputes WHERE status='served'")
        for row in rows:
            dispute = _json_row(row, "scope_json")
            due = parse_dt(dispute["lapse_due_at"])
            if clock.now <= due:
                continue
            review_id = _new_id("rdec")
            self.store.execute(
                "INSERT INTO review_decisions(id,dispute_id,decision,scope_json,"
                "decided_by,decided_at,material_version,note) VALUES(?,?,?,"
                "NULL,?,?,'auto-lapse',?)",
                (review_id, dispute["id"], "lapsed", "system",
                 format_dt(clock.now),
                 "有效异议送达后五个工作日未收到继续维持确认，相应通知失去继续限制依据"),
            )
            self.store.execute(
                "UPDATE disputes SET status='lapsed', lapse_at=? WHERE id=?",
                (format_dt(clock.now), dispute["id"]),
            )
            self.store.event("dispute_lapsed", "dispute", dispute["id"], "system",
                             format_dt(clock.now),
                             {"lapse_due_at": dispute["lapse_due_at"],
                              "review_id": review_id})
            results.append(self._dispute(dispute["id"]))
        return results

    # ==================================================================
    # 有效依据判定引擎
    # ==================================================================
    def notice_effective_scope(self, notice_id: str, at: Clock) -> Scope | None:
        """判明某请求在 at 时刻仍可作为限制依据的范围。

        依据链：法务核准（按具体发布项与限定范围）→ 委托在该时刻仍有效 →
        通知未撤回 → 异议复核/到期对范围的削减。授权撤销、通知撤回或
        异议到期后，相应依据失效；不同权利方的依据相互独立。
        """
        notice = self._notice(notice_id)
        if notice["status"] not in ("approved",):
            return None
        decision = self.store.get(
            "SELECT * FROM legal_decisions WHERE id=?",
            (notice["current_decision_id"],))
        if not decision or decision["decision"] not in ("approved",
                                                        "conflict_resolved") \
                or not decision["scope_json"]:
            return None
        scope = _scope_from_json(json.loads(decision["scope_json"]))
        # 委托撤销 / 授权期间
        mandate = self._mandate_with_rights(notice["mandate_id"])
        now = at.now
        if mandate["revoked_at"] and parse_dt(mandate["revoked_at"]) <= now:
            return None
        if not (parse_dt(mandate["r_start"]) <= now < parse_dt(mandate["r_end"])):
            return None
        # 异议对该依据范围的影响（按时间戳作时点判定，保证历史时点可复算）
        for drow in self.store.all(
                "SELECT * FROM disputes WHERE notice_id=? ORDER BY id",
                (notice_id,)):
            dispute = _json_row(drow, "scope_json")
            dscope = _scope_from_json(dispute["scope_json"])
            served_at = parse_dt(dispute["valid_served_at"]) \
                if dispute["valid_served_at"] else None
            # 未有效送达不开始倒计时，依据维持
            if served_at is None or now < served_at:
                continue
            # 到期时刻（以 lapse_at 实际判定为准，否则按计划期限）
            lapsed_at = parse_dt(dispute["lapse_at"]) \
                if dispute["lapse_at"] else parse_dt(dispute["lapse_due_at"])
            reviews = self.store.all(
                "SELECT * FROM review_decisions WHERE dispute_id=? ORDER BY id",
                (dispute["id"],))
            decided = [r for r in reviews
                       if parse_dt(r["decided_at"]) <= now
                       and r["decision"] != "lapsed"]
            if not decided:
                # 尚无人工复核：到期即削减，且到期不可被事后复核复活
                if now >= lapsed_at:
                    scope = self._cut_scope(scope, dscope)
                continue
            review = decided[-1]
            outcome = review["decision"]
            rscope = _scope_from_json(json.loads(review["scope_json"])) \
                if review["scope_json"] else dscope
            # 到期已发生后，维持决定不能恢复已失去的依据
            if now >= lapsed_at and outcome == "maintain":
                scope = self._cut_scope(scope, dscope)
            elif outcome in ("lift", "suspend", "lapsed"):
                scope = self._cut_scope(scope, rscope)
            elif outcome == "restore":
                # 复核决定恢复（此前确曾被中止/解除）：不再削减
                continue
            # maintain 且未到期：维持，不削减
        return None if scope is None or scope.is_empty() else scope

    @staticmethod
    def _cut_scope(base: Scope, removed: Scope) -> Scope | None:
        result = base.subtract(removed)
        return result

    def active_bases(self, publication_id: str, at: Clock) -> list[dict]:
        """同一发布项上各权利方在 at 时刻仍有效的限制依据（并集前逐请求列示）。"""
        rows = self.store.all(
            "SELECT n.* FROM notices n WHERE n.publication_id=?",
            (publication_id,))
        out = []
        for row in rows:
            notice = _json_row(row, "scope_json", "materials_json")
            scope = self.notice_effective_scope(notice["id"], at)
            if scope is not None:
                mandate = self._mandate_with_rights(notice["mandate_id"])
                out.append({
                    "notice_id": notice["id"],
                    "submitter_id": notice["submitter_id"],
                    "owner": mandate["owner"],
                    "mandate_id": notice["mandate_id"],
                    "effective_scope": scope.to_dict(),
                })
        return out

    def required_scope(self, publication_id: str, at: Clock) -> Scope | None:
        """当前应当向平台要求限制的范围 = 全部有效依据的并集。"""
        scopes = [_scope_from_json(b["effective_scope"])
                  for b in self.active_bases(publication_id, at)]
        return union_scopes(scopes)

    # ==================================================================
    # 送达跟进：受理/执行/拒绝/恢复状态保留平台差异
    # ==================================================================
    def _platform(self, platform_id: str) -> dict:
        row = self.store.get("SELECT * FROM platforms WHERE id=?", (platform_id,))
        if not row:
            raise NotFound(f"平台不存在: {platform_id}")
        return _json_row(row, "capabilities")

    def request_delivery(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """联络人向合作平台发起限制/恢复送达。

        - 尚未核准（无有效依据）的请求不得发送有效处置。
        - 平台能力不支持所申请粒度时保留待协商，不无授权扩大范围。
        - 受理成功只证明收到，不表示限制已生效。
        """
        actor = self._require_role(actor_id, ROLE_LIAISON)
        notice_id = body["notice_id"]
        direction = body.get("direction", "restrict")
        if direction not in ("restrict", "restore"):
            raise ValidationFailed("direction 须为 restrict 或 restore")
        notice = self._notice(notice_id)
        publication = self._publication(notice["publication_id"])
        platform = self._platform(publication["platform_id"])
        # 法务核准绑定的是具体发布项；相同链接换内容版本后原批准不能沿用
        if body.get("publication_id") and \
                body["publication_id"] != notice["publication_id"]:
            raise StateConflict(
                "该请求的核准绑定发布项 "
                f"{notice['publication_id']}，不能沿用到 "
                f"{body['publication_id']}（相同链接的新版本须另行申请核准）"
            )
        active = self.notice_effective_scope(notice_id, clock)
        if direction == "restrict":
            if active is None:
                raise StateConflict(
                    "该请求当前没有有效限制依据，不能向合作平台发送有效处置"
                )
            scope = active
            caps = platform["capabilities"]
            if caps.get("granularity") == "whole_item":
                # 平台乙只支持整项限制；不能无授权扩大范围
                raise PlatformCapability(
                    "该平台只支持整项限制而申请为部分地区/片段，保留待协商",
                    details={"platform_id": platform["id"],
                             "requested_scope": scope.to_dict()})
            supported_regions = set(caps.get("supported_regions") or [])
            if supported_regions and not scope.regions.issubset(supported_regions):
                raise PlatformCapability(
                    "平台不支持所申请地区，无法执行，保留待协商",
                    details={"unsupported": sorted(
                        scope.regions - supported_regions)})
        else:
            # 连接中断期间不得另发相反请求覆盖未知结果（优先于一切恢复判断）
            unknown = self.store.all(
                "SELECT * FROM deliveries WHERE notice_id=? AND state='unknown'",
                (notice_id,))
            if unknown:
                raise UnknownDelivery(
                    "存在结果不明的送达流水，必须先查询原流水，不能另发相反请求"
                )
            scope = _scope_from_json(body["scope_json"]) if body.get(
                "scope_json") else _scope_from_json(notice["scope_json"])
            # 红线一：恢复范围不得与本请求当前仍有效的依据重叠
            if active is not None and scope.intersect(active) is not None:
                raise StateConflict(
                    "恢复范围与本请求仍有效的限制依据重叠；只能就经复核解除或"
                    "依据已失效的部分请求恢复",
                    details={"active_scope": active.to_dict(),
                             "requested_restore": scope.to_dict()})
            # 红线二：不得因一位权利人撤诉/撤销，恢复另一位权利人仍有效保护的范围
            other_union = union_scopes([
                _scope_from_json(b["effective_scope"])
                for b in self.active_bases(publication["id"], clock)
                if b["notice_id"] != notice_id])
            if other_union is not None and scope.intersect(other_union) \
                    is not None:
                raise StateConflict(
                    "恢复范围与其他权利方仍有效的独立保护依据重叠，"
                    "只能就不重叠部分请求恢复，不得覆盖他人有效保护",
                    details={"other_active_scope": other_union.to_dict(),
                             "requested_restore": scope.to_dict()})
        delivery_id = body.get("delivery_id") or _new_id("dlv")
        initial = "draft"
        self.store.execute(
            "INSERT INTO deliveries(id,notice_id,publication_id,platform_id,"
            "command_id,direction,scope_json,state,basis_active_at_request,"
            "requested_by,requested_at,last_observed_at,last_revision,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
            (delivery_id, notice_id, publication["id"], platform["id"],
             body.get("command_id"), direction,
             json.dumps(scope.to_dict(), ensure_ascii=False), initial,
             1 if direction == "restrict" else 0, actor_id,
             format_dt(clock.now), body.get("note", ""))
        )
        self.store.event("delivery_requested", "delivery", delivery_id, actor_id,
                         format_dt(clock.now),
                         {"notice_id": notice_id, "direction": direction,
                          "scope": scope.to_dict(),
                          "command_id": body.get("command_id")})
        return self._delivery(delivery_id)

    def register_negotiation(self, actor_id: str, body: dict,
                             clock: Clock) -> dict:
        """平台能力不支持申请粒度时登记待协商，不无授权扩大范围。"""
        self._require_role(actor_id, ROLE_LIAISON, ROLE_LEGAL)
        notice_id = body["notice_id"]
        notice = self._notice(notice_id)
        publication = self._publication(notice["publication_id"])
        platform = self._platform(publication["platform_id"])
        # 法务核准绑定的是具体发布项；相同链接换内容版本后原批准不能沿用
        if body.get("publication_id") and \
                body["publication_id"] != notice["publication_id"]:
            raise StateConflict(
                "该请求的核准绑定发布项 "
                f"{notice['publication_id']}，不能沿用到 "
                f"{body['publication_id']}（相同链接的新版本须另行申请核准）"
            )
        active = self.notice_effective_scope(notice_id, clock)
        if active is None:
            raise StateConflict("该请求当前没有有效限制依据，不能登记处置")
        delivery_id = body.get("delivery_id") or _new_id("dlv")
        self.store.execute(
            "INSERT INTO deliveries(id,notice_id,publication_id,platform_id,"
            "command_id,direction,scope_json,state,basis_active_at_request,"
            "requested_by,requested_at,last_observed_at,last_revision,note)"
            " VALUES(?,?,?,?,?, 'restrict', ?, 'negotiation', 1, ?, ?, ?, NULL, ?)",
            (delivery_id, notice_id, publication["id"], platform["id"],
             body.get("command_id"),
             json.dumps(active.to_dict(), ensure_ascii=False), actor_id,
             format_dt(clock.now), format_dt(clock.now),
             body.get("note", "平台协议不支持该限制粒度，保留待协商"))
        )
        self._append_event(delivery_id, "negotiation",
                           body.get("native_state", "capability_unsupported"),
                           None, clock,
                           body.get("note", "平台协议不支持该限制粒度，保留待协商"),
                           basis_active=1)
        return self._delivery(delivery_id)

    def record_acceptance(self, actor_id: str, body: dict,
                          clock: Clock) -> dict:
        """记录平台受理（HTTP 202）。受理仅证明收到，不表示限制生效。"""
        self._require_role(actor_id, ROLE_LIAISON)
        delivery = self._delivery(body["delivery_id"])
        new_state = "accepted"
        self.store.execute(
            "UPDATE deliveries SET state=?, last_observed_at=? WHERE id=?",
            (new_state, format_dt(clock.now), delivery["id"]))
        self._append_event(delivery["id"], new_state, body.get("native_state"),
                           body.get("platform_revision"), clock,
                           body.get("note", "平台已受理，受理不代表已生效"),
                           basis_active=1)
        return self._delivery(delivery["id"])

    def record_unknown(self, actor_id: str, body: dict, clock: Clock) -> dict:
        """受理后连接中断（如 504）：标记 unknown，只能继续查询原流水。"""
        self._require_role(actor_id, ROLE_LIAISON)
        delivery = self._delivery(body["delivery_id"])
        self.store.execute(
            "UPDATE deliveries SET state='unknown', last_observed_at=? WHERE id=?",
            (format_dt(clock.now), delivery["id"]))
        self._append_event(delivery["id"], "unknown",
                           body.get("native_state", "gateway_timeout"),
                           body.get("platform_revision"), clock,
                           body.get("note", "响应不明，协作方可能已受理，"
                                            "继续查询原发布项与处置流水"),
                           basis_active=1)
        return self._delivery(delivery["id"])

    def record_observation(self, actor_id: str, body: dict,
                           clock: Clock) -> dict:
        """记录对原流水的查询观测；迟到回执不能把内容重新限制。"""
        self._require_role(actor_id, ROLE_LIAISON)
        delivery = self._delivery(body["delivery_id"])
        native = body["native_state"]
        canonical = NATIVE_MAP.get(native, native)
        basis_active_now = self.notice_effective_scope(
            delivery["notice_id"], clock) is not None
        recorded_state = canonical
        note = body.get("note", "")
        if canonical == "restricted" and not basis_active_now:
            recorded_state = "restricted_no_basis"
            note = (note + "；平台限制回执到达时已无有效依据（迟到回执），"
                           "不能重新形成限制，应发起恢复").strip("；")
        elif canonical == "rejected":
            recorded_state = "rejected"
        elif canonical == "restored":
            recorded_state = "restored"
        self.store.execute(
            "UPDATE deliveries SET state=?, last_observed_at=?, last_revision=?,"
            " note=? WHERE id=?",
            (recorded_state, format_dt(clock.now),
             body.get("platform_revision"), note, delivery["id"]))
        self._append_event(delivery["id"], recorded_state, native,
                           body.get("platform_revision"), clock, note,
                           basis_active=1 if basis_active_now else 0)
        return self._delivery(delivery["id"])

    def record_manual_event(self, actor_id: str, body: dict,
                            clock: Clock) -> dict:
        """无回放协议时按平台真实回执手工登记（状态差异原样保留）。"""
        self._require_role(actor_id, ROLE_LIAISON)
        delivery = self._delivery(body["delivery_id"])
        canonical = body["state"]
        if canonical not in DELIVERY_STATES:
            raise ValidationFailed(f"未知规范状态: {canonical}")
        basis_active = self.notice_effective_scope(
            delivery["notice_id"], clock) is not None
        self.store.execute(
            "UPDATE deliveries SET state=?, last_observed_at=?, last_revision=?,"
            " note=? WHERE id=?",
            (canonical, format_dt(clock.now), body.get("platform_revision"),
             body.get("note", ""), delivery["id"]))
        self._append_event(delivery["id"], canonical, body.get("native_state"),
                           body.get("platform_revision"), clock,
                           body.get("note", ""),
                           basis_active=1 if basis_active else 0)
        return self._delivery(delivery["id"])

    def _append_event(self, delivery_id: str, state: str, native_state,
                      revision, clock: Clock, note: str,
                      basis_active: int) -> None:
        self.store.execute(
            "INSERT INTO delivery_events(delivery_id,observed_state,native_state,"
            "platform_revision,observed_at,recorded_at,note,basis_active)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (delivery_id, state, native_state, revision,
             format_dt(clock.now), format_dt(clock.now), note, basis_active))
        self.store.event("delivery_observed", "delivery", delivery_id, None,
                         format_dt(clock.now),
                         {"state": state, "native_state": native_state,
                          "revision": revision, "basis_active": bool(
                              basis_active), "note": note})

    def _delivery_state_at(self, delivery: dict, at: Clock) -> str:
        """按观测历史重建该流水在 at 时刻的状态；无观测回落到建档状态。"""
        rows = self.store.all(
            "SELECT observed_state, observed_at FROM delivery_events"
            " WHERE delivery_id=? AND observed_at<=? ORDER BY id",
            (delivery["id"], format_dt(at.now)))
        if not rows:
            return delivery["state"] if parse_dt(
                delivery["requested_at"]) <= at.now else "not_created"
        return rows[-1]["observed_state"]

    # ==================================================================
    # 看板与追溯
    # ==================================================================
    def publication_board(self, publication_id: str, clock: Clock) -> dict:
        """一条发布链接：应限制范围、平台实际范围、差异与继续处理责任人。"""
        publication = self._publication(publication_id)
        platform = self._platform(publication["platform_id"])
        required = self.required_scope(publication_id, clock)
        deliveries = [_json_row(r, "scope_json") for r in self.store.all(
            "SELECT * FROM deliveries WHERE publication_id=? ORDER BY id",
            (publication_id,))]
        # 平台实际范围：按各流水在 clock 时刻的状态求净效果。
        # - 恢复未落实（accepted/pending/unknown/restore_pending）不扣除实际范围；
        # - 恢复已落实（restored）才扣除；
        # - restricted_no_basis 表示平台仍限制但本服务已无依据，仍计入“实际范围”，
        #   从而在差异中暴露为过度限制，直到恢复落实。
        actual_parts: list[Scope] = []
        restored_parts: list[Scope] = []
        restore_pending: list[dict] = []
        unknown_flows: list[dict] = []
        negotiation_flows: list[dict] = []
        for d in deliveries:
            state_at = self._delivery_state_at(d, clock)
            if state_at == "not_created":
                continue
            dscope = _scope_from_json(d["scope_json"])
            if d["direction"] == "restore":
                if state_at == "restored":
                    restored_parts.append(dscope)
                elif state_at in ("restore_pending", "accepted", "pending",
                                  "unknown", "draft"):
                    restore_pending.append(d)
                continue
            # 限制流水
            if state_at == "unknown":
                unknown_flows.append(d)
                continue
            if state_at == "negotiation":
                negotiation_flows.append(d)
                continue
            if state_at in ("restricted", "partially_restricted",
                            "restricted_no_basis"):
                actual_parts.append(dscope)
        actual = union_scopes(actual_parts)
        restored = union_scopes(restored_parts)
        if actual and restored:
            actual = actual.subtract(restored)
        required_dict = required.to_dict() if required else None
        actual_dict = actual.to_dict() if actual and not actual.is_empty() else None
        # 差异
        gaps: list[dict] = []
        if required and actual:
            missing = required.subtract(actual)
            if missing:
                gaps.append({"type": "under_restricted",
                             "scope": missing.to_dict(),
                             "owner": "liaison",
                             "action": "继续向平台送达/跟进执行"})
            extra = actual.subtract(required)
            if extra:
                gaps.append({"type": "over_restricted",
                             "scope": extra.to_dict(),
                             "owner": "liaison",
                             "action": "依据已失效，发起恢复并跟进落实"})
        elif required and not actual:
            gaps.append({"type": "under_restricted",
                         "scope": required.to_dict(), "owner": "liaison",
                         "action": "尚未生效，继续跟进送达"})
        elif not required and actual:
            gaps.append({"type": "over_restricted",
                         "scope": actual.to_dict(), "owner": "liaison",
                         "action": "已无有效依据，发起恢复"})
        for d in restore_pending:
            gaps.append({"type": "restore_not_confirmed",
                         "delivery_id": d["id"], "scope": d["scope_json"],
                         "owner": "liaison",
                         "action": "恢复请求尚未落实，不能显示已恢复"})
        for d in unknown_flows:
            gaps.append({"type": "unknown_result",
                         "delivery_id": d["id"], "scope": d["scope_json"],
                         "owner": "liaison",
                         "action": "继续查询原发布项与处置流水，不得另发相反请求"})
        for d in negotiation_flows:
            gaps.append({"type": "capability_negotiation",
                         "delivery_id": d["id"], "scope": d["scope_json"],
                         "owner": "legal",
                         "action": "平台协议不支持该限制粒度，保留待协商"})
        return {
            "publication_id": publication_id,
            "platform_id": platform["id"],
            "link": publication["link"],
            "content_sha256": publication["content_sha256"],
            "version": publication["version"],
            "at": format_dt(clock.now),
            "required_scope": required_dict,
            "platform_actual_scope": actual_dict,
            "active_bases": self.active_bases(publication_id, clock),
            "differences": gaps,
            "deliveries": [self._delivery_summary(d["id"]) for d in deliveries],
        }

    def _delivery_summary(self, delivery_id: str) -> dict:
        d = self._delivery(delivery_id)
        events = self.store.all(
            "SELECT observed_state,native_state,platform_revision,observed_at,"
            "basis_active,note FROM delivery_events WHERE delivery_id=? ORDER BY id",
            (delivery_id,))
        d["events"] = [dict(e) for e in events]
        return d

    def notice_trace(self, notice_id: str) -> dict:
        """从一次法务决定追到送达、异议和后续动作。"""
        notice = self._notice(notice_id)
        decisions = [_json_row(r, "scope_json") for r in self.store.all(
            "SELECT * FROM legal_decisions WHERE notice_id=? ORDER BY id",
            (notice_id,))]
        deliveries = [self._delivery_summary(r["id"]) for r in self.store.all(
            "SELECT id FROM deliveries WHERE notice_id=? ORDER BY id",
            (notice_id,))]
        disputes = []
        for r in self.store.all(
                "SELECT id FROM disputes WHERE notice_id=? ORDER BY id",
                (notice_id,)):
            d = self._dispute(r["id"])
            d["supplements"] = [dict(x) for x in self.store.all(
                "SELECT * FROM dispute_supplements WHERE dispute_id=? ORDER BY id",
                (r["id"],))]
            d["services"] = [dict(x) for x in self.store.all(
                "SELECT * FROM dispute_services WHERE dispute_id=? ORDER BY id",
                (r["id"],))]
            d["reviews"] = [_json_row(x, "scope_json") for x in self.store.all(
                "SELECT * FROM review_decisions WHERE dispute_id=? ORDER BY id",
                (r["id"],))]
            d["continuations"] = [_json_row(x, "materials_json") for x in
                                  self.store.all(
                "SELECT * FROM continuations WHERE dispute_id=? ORDER BY id",
                (r["id"],))]
            disputes.append(d)
        return {
            "notice": notice,
            "legal_decisions": decisions,
            "deliveries": deliveries,
            "disputes": disputes,
            "event_log": self.store.history("notice", notice_id),
        }


def _scope_from_json(data: dict) -> Scope:
    return Scope.from_dict(data)

