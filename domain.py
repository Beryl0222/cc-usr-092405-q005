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
* 撤权准确阻断未发布版本、下架已上线副本，并列出受影响渠道；
* 平台结算可迟到、按市场/渠道/币种拆分导入：结算行关联已发行副本
  与上线时冻结的分成规则，同一外部流水只入账一次；
* 退款与更正只能追加新行，历史结算行不可改写；
* 汇率按结算日版本化取价，跨币种换算的舍入余差单独入账；
* 授权撤回后的新增收入按收入期间拆分并单独隔离；
* 实收与应收差异超过规则阈值自动建立争议并冻结该市场可分配余额，
  制作方与权利方分别批准后才可释放或调整。
"""

from datetime import date, datetime, timedelta, timezone

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

# 结算导入：行类型（退款与更正只能以新行追加，历史行不可改写）
ENTRY_SETTLEMENT = "settlement"
ENTRY_REFUND = "refund"
ENTRY_CORRECTION = "correction"
ENTRY_TYPES = (ENTRY_SETTLEMENT, ENTRY_REFUND, ENTRY_CORRECTION)

# 争议生命周期：未决 → 已释放（按原口径解冻）/ 已调整（按更正行重入账）
DISPUTE_OPEN = "open"
DISPUTE_RELEASED = "released"
DISPUTE_ADJUSTED = "adjusted"

# 争议双方：制作方与权利方须分别批准
PARTY_PRODUCER = "producer"
PARTY_RIGHTS_HOLDER = "rights_holder"
DISPUTE_PARTIES = (PARTY_PRODUCER, PARTY_RIGHTS_HOLDER)

DEFAULT_BASE_CURRENCY = "USD"
DEFAULT_VARIANCE_THRESHOLD = 0.02   # 实收与应收差异比例超过该值即自动建立争议


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


def _parse_date(value, label):
    """解析 YYYY-MM-DD 日期；非法输入归入业务错误而非 500。"""
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise DomainError(f"{label}必须是 YYYY-MM-DD 日期: {value}")


class Domain:
    """内存版领域存储；正式部署可替换为持久化实现，规则不变。"""

    def __init__(self, now=None, base_currency=DEFAULT_BASE_CURRENCY,
                 default_variance_threshold=DEFAULT_VARIANCE_THRESHOLD):
        self._now = now or _today
        self._seq = 0
        self.base_currency = base_currency
        self.default_variance_threshold = float(default_variance_threshold)
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
        # ---- 财务结算 ----
        self.fx_rates = {}              # (currency, effective_date) -> 记录，只增不改
        self.variance_rules = {}        # market -> 阈值覆盖（默认用 default_variance_threshold）
        self.settlement_lines = {}      # external_txn_id -> 结算行（全局幂等键）
        self.disputes = {}
        self.rounding_residuals = {}    # market -> 跨币种换算舍入余差（基准币）

    # ---- 工具 -------------------------------------------------------------

    def _id(self, prefix):
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

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

    # ---- 汇率（按结算日版本化） -------------------------------------------

    def set_base_currency(self, currency):
        """设定对账基准币；已存在入账记录后不允许再改口径。"""
        if self.settlement_lines:
            raise ConflictError("已存在结算入账，不能再变更基准币")
        self.base_currency = currency
        return {"base_currency": self.base_currency}

    def set_fx_rate(self, currency, rate, effective_date):
        """登记某币种自 effective_date 起对基准币的汇率。

        汇率记录只增不改：同一 (币种, 生效日) 重复登记被拒绝，
        历史结算行换算时冻结的汇率快照永远可复算。
        """
        if not currency:
            raise DomainError("币种不能为空")
        if currency == self.base_currency:
            raise DomainError("基准币汇率恒为 1，无需登记")
        rate = float(rate)
        if rate <= 0:
            raise DomainError("汇率必须为正数")
        day = _parse_date(effective_date, "汇率生效日").isoformat()
        if (currency, day) in self.fx_rates:
            raise ConflictError(
                f"{currency} 在 {day} 的汇率已登记（{self.fx_rates[(currency, day)]['rate']}），"
                "历史汇率不可改写；请用新的生效日登记下一版")
        record = {
            "id": self._id("fx"),
            "currency": currency,
            "rate": rate,
            "effective_date": day,
            "recorded_at": self.now(),
        }
        self.fx_rates[(currency, day)] = record
        return record

    def _fx_rate(self, currency, on_date):
        """取 on_date 当日生效的汇率：精确命中优先，否则取此前最近一版。"""
        if currency == self.base_currency:
            return 1.0
        candidates = [day for (cur, day) in self.fx_rates
                      if cur == currency and day <= on_date]
        if not candidates:
            raise DomainError(
                f"缺少 {currency} 在 {on_date} 当日或之前生效的汇率，无法换算")
        return self.fx_rates[(currency, max(candidates))]["rate"]

    # ---- 差异阈值规则 -----------------------------------------------------

    def set_variance_rule(self, threshold, market=None):
        """设定争议阈值：实收与应收差异比例超过该值即自动建立争议。"""
        threshold = float(threshold)
        if threshold < 0:
            raise DomainError("差异阈值不能为负")
        if market is None:
            self.default_variance_threshold = threshold
            return {"scope": "default", "threshold": threshold}
        self.variance_rules[market] = threshold
        return {"scope": market, "threshold": threshold}

    def _threshold(self, market):
        return self.variance_rules.get(market, self.default_variance_threshold)

    # ---- 结算导入（外部流水幂等、退款/更正只追加） -------------------------

    def import_settlement(self, external_txn_id, entry_type=ENTRY_SETTLEMENT,
                          deployment_id=None, version_id=None, channel_code=None,
                          platform=None, market=None, currency=None,
                          gross_amount=None, received_amount=None,
                          period_start=None, period_end=None, settlement_date=None,
                          corrects_txn_id=None, dispute_id=None, note=""):
        """导入一条平台结算/退款/更正行。

        * 同一 external_txn_id 全局只入账一次，重复到达原样返回首次记录；
        * 结算行必须关联已发行（或曾上线）的副本，分成规则取上线时冻结快照；
        * 退款与更正只能以新行追加，历史行不改写；
        * 收入期间跨过授权撤回日的部分按天拆分并单独隔离；
        * 差异超阈值自动建立争议并冻结该市场可分配余额。
        """
        if not external_txn_id:
            raise DomainError("缺少外部流水号 external_txn_id")
        existing = self.settlement_lines.get(external_txn_id)
        if existing is not None:
            return existing          # 幂等：重复到达（含重启后再次导入）不重复入账
        if entry_type not in ENTRY_TYPES:
            raise DomainError(f"未知结算行类型: {entry_type}")

        linked_dispute = None
        if entry_type == ENTRY_SETTLEMENT:
            if not deployment_id:
                raise DomainError("结算行必须指明 deployment_id（发行副本）")
            dep = self._get(self.deployments, deployment_id, "发行副本")
            if dep["status"] == DEP_BLOCKED:
                raise ConflictError("副本从未上线，不存在可结算的收益依据")
            if "revenue_basis" not in dep:
                raise ConflictError("副本缺少上线时冻结的收益依据，无法对账")
            version = self.versions[dep["version_id"]]
            if version_id is not None and version_id != version["id"]:
                raise DomainError("version_id 与副本所属版本不一致")
            if channel_code is not None and channel_code != dep["channel_code"]:
                raise DomainError("channel_code 与副本渠道不一致")
            if market is not None and market != dep["market"]:
                raise DomainError("market 与副本市场不一致")
            channel_code = dep["channel_code"]
            market = dep["market"]
            basis = dep["revenue_basis"]
            version_id = version["id"]
        else:
            # 退款/更正必须挂在已入账的原始结算行上，沿用其口径
            if not corrects_txn_id:
                raise DomainError("退款/更正必须指明 corrects_txn_id（原始结算流水）")
            original = self._get(self.settlement_lines, corrects_txn_id, "原始结算行")
            if original["entry_type"] != ENTRY_SETTLEMENT:
                raise DomainError("只能对结算行（settlement）追加退款或更正")
            dep = self.deployments[original["deployment_id"]]
            version_id = original["version_id"]
            channel_code = original["channel_code"]
            market = original["market"]
            basis = original["frozen_basis"]
            if entry_type == ENTRY_CORRECTION:
                if not dispute_id:
                    raise DomainError("更正行必须指明 dispute_id（由争议调整产生）")
                linked_dispute = self._get(self.disputes, dispute_id, "争议")
                if linked_dispute["status"] != DISPUTE_OPEN:
                    raise ConflictError("只能对未决争议追加调整更正")
                if linked_dispute["line_id"] != original["id"]:
                    raise DomainError("更正行与争议指向的原始结算行不一致")

        if gross_amount is None or received_amount is None:
            raise DomainError("缺少金额字段：gross_amount / received_amount")
        gross_amount = float(gross_amount)
        received_amount = float(received_amount)
        if gross_amount < 0 or received_amount < 0:
            raise DomainError("金额不能为负")
        if not currency:
            raise DomainError("缺少结算币种 currency")
        if not settlement_date:
            raise DomainError("缺少结算日 settlement_date")
        settlement_date = _parse_date(settlement_date, "结算日").isoformat()
        start = _parse_date(period_start, "收入期间起始")
        end = _parse_date(period_end, "收入期间结束")
        if end < start:
            raise DomainError("收入期间结束日不能早于起始日")

        # 汇率按结算日版本化取价，并冻结快照到行内
        rate = self._fx_rate(currency, settlement_date)
        gross_base = round(gross_amount * rate, 2)
        received_base = round(received_amount * rate, 2)
        residual = round((gross_amount * rate - gross_base)
                         + (received_amount * rate - received_base), 6)
        if residual:
            self.rounding_residuals[market] = round(
                self.rounding_residuals.get(market, 0.0) + residual, 6)

        parts = self._split_by_withdrawal(
            basis, start, end, gross_amount, received_amount, rate)
        allocations = self._allocate(basis, gross_base, received_base)

        line = {
            "id": self._id("stl"),
            "external_txn_id": external_txn_id,
            "entry_type": entry_type,
            "deployment_id": dep["id"],
            "version_id": version_id,
            "channel_code": channel_code,
            "platform": platform or dep["channel_code"],
            "market": market,
            "currency": currency,
            "base_currency": self.base_currency,
            "gross_amount": gross_amount,
            "received_amount": received_amount,
            "fx_rate": rate,
            "fx_rate_date": settlement_date,
            "gross_base": gross_base,
            "received_base": received_base,
            "rounding_residual": residual,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "settlement_date": settlement_date,
            "parts": parts,
            "allocations": allocations,
            "frozen_basis": basis,
            "corrects_txn_id": corrects_txn_id,
            "dispute_id": dispute_id,
            "note": note,
            "imported_at": self.now(),
            "sign": -1 if entry_type == ENTRY_REFUND else 1,
        }
        self.settlement_lines[external_txn_id] = line

        # 差异评估：仅结算行参与；退款与更正本身就是对差异的追加处理
        if entry_type == ENTRY_SETTLEMENT:
            self._evaluate_variance(line)
        return line

    def _split_by_withdrawal(self, basis, start, end, gross_amount,
                             received_amount, rate):
        """按冻结快照中授权的撤回日拆分收入期间。

        撤回日（含）之后的收入不再属于任何在册授权，单独隔离，
        不计入市场可分配余额，待权利状态明确后再处理。
        分成比例用冻结快照（历史口径），撤回事实以授权台账当前状态为准。
        """
        withdrawn_dates = []
        for brief in basis.get("grants", []):
            grant = self.grants.get(brief["grant_id"])
            if grant is not None and grant.get("withdrawn_at"):
                withdrawn_dates.append(grant["withdrawn_at"])
        boundary = min(withdrawn_dates, default=None)
        if boundary is None or end.isoformat() < boundary:
            return [{"label": "regular", "days": (end - start).days + 1,
                     "gross_base": round(gross_amount * rate, 2),
                     "received_base": round(received_amount * rate, 2),
                     "quarantined": False}]
        if start.isoformat() >= boundary:
            spans = [("post_withdrawal", start, end)]
        else:
            split = _parse_date(boundary, "撤回日") - timedelta(days=1)
            spans = [("pre_withdrawal", start, split),
                     ("post_withdrawal", split + timedelta(days=1), end)]
        total_days = sum((e - s).days + 1 for _lbl, s, e in spans)
        parts = []
        gross_done = received_done = 0.0
        for index, (label, span_start, span_end) in enumerate(spans):
            days = (span_end - span_start).days + 1
            if index < len(spans) - 1:
                gross_local = round(gross_amount * days / total_days, 2)
                received_local = round(received_amount * days / total_days, 2)
            else:
                # 最后一段吸收按天拆分的舍入差，保证分段合计等于整行
                gross_local = round(gross_amount - gross_done, 2)
                received_local = round(received_amount - received_done, 2)
            gross_done += gross_local
            received_done += received_local
            parts.append({
                "label": label,
                "period_start": span_start.isoformat(),
                "period_end": span_end.isoformat(),
                "days": days,
                "gross_base": round(gross_local * rate, 2),
                "received_base": round(received_local * rate, 2),
                "quarantined": label == "post_withdrawal",
                "quarantine_reason": (
                    f"授权已于 {boundary} 撤回，撤回后新增收入单独隔离"
                    if label == "post_withdrawal" else ""),
            })
        return parts

    @staticmethod
    def _allocate(basis, gross_base, received_base):
        """按上线时冻结的分成规则拆出各方应收与实收（基准币）。

        权利方按各自 share_ratio 分成，制作方取得剩余份额；
        实收按同一比例口径拆分，差异体现在每一方身上。
        """
        grants = basis.get("grants", [])
        total_ratio = round(sum(g.get("share_ratio") or 0 for g in grants), 6)
        allocations = []
        for grant in grants:
            ratio = grant.get("share_ratio") or 0.0
            allocations.append({
                "party": grant["licensor"],
                "role": PARTY_RIGHTS_HOLDER,
                "grant_id": grant["grant_id"],
                "share_ratio": ratio,
                "receivable_base": round(gross_base * ratio, 2),
                "received_base": round(received_base * ratio, 2),
            })
        allocations.append({
            "party": "制作方",
            "role": PARTY_PRODUCER,
            "grant_id": None,
            "share_ratio": round(1 - total_ratio, 6),
            "receivable_base": round(gross_base * (1 - total_ratio), 2),
            "received_base": round(received_base * (1 - total_ratio), 2),
        })
        return allocations

    def _evaluate_variance(self, line):
        """实收与应收差异超过市场阈值时自动建立争议并冻结市场余额。"""
        receivable = line["gross_base"]
        received = line["received_base"]
        threshold = self._threshold(line["market"])
        # 比例先定精度再比较：恰好等于阈值不算超差，避免浮点尾差误判
        ratio = round((abs(received - receivable) / receivable)
                      if receivable else 0.0, 6)
        if ratio <= threshold:
            return None
        dispute = {
            "id": self._id("dsp"),
            "line_id": line["id"],
            "external_txn_id": line["external_txn_id"],
            "deployment_id": line["deployment_id"],
            "version_id": line["version_id"],
            "market": line["market"],
            "expected_base": receivable,
            "received_base": received,
            "variance_base": round(received - receivable, 2),
            "variance_ratio": round(ratio, 6),
            "threshold": threshold,
            "status": DISPUTE_OPEN,
            "approvals": {PARTY_PRODUCER: None, PARTY_RIGHTS_HOLDER: None},
            "frozen_pool_base": self.market_balance(line["market"])["payable_base"],
            "opened_at": self.now(),
            "resolved_at": None,
            "resolution": None,
            "correction_txn_id": None,
        }
        self.disputes[dispute["id"]] = dispute
        return dispute

    # ---- 争议：制作方与权利方分别批准后才可释放或调整 ----------------------

    def _open_disputes(self, market):
        return [d for d in self.disputes.values()
                if d["market"] == market and d["status"] == DISPUTE_OPEN]

    def approve_dispute(self, dispute_id, party, approver, note=""):
        dispute = self._get(self.disputes, dispute_id, "争议")
        if dispute["status"] != DISPUTE_OPEN:
            raise ConflictError("争议已结案，不能再审批")
        if party not in DISPUTE_PARTIES:
            raise DomainError("审批方只能是 producer（制作方）或 rights_holder（权利方）")
        if dispute["approvals"][party] is not None:
            raise ConflictError(f"{party} 已批准过该争议，重复审批无效")
        dispute["approvals"][party] = {
            "approver": approver, "note": note, "at": self.now()}
        return dispute

    def resolve_dispute(self, dispute_id, resolution, correction=None):
        """结案争议：release 按原口径解冻；adjust 追加更正行重入账。

        两种结案方式都要求制作方与权利方分别批准。
        """
        dispute = self._get(self.disputes, dispute_id, "争议")
        if dispute["status"] != DISPUTE_OPEN:
            raise ConflictError("争议已结案")
        missing = [p for p in DISPUTE_PARTIES if dispute["approvals"][p] is None]
        if missing:
            raise ConflictError(
                "争议须制作方与权利方分别批准后才能结案",
                details={"missing_approvals": missing})
        if resolution not in ("release", "adjust"):
            raise DomainError("结案方式只能是 release（释放）或 adjust（调整）")
        line = self.settlement_lines[dispute["external_txn_id"]]
        if resolution == "adjust":
            if not correction:
                raise DomainError("调整结案必须提供更正金额 correction")
            correction_line = self.import_settlement(
                external_txn_id=correction["external_txn_id"],
                entry_type=ENTRY_CORRECTION,
                corrects_txn_id=line["external_txn_id"],
                dispute_id=dispute_id,
                # 更正行默认只补实收侧（gross=0），应收仍以原结算行为准；
                # 双方确认需要调整应收口径时再显式给 gross_amount
                gross_amount=correction.get("gross_amount", 0),
                received_amount=correction["received_amount"],
                currency=correction.get("currency", line["currency"]),
                period_start=correction.get("period_start", line["period_start"]),
                period_end=correction.get("period_end", line["period_end"]),
                settlement_date=correction.get("settlement_date", self.now()),
                platform=line["platform"],
                note=correction.get("note", "争议调整更正"),
            )
            dispute["correction_txn_id"] = correction_line["external_txn_id"]
            dispute["status"] = DISPUTE_ADJUSTED
        else:
            dispute["status"] = DISPUTE_RELEASED
        dispute["resolution"] = resolution
        dispute["resolved_at"] = self.now()
        return dispute

    # ---- 市场台账：应收、实收与可支付余额 ---------------------------------

    def _market_lines(self, market):
        return sorted(
            (l for l in self.settlement_lines.values() if l["market"] == market),
            key=lambda l: l["id"])

    def market_balance(self, market):
        """汇总某市场台账：应收、实收、隔离、舍入余差与可支付余额。

        存在未决争议时，该市场可分配余额整体冻结（payable 为 0），
        争议结案后自动解冻；隔离款（撤权后新增收入）始终不计入可支付。
        """
        lines = self._market_lines(market)
        receivable = received = quarantined = 0.0
        party_totals = {}
        for line in lines:
            sign = line.get("sign", 1)   # 退款行从余额中扣减，结算/更正行为正
            quarantined_base = sum(p["received_base"]
                                   for p in line["parts"] if p["quarantined"])
            receivable += sign * line["gross_base"]
            received += sign * line["received_base"]
            quarantined += sign * quarantined_base
            for alloc in line["allocations"]:
                share = (alloc["received_base"] / line["received_base"]
                         if line["received_base"] else 0.0)
                party = party_totals.setdefault(
                    alloc["party"],
                    {"role": alloc["role"], "receivable_base": 0.0,
                     "received_base": 0.0, "quarantined_base": 0.0})
                party["receivable_base"] += sign * alloc["receivable_base"]
                party["received_base"] += sign * alloc["received_base"]
                party["quarantined_base"] += sign * round(quarantined_base * share, 2)
        receivable = round(receivable, 2)
        received = round(received, 2)
        quarantined = round(quarantined, 2)
        normal = round(received - quarantined, 2)
        open_disputes = self._open_disputes(market)
        frozen = bool(open_disputes)
        payable = 0.0 if frozen else normal
        for party in party_totals.values():
            for key in ("receivable_base", "received_base", "quarantined_base"):
                party[key] = round(party[key], 2)
            party["payable_base"] = (
                0.0 if frozen
                else round(party["received_base"] - party["quarantined_base"], 2))
        return {
            "market": market,
            "base_currency": self.base_currency,
            "receivable_base": receivable,
            "received_base": received,
            "quarantined_base": quarantined,
            "normal_received_base": normal,
            "rounding_residual_base": self.rounding_residuals.get(market, 0.0),
            "frozen": frozen,
            "open_disputes": [d["id"] for d in open_disputes],
            "payable_base": payable,
            "payable_by_party": party_totals,
            "lines": len(lines),
        }

    def settlement_report(self, external_txn_id):
        """按外部流水逐笔追溯：应收、实收、分成、隔离与关联争议。"""
        line = self._get(self.settlement_lines, external_txn_id, "结算行")
        return {
            "line": line,
            "disputes": [d for d in self.disputes.values()
                         if d["line_id"] == line["id"]],
            "corrections": [l for l in self.settlement_lines.values()
                            if l.get("corrects_txn_id") == external_txn_id],
            "market_ledger": self.market_balance(line["market"]),
        }

    def market_ledger(self, market):
        """市场台账：余额汇总 + 逐笔结算行 + 争议清单。"""
        balance = self.market_balance(market)
        return {
            **balance,
            "settlement_lines": self._market_lines(market),
            "disputes": [d for d in self.disputes.values()
                         if d["market"] == market],
        }

    def _settlement_brief(self, line):
        return {
            "external_txn_id": line["external_txn_id"],
            "entry_type": line["entry_type"],
            "settlement_date": line["settlement_date"],
            "currency": line["currency"],
            "gross_amount": line["gross_amount"],
            "received_amount": line["received_amount"],
            "gross_base": line["gross_base"],
            "received_base": line["received_base"],
            "quarantined_base": round(sum(
                p["received_base"] for p in line["parts"] if p["quarantined"]), 2),
            "corrects_txn_id": line["corrects_txn_id"],
        }

    # ---- 持久化：快照导出/恢复（重启后再次导入仍幂等） ---------------------

    _TUPLE_KEY_STORES = ("elements", "assets", "deliveries", "ratings",
                         "fx_rates")
    _PLAIN_STORES = ("ips", "proposals", "versions", "licenses", "grants",
                     "teams", "assignments", "worklogs", "reviews", "channels",
                     "deployments", "settlement_lines", "disputes",
                     "variance_rules", "rounding_residuals")

    def snapshot(self):
        """导出全量状态为可 JSON 序列化的字典。"""
        stores = {}
        for name in self._PLAIN_STORES:
            stores[name] = dict(getattr(self, name))
        for name in self._TUPLE_KEY_STORES:
            stores[name] = [{"key": list(key), "value": value}
                            for key, value in getattr(self, name).items()]
        return {
            "seq": self._seq,
            "base_currency": self.base_currency,
            "default_variance_threshold": self.default_variance_threshold,
            "stores": stores,
            "withdrawals": list(self.withdrawals),
        }

    @classmethod
    def restore(cls, state, now=None):
        """从快照恢复领域状态；ID 序列延续，重复导入仍按幂等键去重。"""
        domain = cls(now=now,
                     base_currency=state.get("base_currency",
                                             DEFAULT_BASE_CURRENCY),
                     default_variance_threshold=state.get(
                         "default_variance_threshold",
                         DEFAULT_VARIANCE_THRESHOLD))
        domain._seq = state["seq"]
        for name in cls._PLAIN_STORES:
            getattr(domain, name).update(state["stores"].get(name, {}))
        for name in cls._TUPLE_KEY_STORES:
            store = getattr(domain, name)
            for item in state["stores"].get(name, []):
                store[tuple(item["key"])] = item["value"]
        domain.withdrawals = list(state.get("withdrawals", []))
        return domain

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
            "finance": self.finance_overview(),
        }

    def finance_overview(self):
        """财务总览：各市场台账 + 争议清单 + 汇率版本。"""
        markets = sorted({l["market"] for l in self.settlement_lines.values()})
        return {
            "base_currency": self.base_currency,
            "markets": {market: self.market_balance(market) for market in markets},
            "disputes": list(self.disputes.values()),
            "fx_rates": list(self.fx_rates.values()),
            "settlement_lines": [self._settlement_brief(l)
                                 for l in self.settlement_lines.values()],
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
            "settlements": self._version_settlements(version["id"]),
        }

    def _version_settlements(self, version_id):
        """按副本归组的已入账结算行：从版本即可逐笔追到回款。"""
        grouped = {}
        for line in self.settlement_lines.values():
            if line["version_id"] != version_id:
                continue
            grouped.setdefault(line["deployment_id"], []).append(
                self._settlement_brief(line))
        return grouped

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
