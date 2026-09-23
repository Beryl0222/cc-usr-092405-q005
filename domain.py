"""跨文化内容共制领域核心。

把原始 IP、文化元素、改编提案、地区权利、译审意见、素材确认、境外团队
素材领取、工时核算、跨时区交付与分市场发行连成一棵版本关系树，并强制：

* 文化顾问可把元素标记为受限（按市场）或禁用，复核与发行闸门据此判定；
* 版权方按市场和授权期限授予权利，过期或撤回即失效；
* 境外团队只能领到该版本目标市场已确权且已确认的素材，其余扣留并说明；
* 演员/音乐等素材未确认、权利不覆盖目标市场的版本不得发行；
* 返工产生新版本，原版本工时一经核算即锁定，只能追加、不能改写；
* 同一内容哈希跨时区重复交付只入账一次；
* 发行必须同时通过权利、文化复核、当地分级三关；
* 撤权准确阻断未发布版本、下架已上线副本，并列出受影响渠道。
"""

from datetime import date, datetime, timezone
from itertools import count

# ---- 稳定枚举（与 fixtures/domain.json 保持一致语义） -----------------------

ELEMENT_ALLOWED = "allowed"          # 可使用
ELEMENT_RESTRICTED = "restricted"    # 受限：仅在受限市场清单中不允许出现
ELEMENT_FORBIDDEN = "forbidden"      # 禁用：所有市场
ELEMENT_STANCES = (ELEMENT_ALLOWED, ELEMENT_RESTRICTED, ELEMENT_FORBIDDEN)

PROPOSAL_PROPOSED = "提案中"
PROPOSAL_ACCEPTED = "已采纳"
PROPOSAL_REJECTED = "已驳回"

ASSET_UNCONFIRMED = "unconfirmed"
ASSET_CONFIRMED = "confirmed"
ASSET_REJECTED = "rejected"
ASSET_DECISIONS = (ASSET_UNCONFIRMED, ASSET_CONFIRMED, ASSET_REJECTED)

GRANT_ACTIVE = "active"
GRANT_WITHDRAWN = "withdrawn"

ASSIGNMENT_ACTIVE = "active"
ASSIGNMENT_RELEASED = "released"

DELIVERY_ACCEPTED = "accepted"
DELIVERY_DUPLICATE = "duplicate"

REVIEW_PASSED = "passed"
REVIEW_FAILED = "failed"
REVIEW_PENDING = "pending"

# 版本生命周期状态
V_IN_PRODUCTION = "制作中"
V_REVIEW_FAILED = "复核未过"
V_READY = "就绪"
V_RELEASED = "已发行"
V_BLOCKED = "已阻断"       # 授权撤回等原因导致未发布版本被阻断
V_TAKEN_DOWN = "已下线"    # 曾上线，因撤权等原因下架

DEP_BLOCKED = "blocked"
DEP_ONLINE = "online"
DEP_TAKEN_DOWN = "taken_down"


class DomainError(Exception):
    """所有业务规则冲突的基类，HTTP 层据此映射状态码。"""

    http_status = 400

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    http_status = 404


class ConflictError(DomainError):
    http_status = 409


def _today():
    return date.today().isoformat()


