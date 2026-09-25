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
* 平台迟到结算按结算日版本化汇率逐笔入账，锚定上线时冻结的分成规则，
  外部流水重复到达不重复入账，退款/更正只能追加；
* 授权撤回后的新增收入按收入周期末日判定并单独隔离；
* 实收与应收差异超市场阈值自动建立争议、冻结可分配余额，
  制作方与权利方分别批准后方可释放或凭更正闭环。
"""

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, getcontext
from itertools import count

getcontext().prec = 28

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

# ---- 结算与对账 -----------------------------------------------------------

SETTLE_SALE = "sale"                 # 销售（正向收入）
SETTLE_REFUND = "refund"             # 退款（负向，只能追加）
SETTLE_ADJUSTMENT = "adjustment"     # 更正（金额可正可负，只能追加）
SETTLE_KINDS = (SETTLE_SALE, SETTLE_REFUND, SETTLE_ADJUSTMENT)

SETTLE_IMPORTED = "imported"         # 正常入账
SETTLE_DUPLICATE = "duplicate"       # 外部流水重复到达
SETTLE_QUARANTINED = "quarantined"   # 撤回边界后新增，隔离不参与分配

DISPUTE_OPEN = "open"
DISPUTE_APPROVED = "approved"        # 双批准完成，可释放/调整
DISPUTE_RESOLVED = "resolved"        # 已凭更正闭环
DISPUTE_REJECTED = "rejected"        # 双批准中出现拒绝，冻结不解除

BASE_CURRENCY = "USD"                # 内部记账本位币
MONEY_QUANT = Decimal("0.01")        # 货币金额最小单位


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


def _money(value):
    """统一金额口径：字符串/数字/Decimal -> 两位小数 Decimal。"""
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _money_str(value):
    return str(_money(value))


def _ratio(value):
    ratio = Decimal(str(value))
    if not 0 <= ratio <= 1:
        raise DomainError("分成比例必须在 0 与 1 之间")
    return ratio


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
        self._counter = 0
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
        # ---- 结算与对账 ----
        self.fx_rates = {}              # (currency, settlement_date) -> rate
        self.settlements = {}           # settlement_id -> 结算批次头
        self.settlement_lines = {}      # line_id -> 结算行
        self.external_refs = {}         # 外部流水标识 -> line_id（幂等键）
        self.dispute_rules = {}         # market -> {"threshold","mode","absolute"}
        self.disputes = {}              # dispute_id -> 争议
        self.approvals = []             # 争议审批事件（追加）
        self.corrections = []           # 退款/更正/再评等追加型账目事件
        self.quarantine = []            # 撤回边界后的新增收入（隔离）
        self.duplicate_imports = []     # 外部流水重放的审计痕迹

    # ---- 工具 -------------------------------------------------------------

    def _id(self, prefix):
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

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
        market_totals = (self._market_totals(version["market"])
                         if any("revenue_basis" in dep for dep in deps)
                         else None)
        dep_finance = []
        for dep in deps:
            receivable, received = self._deployment_diff(dep["id"])
            dep_disputes = [
                {"dispute_id": dsp["id"], "status": dsp["status"],
                 "diff_base": str(dsp["diff_base"]), "frozen": dsp["frozen"]}
                for dsp in self.disputes.values()
                if dsp["deployment_id"] == dep["id"]]
            dep_quarantine = [
                self.settlement_lines[i]["external_ref"]
                for i in self.quarantine
                if self.settlement_lines[i]["deployment_id"] == dep["id"]]
            dep_finance.append({
                "deployment_id": dep["id"],
                "receivable_base": str(receivable),
                "received_base": str(received),
                # 可支付是市场级口径：该市场存在未决/被拒争议则整体为 0
                "payable_base": (market_totals["payable_base"]
                                 if market_totals else "0.00"),
                "market_frozen": market_totals["frozen"] if market_totals else False,
                "disputes": dep_disputes,
                "quarantined_refs": dep_quarantine,
            })
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
            "finance": dep_finance,
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

    # ======================================================================
    # 结算与对账：迟到结算、版本化汇率、追加型退款/更正、撤回隔离、
    # 差异争议与双方批准、逐笔可追溯的应收/实收/可支付台账
    # ======================================================================

    # ---- 汇率（按结算日版本化） -------------------------------------------

    def set_fx_rate(self, currency, rate, as_of, base_currency=BASE_CURRENCY):
        """登记某结算日的汇率口径；一经结算行引用即冻结，不可改写。"""
        currency = (currency or "").upper()
        if not currency:
            raise DomainError("币种不能为空")
        if currency == base_currency:
            raise DomainError(f"本位币 {base_currency} 无需登记汇率")
        value = Decimal(str(rate))
        if value <= 0:
            raise DomainError("汇率必须为正数")
        used = any((line.get("currency") == currency
                    and line.get("settlement_date") == as_of)
                   for line in self.settlement_lines.values())
        if used:
            raise ConflictError(
                f"{currency} 在 {as_of} 的汇率已被结算行引用，不能改写；"
                "汇率口径按结算日版本化，差异请走更正")
        record = {
            "currency": currency,
            "base_currency": base_currency,
            "rate": value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            "as_of": as_of,
            "updated_at": self.now(),
        }
        self.fx_rates[(currency, as_of)] = record
        return record

    def _fx(self, currency, settlement_date):
        currency = currency.upper()
        if currency == BASE_CURRENCY:
            return {"currency": BASE_CURRENCY, "base_currency": BASE_CURRENCY,
                    "rate": Decimal("1"), "as_of": settlement_date}
        record = self.fx_rates.get((currency, settlement_date))
        if record is None:
            raise DomainError(
                f"缺少 {currency} 在结算日 {settlement_date} 的汇率口径",
                code="MissingFxRate",
                details={"currency": currency, "settlement_date": settlement_date})
        return record

    # ---- 差异阈值规则 ------------------------------------------------------

    def set_dispute_rule(self, market, threshold=None, absolute=None):
        """设置市场差异阈值：按应收比例 threshold 和/或本位币绝对额 absolute。

        任一阈值被突破即自动建立争议。阈值在导入时按最新设置生效。
        """
        if threshold is None and absolute is None:
            raise DomainError("差异规则至少给出 threshold（比例）或 absolute（本位币额）")
        rule = {"market": market, "updated_at": self.now()}
        if threshold is not None:
            value = Decimal(str(threshold))
            if not 0 < value:
                raise DomainError("比例阈值必须为正数")
            rule["threshold"] = value
        if absolute is not None:
            amount = _money(absolute)
            if amount <= 0:
                raise DomainError("绝对额阈值必须为正数")
            rule["absolute"] = amount
        self.dispute_rules[market] = rule
        return rule

    # ---- 冻结分成参与方与舍入分配 -----------------------------------------

    def _frozen_participants(self, deployment):
        """以副本上线时冻结的分成快照还原参与方；制作方取得剩余比例。"""
        basis = deployment.get("revenue_basis")
        if not basis:
            raise DomainError("副本尚未发行，无冻结分成快照，不能接收结算")
        participants, seen, rights_total = [], set(), Decimal("0")
        for grant in basis.get("grants", []):
            if grant.get("share_ratio") is None or grant["licensor"] in seen:
                continue
            seen.add(grant["licensor"])
            ratio = _ratio(grant["share_ratio"])
            rights_total += ratio
            participants.append({
                "party": grant["licensor"], "role": "权利方",
                "grant_id": grant["grant_id"], "ratio": ratio,
            })
        if rights_total > 1:
            raise DomainError(
                "冻结分成比例之和超过 100%，无法分配",
                code="ShareRatioOverflow",
                details={"rights_total": str(rights_total)})
        participants.append({
            "party": "制作方", "role": "制作方", "grant_id": None,
            "ratio": (Decimal("1") - rights_total).quantize(Decimal("0.000001")),
        })
        return participants, basis.get("frozen_at")

    @staticmethod
    def _allocate(base_amount, participants):
        """按冻结比例把实收（本位币）分到各方。

        权利方份额各自四舍五入到分；跨币种/比例舍入产生的尾差全部由
        制作方吸收，保证逐行各方金额之和与实收精确相等。
        """
        total_cents = int((base_amount * 100).to_integral_value(
            rounding=ROUND_HALF_UP))
        sign = -1 if total_cents < 0 else 1
        total_cents_abs = abs(total_cents)
        shares, rights_cents = [], 0
        rights = sorted(
            (p for p in participants if p["role"] == "权利方"),
            key=lambda p: p["party"])
        for party in rights:
            raw = (base_amount * party["ratio"])
            cents = int((abs(raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))
            cents *= sign
            rights_cents += cents
            shares.append({
                "party": party["party"], "role": "权利方",
                "grant_id": party["grant_id"],
                "ratio": str(party["ratio"]),
                "raw_amount": str(raw.quantize(Decimal("0.000001"))),
                "amount": str((Decimal(cents) / 100).quantize(MONEY_QUANT)),
            })
        producer = next(p for p in participants if p["role"] == "制作方")
        producer_raw = base_amount * producer["ratio"]
        producer_cents = total_cents - rights_cents
        shares.append({
            "party": "制作方", "role": "制作方", "grant_id": None,
            "ratio": str(producer["ratio"]),
            "raw_amount": str(producer_raw.quantize(Decimal("0.000001"))),
            "amount": str((Decimal(producer_cents) / 100).quantize(MONEY_QUANT)),
            "rounding_absorbed": str(
                (Decimal(producer_cents) / 100).quantize(MONEY_QUANT)
                - producer_raw.quantize(MONEY_QUANT)),
        })
        allocated = sum(Decimal(s["amount"]) for s in shares)
        assert allocated == base_amount, (allocated, base_amount)
        return shares

    # ---- 撤回边界 ----------------------------------------------------------

    def _withdrawal_boundary(self, ip_id, market):
        rows = [g for g in self.grants.values()
                if g["ip_id"] == ip_id and g["market"] == market
                and g["status"] == GRANT_WITHDRAWN]
        if not rows:
            return None, []
        boundary = min(g["withdrawn_at"] for g in rows)
        grants = sorted(({"grant_id": g["id"], "licensor": g["licensor"],
                          "withdrawn_at": g["withdrawn_at"]}
                         for g in rows), key=lambda x: x["grant_id"])
        return boundary, grants

    # ---- 结算行导入（幂等、隔离、分配、争议） -----------------------------

    def _normalize_kind_amount(self, kind, gross):
        if kind not in SETTLE_KINDS:
            raise DomainError(
                f"结算行类型只能是 {', '.join(SETTLE_KINDS)}")
        amount = Decimal(str(gross))
        if amount == 0:
            raise DomainError("结算金额不能为 0")
        if kind == SETTLE_SALE and amount < 0:
            raise DomainError("销售行金额必须为正；负数请走 refund 或 adjustment")
        if kind == SETTLE_REFUND:
            amount = -abs(amount)     # 退款一律按负向追加
        return _money(amount)

    def import_settlement(self, channel_code, lines, settlement_date,
                          batch_ref="", at=None):
        """导入平台结算批次。同一外部流水重复到达只入账一次。

        每行关联已发行副本及其上线时冻结的分成规则；撤回边界之后的新增
        收入进入隔离；实收与应收差异超阈值时自动建争议并冻结该市场。
        """
        channel = self._get(self.channels, channel_code, "发行渠道")
        if not isinstance(lines, (list, tuple)) or not lines:
            raise DomainError("结算批次至少包含一行")
        at = at or self.now()
        # 批次具备原子性：任一行失败，回滚本批次已写的全部账目与争议变更
        backup = self._finance_backup()
        batch = {
            "id": self._id("set"),
            "channel_code": channel_code,
            "market": channel["market"],
            "settlement_date": settlement_date,
            "batch_ref": batch_ref,
            "imported_at": at,
            "line_ids": [],
            "correction": False,
        }
        self.settlements[batch["id"]] = batch
        results = []
        try:
            for raw in lines:
                result = self._import_line(batch, raw, settlement_date, at)
                if result.get("line_id"):
                    batch["line_ids"].append(result["line_id"])
                results.append(result)
        except BaseException:
            self._finance_restore(backup)
            raise
        totals = self._market_totals(channel["market"])
        return {
            "batch_id": batch["id"],
            "channel_code": channel_code,
            "market": channel["market"],
            "settlement_date": settlement_date,
            "results": results,
            "market_totals": totals,
        }

    def _import_line(self, batch, raw, settlement_date, at,
                     evaluate_dispute=True):
        external_ref = raw.get("external_ref")
        if not external_ref:
            raise DomainError("结算行必须带 external_ref 外部流水标识")
        duplicate_of = self.external_refs.get(external_ref)
        if duplicate_of is not None:
            # 外部流水重放：不新建账目不重新分配，只留下审计痕迹
            hit = {"external_ref": external_ref, "line_id": duplicate_of,
                   "batch_id": batch["id"], "at": at}
            self.duplicate_imports.append(hit)
            return {"external_ref": external_ref, "status": SETTLE_DUPLICATE,
                    "line_id": duplicate_of, "rejected_batch_id": batch["id"]}

        deployment_id = raw.get("deployment_id")
        deployment = self._get(self.deployments, deployment_id, "发行副本")
        if deployment["channel_code"] != batch["channel_code"]:
            raise DomainError(
                f"结算行渠道 {batch['channel_code']} 与副本发行渠道 "
                f"{deployment['channel_code']} 不一致",
                details={"external_ref": external_ref})
        version = self.versions[deployment["version_id"]]
        market = version["market"]
        if market != batch["market"]:
            raise DomainError("结算行市场与副本市场不一致")

        kind = raw.get("kind", SETTLE_SALE)
        gross = self._normalize_kind_amount(kind, raw.get("gross"))
        currency = (raw.get("currency") or BASE_CURRENCY).upper()
        report = (raw.get("report_amount") if raw.get("report_amount") is not None
                  else gross)
        report = _money(report)
        if kind == SETTLE_REFUND:
            report = -abs(report)

        period_start = raw.get("period_start") or ""
        period_end = raw.get("period_end") or period_start
        if period_start and period_end and period_end < period_start:
            raise DomainError("结算周期结束日不能早于开始日")

        fx = self._fx(currency, settlement_date)
        base_amount = (gross * fx["rate"]).quantize(MONEY_QUANT,
                                                    rounding=ROUND_HALF_UP)
        report_base = (report * fx["rate"]).quantize(MONEY_QUANT,
                                                     rounding=ROUND_HALF_UP)

        ip_id = self._version_ip(version)
        boundary, withdrawn_grants = self._withdrawal_boundary(ip_id, market)
        revenue_end = period_end or period_start or settlement_date
        quarantined = bool(boundary and revenue_end >= boundary)

        line = {
            "id": self._id("line"),
            "batch_id": batch["id"],
            "external_ref": external_ref,
            "channel_code": batch["channel_code"],
            "market": market,
            "deployment_id": deployment_id,
            "version_id": version["id"],
            "version_code": version["code"],
            "kind": kind,
            "status": SETTLE_QUARANTINED if quarantined else SETTLE_IMPORTED,
            "currency": currency,
            "gross_amount": gross,                 # 实收（原币，带符号）
            "report_amount": report,              # 应收（平台报表口径）
            "fx": {"rate": fx["rate"], "as_of": fx["as_of"],
                   "base_currency": fx["base_currency"]},
            "base_amount": base_amount,           # 实收本位币
            "report_base": report_base,           # 应收本位币
            "settlement_date": settlement_date,
            "period_start": period_start,
            "period_end": period_end,
            "withdrawn_boundary": boundary,
            "withdrawn_grants": withdrawn_grants,
            "shares": [],
            "dispute_ids": [],
            "note": raw.get("note", ""),
            "received_at": at,
        }

        if quarantined:
            # 撤回生效后的新增收入：只登记隔离，不分配、不入可支付余额
            line["quarantine_reason"] = (
                f"收入周期末日 {revenue_end} 不早于授权撤回边界 {boundary}")
            self.quarantine.append(line["id"])
        else:
            participants, frozen_at = self._frozen_participants(deployment)
            line["shares"] = self._allocate(base_amount, participants)
            line["basis_frozen_at"] = frozen_at

        self.settlement_lines[line["id"]] = line
        self.external_refs[external_ref] = line["id"]

        dispute_id = None
        if not quarantined and evaluate_dispute:
            dispute_id = self._evaluate_dispute(line, at)
        return {
            "external_ref": external_ref,
            "status": line["status"],
            "line_id": line["id"],
            "deployment_id": deployment_id,
            "base_amount": str(base_amount),
            "quarantined": quarantined,
            "dispute_id": dispute_id,
        }

    # ---- 差异争议与双批准 -------------------------------------------------

    def _normal_lines(self, deployment_id):
        return [ln for ln in self.settlement_lines.values()
                if ln["deployment_id"] == deployment_id
                and ln["status"] == SETTLE_IMPORTED]

    def _deployment_diff(self, deployment_id):
        lines = self._normal_lines(deployment_id)
        receivable = sum((ln["report_base"] for ln in lines), Decimal("0"))
        received = sum((ln["base_amount"] for ln in lines), Decimal("0"))
        return receivable.quantize(MONEY_QUANT), received.quantize(MONEY_QUANT)

    def _rule_breached(self, market, receivable, received):
        rule = self.dispute_rules.get(market)
        if rule is None:
            return None
        diff = receivable - received
        breaches = []
        if "threshold" in rule and receivable > 0:
            ratio = abs(diff) / receivable
            if ratio >= rule["threshold"]:
                breaches.append({"type": "ratio", "limit": str(rule["threshold"]),
                                 "actual": str(ratio.quantize(Decimal("0.000001")))})
        if "absolute" in rule and abs(diff) >= rule["absolute"]:
            breaches.append({"type": "absolute", "limit": str(rule["absolute"]),
                             "actual": str(abs(diff))})
        return {"diff": diff.quantize(MONEY_QUANT), "breaches": breaches} \
            if breaches else None

    def _evaluate_dispute(self, line, at):
        deployment_id = line["deployment_id"]
        market = line["market"]
        receivable, received = self._deployment_diff(deployment_id)
        open_dispute = next(
            (dsp for dsp in self.disputes.values()
             if dsp["deployment_id"] == deployment_id
             and dsp["status"] in (DISPUTE_OPEN, DISPUTE_REJECTED)), None)
        breach = self._rule_breached(market, receivable, received)
        if open_dispute is not None:
            # 已开争议：迟到结算即使把差异补齐也维持冻结，释放仍须双批准；
            # 差异重新扩大时刷新超限口径。
            open_dispute["line_ids"].append(line["id"])
            open_dispute["receivable_base"] = receivable
            open_dispute["received_base"] = received
            open_dispute["diff_base"] = (receivable - received).quantize(MONEY_QUANT)
            if breach is not None:
                open_dispute["breaches"] = breach["breaches"]
            if open_dispute["id"] not in line["dispute_ids"]:
                line["dispute_ids"].append(open_dispute["id"])
            return open_dispute["id"]
        if breach is None:
            return None
        dispute = {
            "id": self._id("dsp"),
            "market": market,
            "deployment_id": deployment_id,
            "version_id": line["version_id"],
            "channel_code": line["channel_code"],
            "opened_at": at,
            "status": DISPUTE_OPEN,
            "receivable_base": receivable,
            "received_base": received,
            "diff_base": breach["diff"],
            "breaches": breach["breaches"],
            "line_ids": [line["id"]],
            "approvals": {"producer": None, "rights": None},
            "frozen": True,
            "resolved_at": None,
            "resolution_line_id": None,
        }
        self.disputes[dispute["id"]] = dispute
        line["dispute_ids"].append(dispute["id"])
        return dispute["id"]

    def approve_dispute(self, dispute_id, role, approver, decision="approve",
                        note="", at=None):
        """制作方与权利方分别表态；双方最新表态均为批准才释放市场余额。

        任一方拒绝即维持冻结；拒绝方随后改投批准可翻案，双方一致即释放。
        """
        dispute = self._get(self.disputes, dispute_id, "争议")
        if role not in ("producer", "rights"):
            raise DomainError("批准角色只能是 producer（制作方）或 rights（权利方）")
        if decision not in ("approve", "reject"):
            raise DomainError("决定只能是 approve 或 reject")
        if dispute["status"] not in (DISPUTE_OPEN, DISPUTE_REJECTED):
            raise ConflictError(f"争议已处于 {dispute['status']} 状态，不能再批准")
        event = {"dispute_id": dispute_id, "role": role, "approver": approver,
                 "decision": decision, "note": note, "at": at or self.now()}
        self.approvals.append(event)
        dispute["approvals"][role] = event
        approved_by_all = all(
            dispute["approvals"][r] is not None
            and dispute["approvals"][r]["decision"] == "approve"
            for r in ("producer", "rights"))
        if approved_by_all:
            dispute["status"] = DISPUTE_APPROVED
            dispute["approved_at"] = event["at"]
            dispute["frozen"] = False
        else:
            # 仍有任一方未批准或最新表态为拒绝：维持冻结/被拒状态
            dispute["status"] = (DISPUTE_REJECTED
                                 if decision == "reject"
                                 or any(dispute["approvals"][r] is not None
                                        and dispute["approvals"][r]["decision"] == "reject"
                                        for r in ("producer", "rights"))
                                 else DISPUTE_OPEN)
            dispute["frozen"] = True
        return dispute

    def append_correction(self, deployment_id, external_ref, kind, gross,
                          currency, settlement_date, report_amount=None,
                          period_start="", period_end="", note="",
                          dispute_id=None, at=None):
        """退款/更正只能追加；引用争议时必须先经双方批准。

        批准后的更正若把累计差异带回阈值内，争议闭环并彻底释放。
        """
        deployment = self._get(self.deployments, deployment_id, "发行副本")
        channel_code = deployment["channel_code"]
        dispute = None
        if dispute_id is not None:
            dispute = self._get(self.disputes, dispute_id, "争议")
            if dispute["deployment_id"] != deployment_id:
                raise DomainError("更正与争议不属于同一发行副本")
            if dispute["status"] == DISPUTE_OPEN:
                raise ConflictError(
                    "争议尚未取得制作方与权利方双批准，不能调整；"
                    "退款与更正已可先行登记，但调整争议须等批准")
            if dispute["status"] != DISPUTE_APPROVED:
                raise ConflictError(f"争议状态为 {dispute['status']}，不能追加调整")
        at = at or self.now()
        backup = self._finance_backup()
        batch = {
            "id": self._id("set"),
            "channel_code": channel_code,
            "market": self.versions[deployment["version_id"]]["market"],
            "settlement_date": settlement_date,
            "batch_ref": f"correction:{external_ref}",
            "imported_at": at,
            "line_ids": [],
            "correction": True,
        }
        self.settlements[batch["id"]] = batch
        raw = {"deployment_id": deployment_id, "external_ref": external_ref,
               "kind": kind, "gross": gross, "currency": currency,
               "report_amount": report_amount, "period_start": period_start,
               "period_end": period_end, "note": note}
        # 更正行不参与自动开争议；由本方法在双批准前提下统一重评
        try:
            result = self._import_line(batch, raw, settlement_date, at,
                                       evaluate_dispute=False)
        except BaseException:
            self._finance_restore(backup)
            raise
        self.corrections.append({"line_id": result.get("line_id"),
                                 "dispute_id": dispute_id, "at": at})
        if dispute is None or not result.get("line_id"):
            return {"batch_id": batch["id"], "result": result,
                    "dispute_id": dispute_id, "dispute_status": None}
        if result.get("quarantined"):
            # 撤回边界后的金额只进隔离区，不冲减争议差异
            return {"batch_id": batch["id"], "result": result,
                    "dispute_id": dispute_id, "dispute_status": dispute["status"],
                    "quarantined": True}
        receivable, received = self._deployment_diff(deployment_id)
        dispute["receivable_base"] = receivable
        dispute["received_base"] = received
        dispute["diff_base"] = (receivable - received).quantize(MONEY_QUANT)
        breach = self._rule_breached(dispute["market"], receivable, received)
        if breach is None:
            dispute["status"] = DISPUTE_RESOLVED
            dispute["resolved_at"] = at
            dispute["resolution_line_id"] = result["line_id"]
            dispute["frozen"] = False
            dispute["breaches"] = []
            outcome = "resolved"
        else:
            # 更正后仍超阈值：争议重开，恢复冻结，双方须重新批准
            dispute["status"] = DISPUTE_OPEN
            dispute["frozen"] = True
            dispute["breaches"] = breach["breaches"]
            dispute["approvals"] = {"producer": None, "rights": None}
            dispute["reopened_at"] = at
            dispute["reopen_line_id"] = result["line_id"]
            outcome = "reopened"
        return {"batch_id": batch["id"], "result": result,
                "dispute_id": dispute_id, "dispute_status": dispute["status"],
                "outcome": outcome,
                "receivable_base": str(receivable),
                "received_base": str(received)}

    # ---- 台账与逐笔追溯 ----------------------------------------------------

    def _market_frozen(self, market):
        # 未决争议或被任一方拒绝的争议都维持冻结；只有双批准/闭环才释放
        return any(dsp["market"] == market
                   and dsp["status"] in (DISPUTE_OPEN, DISPUTE_REJECTED)
                   for dsp in self.disputes.values())

    def _market_totals(self, market):
        lines = [ln for ln in self.settlement_lines.values()
                 if ln["market"] == market and ln["status"] == SETTLE_IMPORTED]
        receivable = sum((ln["report_base"] for ln in lines), Decimal("0"))
        received = sum((ln["base_amount"] for ln in lines), Decimal("0"))
        parties = {}
        currencies = {}
        for ln in lines:
            currencies[ln["currency"]] = currencies.get(ln["currency"], Decimal("0")) \
                + ln["gross_amount"]
            for share in ln["shares"]:
                slot = parties.setdefault(
                    share["party"],
                    {"role": share["role"], "amount": Decimal("0")})
                slot["amount"] += Decimal(share["amount"])
        frozen = self._market_frozen(market)
        return {
            "market": market,
            "receivable_base": str(receivable.quantize(MONEY_QUANT)),
            "received_base": str(received.quantize(MONEY_QUANT)),
            "distributable_base": str(received.quantize(MONEY_QUANT)),
            "payable_base": "0.00" if frozen
            else str(received.quantize(MONEY_QUANT)),
            "frozen": frozen,
            "currency_breakdown": {code: str(amount.quantize(MONEY_QUANT))
                                   for code, amount in sorted(currencies.items())},
            "participants": {
                name: {"role": slot["role"],
                       "amount": str(slot["amount"].quantize(MONEY_QUANT))}
                for name, slot in sorted(parties.items())},
        }

    def _line_brief(self, line):
        return {
            "line_id": line["id"], "external_ref": line["external_ref"],
            "batch_id": line["batch_id"], "kind": line["kind"],
            "status": line["status"],
            "deployment_id": line["deployment_id"],
            "version_code": line["version_code"],
            "currency": line["currency"],
            "gross_amount": str(line["gross_amount"]),
            "report_amount": str(line["report_amount"]),
            "fx_as_of": line["fx"]["as_of"], "fx_rate": str(line["fx"]["rate"]),
            "base_amount": str(line["base_amount"]),
            "report_base": str(line["report_base"]),
            "settlement_date": line["settlement_date"],
            "period": [line["period_start"], line["period_end"]],
            "dispute_ids": list(line["dispute_ids"]),
        }

    def settlement_report(self, settlement_id):
        """一个结算批次的逐行明细：每行都能追到副本、冻结快照与分成。"""
        batch = self._get(self.settlements, settlement_id, "结算批次")
        lines = [self.settlement_lines[i] for i in batch["line_ids"]]
        return {
            "batch": {k: v for k, v in batch.items()},
            "lines": [self._line_brief(ln) for ln in lines],
        }

    def line_trace(self, line_id):
        """逐笔追溯：外部流水 -> 结算行 -> 发行副本 -> 冻结分成 -> 台账累计。"""
        line = self._get(self.settlement_lines, line_id, "结算行")
        deployment = self.deployments[line["deployment_id"]]
        version = self.versions[line["version_id"]]
        receivable, received = self._deployment_diff(line["deployment_id"])
        participants, frozen_at = self._frozen_participants(deployment)
        market = self._market_totals(line["market"])
        return {
            "line": self._line_brief(line),
            "path": {
                "channel_code": deployment["channel_code"],
                "deployment_id": deployment["id"],
                "deployment_status": deployment["status"],
                "released_at": deployment.get("released_at"),
                "version_id": version["id"],
                "version_code": version["code"],
                "market": version["market"],
                "basis_frozen_at": frozen_at,
                "frozen_grants": deployment["revenue_basis"]["grants"],
                "locked_labor_cost":
                    deployment["revenue_basis"]["locked_labor_cost"],
            },
            "fx_version": line["fx"],
            "shares": line["shares"] if line["status"] == SETTLE_IMPORTED
            else [],
            "ledger": {
                "scope": "deployment",
                "receivable_base": str(receivable),
                "received_base": str(received),
                "payable_base": market["payable_base"],
                "market_frozen": market["frozen"],
                "quarantined": line["status"] == SETTLE_QUARANTINED,
            },
            "quarantine": {
                "active": line["status"] == SETTLE_QUARANTINED,
                "boundary": line.get("withdrawn_boundary"),
                "grants": line.get("withdrawn_grants", []),
                "reason": line.get("quarantine_reason", ""),
            },
        }

    def finance_report(self, market=None, at=None):
        """财务总账：分市场应收/实收/可支付、争议、隔离与汇率版本。"""
        markets = sorted({ln["market"] for ln in self.settlement_lines.values()}
                         | set(self.dispute_rules))
        if market is not None:
            markets = [m for m in markets if m == market]
        deployment_rows = []
        for dep_id, dep in sorted(self.deployments.items()):
            lines = self._normal_lines(dep_id)
            if not lines and not any(
                    ln["deployment_id"] == dep_id
                    for ln in self.settlement_lines.values()):
                continue
            version = self.versions[dep["version_id"]]
            receivable, received = self._deployment_diff(dep_id)
            deployment_rows.append({
                "deployment_id": dep_id,
                "version_id": version["id"],
                "version_code": version["code"],
                "market": version["market"],
                "channel_code": dep["channel_code"],
                "deployment_status": dep["status"],
                "lines": len(lines),
                "receivable_base": str(receivable),
                "received_base": str(received),
                "frozen_basis": dep.get("revenue_basis"),
            })
        return {
            "generated_at": at or self.now(),
            "base_currency": BASE_CURRENCY,
            "markets": {m: self._market_totals(m) for m in markets},
            "deployments": deployment_rows,
            "disputes": [dsp for dsp in
                         sorted(self.disputes.values(),
                                key=lambda d: d["opened_at"])
                         if market is None or dsp["market"] == market],
            "quarantined_lines": [
                self._line_brief(self.settlement_lines[i])
                for i in self.quarantine
                if market is None
                or self.settlement_lines[i]["market"] == market],
            "duplicate_imports": list(self.duplicate_imports),
            "fx_versions": [
                {"currency": k[0], "as_of": k[1],
                 "rate": str(rec["rate"]), "base_currency": rec["base_currency"]}
                for k, rec in sorted(self.fx_rates.items())],
        }

    # ---- 快照/恢复（重启后再次导入账目一致） ------------------------------

    _FINANCE_STORES = (
        "settlements", "settlement_lines", "external_refs", "disputes",
        "approvals", "corrections", "quarantine", "duplicate_imports")

    def _finance_backup(self):
        from copy import deepcopy
        return {"_counter": self._counter,
                **{name: deepcopy(getattr(self, name))
                   for name in self._FINANCE_STORES}}

    def _finance_restore(self, backup):
        self._counter = backup["_counter"]
        for name in self._FINANCE_STORES:
            setattr(self, name, backup[name])

    def snapshot(self):
        return _freeze({k: v for k, v in self.__dict__.items()
                        if k != "_now"})

    def load_state(self, state):
        data = _thaw(state)
        self._counter = int(data.get("_counter", 0))
        for name in (
                "ips", "elements", "proposals", "versions", "licenses",
                "grants", "teams", "assets", "assignments", "worklogs",
                "deliveries", "reviews", "ratings", "channels", "deployments",
                "withdrawals", "fx_rates", "settlements", "settlement_lines",
                "external_refs", "dispute_rules", "disputes", "approvals",
                "corrections", "quarantine", "duplicate_imports"):
            setattr(self, name, data.get(name, _empty_default(name)))
        return self


def _empty_default(name):
    return [] if name in ("withdrawals", "approvals", "corrections",
                          "quarantine", "duplicate_imports") else {}


def _freeze(obj):
    if isinstance(obj, Decimal):
        return {"__decimal__": str(obj)}
    if isinstance(obj, dict):
        if any(isinstance(k, tuple) for k in obj):
            return {"__pairs__": [
                [list(k) if isinstance(k, tuple) else k, _freeze(v)]
                for k, v in obj.items()]}
        return {k: _freeze(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_freeze(v) for v in obj]
    return obj


def _thaw(obj):
    if isinstance(obj, dict):
        if "__decimal__" in obj:
            return Decimal(obj["__decimal__"])
        if "__pairs__" in obj:
            restored = {}
            for key, value in obj["__pairs__"]:
                restored[tuple(key) if isinstance(key, list) else key] = _thaw(value)
            return restored
        return {k: _thaw(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_thaw(v) for v in obj]
    return obj