def _parse_instant(value):
    """把带时区的交付时间规范化为 UTC 的 datetime，用于跨时区去重。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise DomainError("交付时间必须带时区偏移，例如 2026-09-21T10:00:00+04:00")
    return parsed.astimezone(timezone.utc)


class Domain:
    """内存版领域存储；正式部署可替换为持久化实现，规则不变。"""

    def __init__(self, now=None):
        self._now = now or _today
        self._seq = count(1)
        self.ips = {}
        self.elements = {}              # element_key 在同一 IP 内唯一
        self.proposals = {}
        self.versions = {}
        self.licenses = {}
        self.grants = {}
        self.teams = {}
        self.assets = {}                # (version_id, asset_key)
        self.assignments = {}
        self.worklogs = {}
        self.deliveries = {}            # (version_id, content_hash)
        self.reviews = {}               # version_id -> 最新复核
        self.ratings = {}               # (version_id, market)
        self.channels = {}
        self.deployments = {}
        self.withdrawals = []

    # ---- 工具 -------------------------------------------------------------

    def _id(self, prefix):
        return f"{prefix}_{next(self._seq):04d}"

    def now(self):
        value = self._now()
        return value if isinstance(value, str) else value.isoformat()

    def _get(self, store, key, label):
        try:
            return store[key]
        except KeyError:
            raise NotFoundError(f"{label}不存在: {key}")

    def _version_ip(self, version):
        return self.proposals[version["proposal_id"]]["ip_id"]

    # ---- 原始 IP 与文化元素 ----------------------------------------------

    def register_ip(self, title, origin="", note=""):
        ip = {
            "id": self._id("ip"),
            "title": title,
            "origin": origin,
            "note": note,
            "created_at": self.now(),
        }
        self.ips[ip["id"]] = ip
        return ip

    def add_cultural_element(self, ip_id, key, name, stance=ELEMENT_ALLOWED,
                             restricted_markets=None, note="", reviewer=None):
        if ip_id not in self.ips:
            raise NotFoundError(f"IP不存在: {ip_id}")
        if stance not in ELEMENT_STANCES:
            raise DomainError(f"未知元素立场: {stance}")
        if stance == ELEMENT_RESTRICTED and not restricted_markets:
            raise DomainError("受限元素必须给出受限市场清单")
        if (ip_id, key) in self.elements:
            raise ConflictError(f"文化元素已存在: {key}")
        element = {
            "id": self._id("ele"),
            "ip_id": ip_id,
            "key": key,
            "name": name,
            "stance": stance,
            "restricted_markets": sorted(restricted_markets or []),
            "note": note,
            "reviewer": reviewer,
            "updated_at": self.now(),
        }
        self.elements[(ip_id, key)] = element
        return element

    def set_element_stance(self, ip_id, key, stance, restricted_markets=None,
                           note="", reviewer=None):
        """文化顾问随时可收紧（或放宽）元素口径，已上线判断以最新口径为准。"""
        element = self._get(self.elements, (ip_id, key), "文化元素")
        if stance not in ELEMENT_STANCES:
            raise DomainError(f"未知元素立场: {stance}")
        if stance == ELEMENT_RESTRICTED and not restricted_markets:
            raise DomainError("受限元素必须给出受限市场清单")
        element["stance"] = stance
        element["restricted_markets"] = sorted(restricted_markets or [])
        if note:
            element["note"] = note
        element["reviewer"] = reviewer or element["reviewer"]
        element["updated_at"] = self.now()
        for version in self.versions.values():
            if self._version_ip(version) == ip_id and key in version["elements"]:
                self._refresh_status(version)
        return element

    # ---- 改编提案与版本 ---------------------------------------------------

    def create_proposal(self, ip_id, title, summary="", creator=None,
                        target_markets=None):
        if ip_id not in self.ips:
            raise NotFoundError(f"IP不存在: {ip_id}")
        markets = sorted(set(target_markets or []))
        if not markets:
            raise DomainError("改编提案必须至少面向一个市场")
        proposal = {
            "id": self._id("prop"),
            "ip_id": ip_id,
            "title": title,
            "summary": summary,
            "creator": creator,
            "target_markets": markets,
            "status": PROPOSAL_PROPOSED,
            "decision_note": "",
            "created_at": self.now(),
        }
        self.proposals[proposal["id"]] = proposal
        return proposal

    def decide_proposal(self, proposal_id, decision, note="", reviewer=None):
        proposal = self._get(self.proposals, proposal_id, "改编提案")
        if decision not in (PROPOSAL_ACCEPTED, PROPOSAL_REJECTED):
            raise DomainError("提案决议只能是 已采纳 或 已驳回")
        proposal["status"] = decision
        proposal["decision_note"] = note
        proposal["reviewer"] = reviewer
        proposal["decided_at"] = self.now()
        return proposal

    def create_version(self, proposal_id, code, market, team_id=None,
                       parent_id=None, note=""):
        proposal = self._get(self.proposals, proposal_id, "改编提案")
        if proposal["status"] != PROPOSAL_ACCEPTED:
            raise ConflictError("只有已采纳的提案才能切出成片版本")
        if market not in proposal["target_markets"]:
            raise DomainError(f"市场 {market} 不在提案授权目标范围内",
                              details={"target_markets": proposal["target_markets"]})
        if team_id is not None and team_id not in self.teams:
            raise NotFoundError(f"制片团队不存在: {team_id}")
        if parent_id is not None and parent_id not in self.versions:
            raise NotFoundError(f"返工母版不存在: {parent_id}")
        version = {
            "id": self._id("ver"),
            "proposal_id": proposal_id,
            "code": code,
            "market": market,
            "team_id": team_id,
            "parent_id": parent_id,          # 返工链：指向上一版
            "note": note,
            "elements": [],                  # 本版使用的文化元素 key
            "status": V_IN_PRODUCTION,
            "created_at": self.now(),
        }
        self.versions[version["id"]] = version
        return version

    def use_element(self, version_id, element_key):
        version = self._get(self.versions, version_id, "版本")
        ip_id = self._version_ip(version)
        element = self._get(self.elements, (ip_id, element_key), "文化元素")
        if element_key in version["elements"]:
            return version
        if element["stance"] == ELEMENT_FORBIDDEN:
            raise ConflictError(
                f"元素 {element['name']} 已被文化顾问禁用，不得进入任何版本",
                details={"element": element_key})
        version["elements"].append(element_key)
        self._refresh_status(version)
        return version

    def remove_element(self, version_id, element_key):
        version = self._get(self.versions, version_id, "版本")
        if element_key in version["elements"]:
            version["elements"].remove(element_key)
            self._refresh_status(version)
        return version

    # ---- 版权授权（按市场、按期限） ---------------------------------------

    def register_license(self, ip_id, licensor, scope="改编与发行", note="",
                         coverage="ip"):
        """登记授权合同。

        coverage="ip"：该 IP 的每个版本发行都要求该授权方在目标市场有效；
        coverage="asset"：仅当成片使用了该授权方的素材（按 licensor 匹配）
        时才要求，例如只覆盖部分地区的音乐。
        """
        if ip_id not in self.ips:
            raise NotFoundError(f"IP不存在: {ip_id}")
        if coverage not in ("ip", "asset"):
            raise DomainError("授权覆盖类型只能是 ip 或 asset")
        license_ = {
            "id": self._id("lic"),
            "ip_id": ip_id,
            "licensor": licensor,
            "scope": scope,
            "note": note,
            "coverage": coverage,
            "created_at": self.now(),
        }
        self.licenses[license_["id"]] = license_
        return license_

    def add_grant(self, license_id, market, starts, expires, share_ratio=None,
                  currency="", note=""):
        license_ = self._get(self.licenses, license_id, "授权合同")
        if expires < starts:
            raise DomainError("授权到期日不能早于起始日")
        for grant in self.grants.values():
            if (grant["license_id"] == license_id and grant["market"] == market
                    and grant["status"] == GRANT_ACTIVE
                    and not (expires < grant["starts"] or starts > grant["expires"])):
                raise ConflictError(
                    f"{license_['licensor']} 在 {market} 的授权期限重叠",
                    details={"existing_grant": grant["id"]})
        grant = {
            "id": self._id("grant"),
            "license_id": license_id,
            "ip_id": license_["ip_id"],
            "licensor": license_["licensor"],
            "market": market,
            "starts": starts,
            "expires": expires,
            "share_ratio": share_ratio,    # 收益分成比例，发行时冻结快照
            "currency": currency,
            "note": note,
            "status": GRANT_ACTIVE,
        }
        self.grants[grant["id"]] = grant
        return grant

    def active_grants(self, ip_id, market, at=None):
        at = at or self.now()
        result = []
        for grant in self.grants.values():
            if (grant["ip_id"] == ip_id and grant["market"] == market
                    and grant["status"] == GRANT_ACTIVE
                    and grant["starts"] <= at <= grant["expires"]):
                result.append(grant)
        return result

    def withdraw_grant(self, grant_id, reason="", at=None):
        """授权撤回：阻断未发布版本、下架已上线副本、收回团队素材访问权。"""
        grant = self._get(self.grants, grant_id, "授权")
        if grant["status"] != GRANT_ACTIVE:
            raise ConflictError("授权已处于撤回状态")
        at = at or self.now()
        grant["status"] = GRANT_WITHDRAWN
        grant["withdrawn_at"] = at
        grant["withdraw_reason"] = reason

        affected_versions = [
            v for v in self.versions.values()
            if self._version_ip(v) == grant["ip_id"] and v["market"] == grant["market"]
        ]
        blocked_unpublished, taken_down, released_assignments = [], [], []

        for version in affected_versions:
            online = [d for d in self.deployments.values()
                      if d["version_id"] == version["id"] and d["status"] == DEP_ONLINE]
            if online:
                for dep in online:
                    dep["status"] = DEP_TAKEN_DOWN
                    dep["taken_down_at"] = at
                    dep["takedown_reason"] = f"授权撤回: {reason}".strip(": ")
                    channel = self.channels[dep["channel_code"]]
                    taken_down.append({
                        "deployment_id": dep["id"],
                        "version_id": version["id"],
                        "version_code": version["code"],
                        "market": grant["market"],
                        "channel_code": dep["channel_code"],
                        "channel_name": channel["name"],
                        "released_at": dep["released_at"],
                        "content_hash": dep.get("content_hash"),
                    })
            else:
                # 未发布版本：该市场全部已登记渠道都处于不可发行状态
                planned = [d for d in self.deployments.values()
                           if d["version_id"] == version["id"]
                           and d["status"] == DEP_BLOCKED]
                for dep in planned:
                    reason_text = f"授权撤回（{grant['licensor']}）: {reason}".strip(": ")
                    if reason_text not in dep["blockers"]:
                        dep["blockers"].append(reason_text)
                    dep["blocked_at"] = at
                channels = sorted(
                    {c["code"] for c in self.channels.values()
                     if c["market"] == grant["market"]})
                blocked_unpublished.append({
                    "version_id": version["id"],
                    "version_code": version["code"],
                    "market": grant["market"],
                    "pending_channels": channels,
                })
            for assignment in self.assignments.values():
                if (assignment["version_id"] == version["id"]
                        and assignment["status"] == ASSIGNMENT_ACTIVE):
                    assignment["status"] = ASSIGNMENT_RELEASED
                    assignment["released_at"] = at
                    assignment["release_reason"] = "授权撤回"
                    released_assignments.append(assignment["id"])
            self._refresh_status(version)

        impact = {
            "grant_id": grant_id,
            "licensor": grant["licensor"],
            "market": grant["market"],
            "withdrawn_at": at,
            "reason": reason,
            "blocked_unpublished": blocked_unpublished,
            "taken_down": taken_down,
            "released_assignments": sorted(set(released_assignments)),
            "affected_channels": sorted(
                {d["channel_code"] for d in taken_down}
                | {c for item in blocked_unpublished for c in item["pending_channels"]}),
        }
        self.withdrawals.append(impact)
        return impact

    # ---- 素材登记与确认（演员、音乐、镜头） -------------------------------

    def register_asset(self, version_id, key, name, rights_markets=None,
                       confirmed=ASSET_UNCONFIRMED, licensor=None, kind="素材"):
        version = self._get(self.versions, version_id, "版本")
        if (version_id, key) in self.assets:
            raise ConflictError(f"素材已存在于该版本: {key}")
        if confirmed not in ASSET_DECISIONS:
            raise DomainError("素材确认状态非法")
        asset = {
            "id": self._id("asset"),
            "version_id": version_id,
            "key": key,
            "name": name,
            "kind": kind,
            "licensor": licensor,
            "rights_markets": sorted(set(rights_markets or [])),
            "confirmed": confirmed,
            "excluded": False,   # 被排除出成片的素材不参与发行核验
            "exclude_reason": "",
            "updated_at": self.now(),
        }
        self.assets[(version_id, key)] = asset
        return asset

    def decide_asset(self, version_id, key, decision, note=""):
        asset = self._get(self.assets, (version_id, key), "素材")
        if decision not in ASSET_DECISIONS or decision == ASSET_UNCONFIRMED:
            raise DomainError("素材只能确认为 confirmed 或 rejected")
        asset["confirmed"] = decision
        asset["decision_note"] = note
        asset["updated_at"] = self.now()
        self._refresh_status(self.versions[version_id])
        return asset

    def remove_asset(self, version_id, key, reason=""):
        """素材被替换或否决后从版本移除（保留登记历史，不再参与发行核验）。"""
        self._get(self.assets, (version_id, key), "素材")
        del self.assets[(version_id, key)]
        self._refresh_status(self.versions[version_id])
        return {"removed": key, "reason": reason}

    def set_asset_included(self, version_id, key, included, reason=""):
        """声明素材是否进入成片；不覆盖某市场的素材应排除，而非卡住全片。"""
        asset = self._get(self.assets, (version_id, key), "素材")
        asset["excluded"] = not included
        asset["exclude_reason"] = reason
        asset["updated_at"] = self.now()
        self._refresh_status(self.versions[version_id])
        return asset

    # ---- 境外制片团队与按地区发料 -----------------------------------------

    def register_team(self, team_id, name, region="", timezone="",
                      hourly_cost=0.0):
        if team_id in self.teams:
            raise ConflictError(f"团队已存在: {team_id}")
        team = {
            "id": team_id,
            "name": name,
            "region": region,
            "timezone": timezone,
            "hourly_cost": float(hourly_cost),
        }
        self.teams[team_id] = team
        return team

    def assign_assets(self, version_id, team_id, asset_keys=None):
        version = self._get(self.versions, version_id, "版本")
        team = self._get(self.teams, team_id, "制片团队")
        market = version["market"]
        keys = asset_keys if asset_keys is not None else [
            key for (vid, key) in self.assets if vid == version_id]
        released, withheld = [], []
        for key in keys:
            asset = self._get(self.assets, (version_id, key), "素材")
            if asset["excluded"]:
                withheld.append({"asset_key": key, "reason": "该素材已声明不进入此市场成片"})
            elif asset["confirmed"] == ASSET_REJECTED:
                withheld.append({"asset_key": key, "reason": "素材已被否决"})
            elif asset["confirmed"] != ASSET_CONFIRMED:
                withheld.append({"asset_key": key, "reason": "演员或音乐尚未确认"})
            elif market not in asset["rights_markets"]:
                withheld.append({"asset_key": key,
                                 "reason": f"素材权利不覆盖 {market} 市场"})
            else:
                released.append(key)
        assignment = {
            "id": self._id("ass"),
            "version_id": version_id,
            "team_id": team_id,
            "market": market,
            "released_assets": sorted(released),
            "withheld_assets": withheld,
            "status": ASSIGNMENT_ACTIVE,
            "created_at": self.now(),
        }
        self.assignments[assignment["id"]] = assignment
        return assignment

    # ---- 工时（一经核算即锁定） -------------------------------------------

    def log_hours(self, version_id, team_id, hours, activity="", worked_on=None):
        version = self._get(self.versions, version_id, "版本")
        team = self._get(self.teams, team_id, "制片团队")
        if hours <= 0:
            raise DomainError("工时必须为正数")
        log = {
            "id": self._id("work"),
            "version_id": version_id,
            "team_id": team_id,
            "hours": float(hours),
            "hourly_cost": team["hourly_cost"],      # 核算时冻结单价
            "amount": round(float(hours) * team["hourly_cost"], 2),
            "activity": activity,
            "worked_on": worked_on or self.now(),
            "locked": True,
        }
        self.worklogs[log["id"]] = log
        return log

    def labor_totals(self, version_id):
        logs = [w for w in self.worklogs.values() if w["version_id"] == version_id]
        return {
            "version_id": version_id,
            "hours": round(sum(w["hours"] for w in logs), 2),
            "cost": round(sum(w["amount"] for w in logs), 2),
            "locked": True,
            "entries": len(logs),
        }

    # ---- 交付（跨时区幂等） -----------------------------------------------

    def register_delivery(self, version_id, team_id, content_hash, delivered_at,
                          note=""):
        version = self._get(self.versions, version_id, "版本")
        team = self._get(self.teams, team_id, "制片团队")
        instant = _parse_instant(delivered_at)
        existing = self.deliveries.get((version_id, content_hash))
        if existing is not None:
            # 同一内容哈希，无论来自哪个时区、重复提交几次，只入账一次
            return {
                "delivery_id": existing["id"],
                "status": DELIVERY_DUPLICATE,
                "first_received_at": existing["received_at"],
                "duplicate_received_at": instant.isoformat(),
                "from_team": team_id,
            }
        delivery = {
            "id": self._id("deliv"),
            "version_id": version_id,
            "team_id": team_id,
            "content_hash": content_hash,
            "received_at": instant.isoformat(),
            "submitted_at": delivered_at,
            "note": note,
        }
        self.deliveries[(version_id, content_hash)] = delivery
        self._refresh_status(version)
        return {"delivery_id": delivery["id"], "status": DELIVERY_ACCEPTED,
                "received_at": delivery["received_at"]}

    # ---- 译审文化复核与当地分级 -------------------------------------------

    def submit_review(self, version_id, reviewer, result, note=""):
        version = self._get(self.versions, version_id, "版本")
        if result not in (REVIEW_PASSED, REVIEW_FAILED):
            raise DomainError("复核结论只能是 passed 或 failed")
        violations = self._cultural_violations(version)
        if result == REVIEW_PASSED and violations:
            raise ConflictError(
                "版本仍含受限文化元素，不能出具通过结论",
                details={"violations": violations})
        review = {
            "version_id": version_id,
            "reviewer": reviewer,
            "result": result,
            "note": note,
            "reviewed_at": self.now(),
        }
        self.reviews[version_id] = review
        self._refresh_status(version)
        return review

    def set_rating(self, version_id, rating, body="", note=""):
        version = self._get(self.versions, version_id, "版本")
        record = {
            "version_id": version_id,
            "market": version["market"],
            "rating": rating,
            "body": body,
            "note": note,
            "rated_at": self.now(),
        }
        self.ratings[(version_id, version["market"])] = record
        self._refresh_status(version)
        return record

    # ---- 发行闸门 ---------------------------------------------------------

    def _cultural_violations(self, version):
        ip_id = self._version_ip(version)
        violations = []
        for key in version["elements"]:
            element = self.elements[(ip_id, key)]
            if element["stance"] == ELEMENT_FORBIDDEN:
                violations.append({"element": key, "reason": "元素已被禁用"})
            elif (element["stance"] == ELEMENT_RESTRICTED
                  and version["market"] in element["restricted_markets"]):
                violations.append(
                    {"element": key,
                     "reason": f"敏感元素在 {version['market']} 市场受限"})
        return violations

    def release_check(self, version_id, at=None):
        version = self._get(self.versions, version_id, "版本")
        at = at or self.now()
        ip_id = self._version_ip(version)
        market = version["market"]
        blockers = []

        # 只有进入成片的素材才参与核验：不覆盖某市场的素材应声明排除。
        included_assets = [
            a for (vid, _key), a in self.assets.items()
            if vid == version_id and not a["excluded"]]
        included_licensors = {a["licensor"] for a in included_assets
                              if a.get("licensor")}

        required = {}
        for license_ in self.licenses.values():
            if license_["ip_id"] != ip_id:
                continue
            if (license_["coverage"] == "ip"
                    or license_["licensor"] in included_licensors):
                required[license_["id"]] = license_
        grant_by_license = {g["license_id"]: g
                            for g in self.active_grants(ip_id, market, at)}
        active_grants = [grant_by_license[lid]
                         for lid in required if lid in grant_by_license]
        rights_ok = all(lid in grant_by_license for lid in required)
        if not rights_ok:
            missing = [required[lid]["licensor"] for lid in required
                       if lid not in grant_by_license]
            blockers.append(
                "权利：该市场授权不完整（"
                + ("、".join(missing) if missing else "无任何授权")
                + " 的授权缺失/过期/撤回）")

        violations = self._cultural_violations(version)
        review = self.reviews.get(version_id)
        cultural_ok = review is not None and review["result"] == REVIEW_PASSED and not violations
        if violations:
            blockers.extend(f"文化：{v['reason']}（{v['element']}）" for v in violations)
        if review is None:
            blockers.append("文化：尚无可复核结论")
        elif review["result"] == REVIEW_FAILED:
            blockers.append("文化：译审复核未通过")

        unconfirmed, no_rights = [], []
        for asset in included_assets:
            if asset["confirmed"] != ASSET_CONFIRMED:
                unconfirmed.append(asset["key"])
            if market not in asset["rights_markets"]:
                no_rights.append(asset["key"])
        assets_ok = not unconfirmed and not no_rights
        if unconfirmed:
            blockers.append(f"素材：演员/音乐尚未确认（{', '.join(sorted(unconfirmed))}）")
        if no_rights:
            blockers.append(f"素材：进入成片的素材权利不覆盖 {market}（{', '.join(sorted(no_rights))}）")

        rating = self.ratings.get((version_id, market))
        rating_ok = rating is not None
        if not rating_ok:
            blockers.append("分级：尚未取得当地分级")

        accepted = [d for d in self.deliveries.values() if d["version_id"] == version_id]
        delivery_ok = bool(accepted)
        if not delivery_ok:
            blockers.append("交付：尚无入账成片")

        return {
            "version_id": version_id,
            "market": market,
            "checked_at": at,
            "rights_ok": rights_ok,
            "cultural_ok": cultural_ok,
            "assets_ok": assets_ok,
            "rating_ok": rating_ok,
            "delivery_ok": delivery_ok,
            "grants": [self._grant_brief(g) for g in active_grants],
            "blockers": blockers,
            "ready": not blockers,
        }

    @staticmethod
    def _grant_brief(grant):
        return {
            "grant_id": grant["id"],
            "licensor": grant["licensor"],
            "market": grant["market"],
            "window": [grant["starts"], grant["expires"]],
            "share_ratio": grant["share_ratio"],
            "currency": grant["currency"],
        }

    def register_channel(self, code, name, market):
        if code in self.channels:
            raise ConflictError(f"渠道已存在: {code}")
        channel = {"code": code, "name": name, "market": market}
        self.channels[code] = channel
        return channel

    def request_release(self, version_id, channel_codes, at=None):
        """对每个渠道执行发行三关；不通过则留下带阻塞原因的未发布记录。"""
        version = self._get(self.versions, version_id, "版本")
        at = at or self.now()
        market = version["market"]
        check = self.release_check(version_id, at)
        results = []

        for code in channel_codes:
            channel = self._get(self.channels, code, "发行渠道")
            if channel["market"] != market:
                raise DomainError(
                    f"渠道 {code} 面向 {channel['market']}，不能发行 {market} 版本")
            existing = next((d for d in self.deployments.values()
                             if d["version_id"] == version_id
                             and d["channel_code"] == code), None)
            if existing is not None and existing["status"] == DEP_ONLINE:
                # 已在线副本不因重复发行请求重复入账
                results.append({"channel_code": code, "status": DEP_ONLINE,
                                "deployment_id": existing["id"],
                                "released_at": existing["released_at"]})
                continue
            if check["ready"]:
                delivery = next(d for d in self.deliveries.values()
                                if d["version_id"] == version_id)
                labor = self.labor_totals(version_id)
                dep = existing or {
                    "id": self._id("dep"),
                    "version_id": version_id,
                    "channel_code": code,
                    "market": market,
                }
                dep.update({
                    "status": DEP_ONLINE,
                    "released_at": at,
                    "content_hash": delivery["content_hash"],
                    "revenue_basis": {
                        # 上线瞬间冻结：此后授权撤回只影响副本状态，不改历史账
                        "grants": check["grants"],
                        "locked_hours": labor["hours"],
                        "locked_labor_cost": labor["cost"],
                        "frozen_at": at,
                    },
                })
                dep.pop("blockers", None)
                dep.pop("blocked_at", None)
                self.deployments[dep["id"]] = dep
                results.append({"channel_code": code, "status": DEP_ONLINE,
                                "deployment_id": dep["id"]})
            else:
                dep = existing or {
                    "id": self._id("dep"),
                    "version_id": version_id,
                    "channel_code": code,
                    "market": market,
                    "status": DEP_BLOCKED,
                }
                # 阻塞快照刷新为最新核验结果（撤权原因在核验中体现）
                dep["status"] = DEP_BLOCKED
                dep["blocked_at"] = at
                dep["blockers"] = list(check["blockers"])
                self.deployments[dep["id"]] = dep
                results.append({"channel_code": code, "status": DEP_BLOCKED,
                                "deployment_id": dep["id"],
                                "blockers": check["blockers"]})
        self._refresh_status(version)
        return {"version_id": version_id, "market": market, "results": results}

    def _refresh_status(self, version):
        deps = [d for d in self.deployments.values()
                if d["version_id"] == version["id"]]
        if any(d["status"] == DEP_ONLINE for d in deps):
            version["status"] = V_RELEASED
            return
        if any(d["status"] == DEP_TAKEN_DOWN for d in deps):
            version["status"] = V_TAKEN_DOWN
            return
        ip_id = self._version_ip(version)
        if not self.release_check(version["id"])["rights_ok"]:
            version["status"] = V_BLOCKED
            return
        review = self.reviews.get(version["id"])
        if review is not None and review["result"] == REVIEW_FAILED:
            version["status"] = V_REVIEW_FAILED
        elif self.release_check(version["id"])["ready"]:
            version["status"] = V_READY
        else:
            version["status"] = V_IN_PRODUCTION

    # ---- 管理看板 ---------------------------------------------------------

    def dashboard(self, at=None):
        at = at or self.now()
        market_index = {}
        open_blockers = []
        ip_rows = []

        for ip in self.ips.values():
            proposal_rows = []
            for proposal in self.proposals.values():
                if proposal["ip_id"] != ip["id"]:
                    continue
                version_rows = []
                for version in self._versions_of(proposal["id"]):
                    row = self._version_row(version, at)
                    version_rows.append(row)
                    online = [d for d in row["deployments"]
                              if d["status"] == DEP_ONLINE]
                    blocked = [d for d in row["deployments"]
                               if d["status"] == DEP_BLOCKED]
                    taken_down = [d for d in row["deployments"]
                                  if d["status"] == DEP_TAKEN_DOWN]
                    for dep in online:
                        market_index.setdefault(version["market"], []).append({
                            "version_code": version["code"],
                            "proposal": proposal["title"],
                            "channel": dep["channel_code"],
                            "state": DEP_ONLINE,
                            "content_hash": dep.get("content_hash"),
                        })
                    for dep in taken_down:
                        market_index.setdefault(version["market"], []).append({
                            "version_code": version["code"],
                            "proposal": proposal["title"],
                            "channel": dep["channel_code"],
                            "state": DEP_TAKEN_DOWN,
                            "content_hash": dep.get("content_hash"),
                            "taken_down_at": dep.get("taken_down_at"),
                        })
                    # 仍未上线且无在线副本的版本，其阻断记录进入未决清单
                    if not online:
                        for dep in blocked:
                            market_index.setdefault(version["market"], []).append({
                                "version_code": version["code"],
                                "proposal": proposal["title"],
                                "channel": dep["channel_code"],
                                "state": DEP_BLOCKED,
                                "content_hash": row["content_hash"],
                            })
                        if version["status"] != V_TAKEN_DOWN:
                            open_blockers.append({
                                "version_id": version["id"],
                                "version_code": version["code"],
                                "market": version["market"],
                                "status": version["status"],
                                "channels": [d["channel_code"] for d in blocked],
                                "blockers": row["release_check"]["blockers"],
                            })
                proposal_rows.append({
                    "proposal_id": proposal["id"],
                    "title": proposal["title"],
                    "status": proposal["status"],
                    "target_markets": proposal["target_markets"],
                    "versions": version_rows,
                })
            ip_rows.append({"ip": ip, "proposals": proposal_rows})

        # 撤权波及的每个副本（已下架副本 + 被阻断的未发布版本）
        affected_copies = []
        for impact in self.withdrawals:
            for item in impact["taken_down"]:
                affected_copies.append({
                    "grant_id": impact["grant_id"],
                    "licensor": impact["licensor"],
                    "reason": impact["reason"],
                    "kind": "已上线副本下架",
                    **item,
                })
            for item in impact["blocked_unpublished"]:
                affected_copies.append({
                    "grant_id": impact["grant_id"],
                    "licensor": impact["licensor"],
                    "reason": impact["reason"],
                    "kind": "未发布版本阻断",
                    **item,
                })

        return {
            "generated_at": at,
            "ips": ip_rows,
            "market_index": market_index,
            "open_blockers": open_blockers,
            "affected_copies": affected_copies,
            "withdrawals": list(self.withdrawals),
        }

    def _versions_of(self, proposal_id):
        versions = [v for v in self.versions.values()
                    if v["proposal_id"] == proposal_id]
        return sorted(versions, key=lambda v: (v["market"], v["created_at"]))

    def _version_row(self, version, at):
        deps = [self.deployments[d_id]
                for d_id in self.deployments
                if self.deployments[d_id]["version_id"] == version["id"]]
        delivery = next((d for d in self.deliveries.values()
                         if d["version_id"] == version["id"]), None)
        online = next((d for d in deps if d["status"] == DEP_ONLINE), None)
        frozen_dep = next((d for d in deps if "revenue_basis" in d), None)
        if online is not None:
            basis = online["revenue_basis"]
        elif frozen_dep is not None:
            # 已下架副本仍保留上线瞬间冻结的收益依据
            basis = {"state": "已下架（冻结快照保留）", **frozen_dep["revenue_basis"]}
        else:
            basis = self._basis_draft(version, at)
        return {
            "version_id": version["id"],
            "code": version["code"],
            "market": version["market"],
            "status": version["status"],
            "parent_id": version["parent_id"],
            "team_id": version["team_id"],
            "elements": list(version["elements"]),
            "review": self.reviews.get(version["id"]),
            "rating": self.ratings.get((version["id"], version["market"])),
            "content_hash": delivery["content_hash"] if delivery else None,
            "release_check": self.release_check(version["id"], at),
            "labor": self.labor_totals(version["id"]),
            "revenue_basis": basis,
            "deployments": deps,
        }

    def version_report(self, version_id, at=None):
        version = self._get(self.versions, version_id, "版本")
        return self._version_row(version, at or self.now())

    def _basis_draft(self, version, at):
        grants = self.active_grants(self._version_ip(version), version["market"], at)
        labor = self.labor_totals(version["id"])
        if not grants:
            return {"state": "无有效授权", "locked_hours": labor["hours"],
                    "locked_labor_cost": labor["cost"]}
        return {
            "state": "待发行冻结",
            "grants": [self._grant_brief(g) for g in grants],
            "locked_hours": labor["hours"],
            "locked_labor_cost": labor["cost"],
        }
