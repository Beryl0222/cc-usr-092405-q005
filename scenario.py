"""首部作品端到端样例：按现有权利矩阵走完多地上线流程。

固定时钟推进，便于复现。样例覆盖：

1. 原始 IP、文化顾问限定元素（受限/禁用）；
2. 版权方按市场和期限授权（六市场矩阵，其中一处期限不覆盖上线日）；
3. 境外团队按地区领取素材，未确认音乐与不覆盖地区的素材被扣留；
4. 发行三关：权利、文化复核、当地分级；
5. 译审驳回后返工切新版本；返工不改写已核算工时（另起新账）；
6. 跨时区重复交付（莫斯科/北京同哈希）只入账一次；
7. 发行前撤权：准确阻断未发布版本并指出受影响渠道；
8. 上线后撤权：在线副本下架，收益依据保留为上线时冻结快照；
9. 管理看板：各市场成片、未决阻塞、收益分配依据、撤权波及的每个副本；
10. 迟到结算与对账：按结算日版本化汇率、多市场/渠道/币种拆笔、
    外部流水幂等、退款/更正追加、撤回边界隔离、差异争议双批准与闭环，
    以及"重启后再次导入"逐分一致。
"""

from decimal import Decimal

from domain import (
    Domain,
    ELEMENT_ALLOWED, ELEMENT_RESTRICTED, ELEMENT_FORBIDDEN,
    PROPOSAL_ACCEPTED, ASSET_CONFIRMED, ASSET_UNCONFIRMED,
    DEP_BLOCKED, DEP_ONLINE, DEP_TAKEN_DOWN, DELIVERY_DUPLICATE,
)

T0 = "2026-09-10"
T_RELEASE = "2026-09-20"
T_WITHDRAW_BEFORE = "2026-09-19"
T_WITHDRAW_AFTER = "2026-09-22"


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def run():
    clock = _Clock(T0)
    d = Domain(now=clock)
    trace = []

    def step(title, result):
        trace.append({"step": title, "result": result})
        return result

    # ---- 1. 原始 IP 与文化口径 -------------------------------------------
    ip = step("登记原始IP《丝路长风》",
              d.register_ip("丝路长风", origin="敦煌",
                            note="丝路题材互动游戏+网络短剧"))

    d.add_cultural_element(ip["id"], "dunhuang_mural", "敦煌壁画纹样",
                           stance=ELEMENT_ALLOWED, reviewer="文化顾问-苏")
    step("文化顾问限定宗教仪轨元素（US/ID 受限）",
         d.add_cultural_element(
             ip["id"], "religious_ritual", "宗教仪轨场景",
             stance=ELEMENT_RESTRICTED, restricted_markets=["US", "ID"],
             reviewer="文化顾问-苏"))
    step("文化顾问禁用争议图腾（全部市场）",
         d.add_cultural_element(
             ip["id"], "disputed_totem", "争议图腾",
             stance=ELEMENT_FORBIDDEN, reviewer="文化顾问-苏"))

    # ---- 2. 改编提案与分市场版本 -----------------------------------------
    markets = ["CN", "US", "JP", "KR", "ID", "RU"]
    proposal = step("青年创作者提交多市场改编提案",
                    d.create_proposal(
                        ip["id"], "丝路长风·破晓",
                        summary="壁画守护者跨丝路冒险短剧",
                        creator="青年创作者-林", target_markets=markets))
    d.decide_proposal(proposal["id"], PROPOSAL_ACCEPTED,
                      note="通过提案评审，按市场分别切版", reviewer="国际制片主管")

    # ---- 3. 境外制片团队 --------------------------------------------------
    teams = {}
    for tid, name, region, tz, cost in [
        ("team-moscow", "莫斯科极光工作室", "RU", "Europe/Moscow", 38.0),
        ("team-seoul", "首尔汉江映像", "KR", "Asia/Seoul", 45.0),
        ("team-la", "洛杉矶金门制作", "US", "America/Los_Angeles", 62.0),
        ("team-jakarta", "雅加达椰岛制片", "ID", "Asia/Jakarta", 24.0),
    ]:
        teams[tid] = d.register_team(tid, name, region, tz, cost)

    versions = {}
    for market, team in [
        ("RU", "team-moscow"), ("US", "team-la"), ("KR", "team-seoul"),
        ("ID", "team-jakarta")]:
        versions[market] = d.create_version(
            proposal["id"], f"SL-{market}-v1", market, team_id=team)

    # ---- 4. 版权方按市场和期限授权 ---------------------------------------
    license_novel = d.register_license(
        ip["id"], "长河小说版权", scope="改编与信息网络传播", note="小说方")
    license_music = d.register_license(
        ip["id"], "驼铃音乐厂牌", scope="原声音乐同步与公开表演", note="音乐方",
        coverage="asset")

    grant = {}
    for market, ratio in [("CN", 0.55), ("US", 0.55), ("JP", 0.55),
                          ("KR", 0.55), ("ID", 0.50), ("RU", 0.50)]:
        grant[("novel", market)] = d.add_grant(
            license_novel["id"], market, "2026-01-01", "2028-12-31",
            share_ratio=ratio, currency="USD")
    # JP 窗口不含 9 月上线日：2026-09-30 才生效
    d.grants[grant[("novel", "JP")]["id"]]["starts"] = "2026-09-30"
    grant[("novel", "JP")]["starts"] = "2026-09-30"

    grant[("music", "US")] = d.add_grant(
        license_music["id"], "US", "2026-01-01", "2027-12-31",
        share_ratio=0.12, currency="USD")
    grant[("music", "RU")] = d.add_grant(
        license_music["id"], "RU", "2026-01-01", "2027-12-31",
        share_ratio=0.10, currency="USD")

    # ---- 5. 素材登记：演员/音乐权利只覆盖部分地区 ------------------------
    def build_version(market):
        vid = versions[market]["id"]
        d.register_asset(vid, "lead_actor", "主演确认包",
                         rights_markets=markets, confirmed=ASSET_CONFIRMED,
                         kind="演员")
        d.register_asset(vid, "theme_song", "主题曲《长风》",
                         rights_markets=["US", "RU"],
                         confirmed=ASSET_UNCONFIRMED, kind="音乐",
                         licensor="驼铃音乐厂牌")
        d.register_asset(vid, "mural_pack", "壁画数字化素材",
                         rights_markets=markets, confirmed=ASSET_CONFIRMED,
                         kind="视觉")
        return vid

    for market in ("RU", "US", "KR", "ID"):
        build_version(market)

    # ---- 6. 按地区发料：未确认/不覆盖地区的素材被扣留 --------------------
    step("RU 团队领料（主题曲尚未确认，先行扣留）",
         d.assign_assets(versions["RU"]["id"], "team-moscow"))
    assignment_us = step("美国团队领料（主题曲被扣留：尚未确认）",
                         d.assign_assets(versions["US"]["id"], "team-la"))
    assignment_kr = step("韩国团队领料（主题曲被扣留：权利不覆盖 KR）",
                         d.assign_assets(versions["KR"]["id"], "team-seoul"))
    step("印尼团队领料（主题曲被扣留：权利不覆盖 ID）",
         d.assign_assets(versions["ID"]["id"], "team-jakarta"))

    # 音乐确认后，US/RU 版本重新领料即可拿到；KR 因地区权利始终拿不到
    d.decide_asset(versions["US"]["id"], "theme_song", ASSET_CONFIRMED,
                   note="演员与音乐权属链确认完成")
    d.decide_asset(versions["RU"]["id"], "theme_song", ASSET_CONFIRMED)
    step("美国团队复核领料（主题曲放行）",
         d.assign_assets(versions["US"]["id"], "team-la",
                         asset_keys=["theme_song"]))
    step("俄罗斯团队复核领料（主题曲放行）",
         d.assign_assets(versions["RU"]["id"], "team-moscow",
                         asset_keys=["theme_song"]))

    # KR/ID 版本不使用主题曲（改用本地配乐）：声明排除后不再卡住权利闸门
    step("KR 版本排除主题曲（改用韩国本地配乐）",
         d.set_asset_included(versions["KR"]["id"], "theme_song", False,
                              reason="音乐权利不覆盖 KR，本地配乐替换"))
    step("ID 版本排除主题曲（改用印尼本地配乐）",
         d.set_asset_included(versions["ID"]["id"], "theme_song", False,
                              reason="音乐权利不覆盖 ID，本地配乐替换"))

    # ---- 7. 禁用元素拦截 --------------------------------------------------
    try:
        d.use_element(versions["RU"]["id"], "disputed_totem")
        raise AssertionError("禁用元素应当被拒绝")
    except Exception as exc:
        forbidden_guard = str(exc)
    trace.append({"step": "禁用元素不得进入任何版本", "result": {"blocked": forbidden_guard}})

    # ---- 8. 工时核算（锁定）+ 译审驳回 + 返工 ----------------------------
    v_us = versions["US"]["id"]
    d.log_hours(v_us, "team-la", 40, activity="初剪", worked_on="2026-09-12")
    d.log_hours(v_us, "team-la", 22, activity="混音", worked_on="2026-09-13")
    locked_v1 = step("美国 v1 工时核算并锁定", d.labor_totals(v_us))

    v_ru = versions["RU"]["id"]
    d.log_hours(v_ru, "team-moscow", 36, activity="俄语本地化剪辑",
                worked_on="2026-09-11")
    d.log_hours(v_ru, "team-moscow", 14, activity="配音与混音",
                worked_on="2026-09-14")
    step("俄罗斯版工时核算并锁定", d.labor_totals(v_ru))

    d.use_element(v_us, "religious_ritual")
    step("美国 v1 译审：敏感元素被驳回",
         d.submit_review(v_us, "文化顾问-苏", "failed",
                         note="宗教仪轨在 US 属受限表达，需替换"))

    # 返工：切 v2（保留返工链），v1 工时账不动，v2 另记新账
    v_us2 = step("返工切出美国 v2",
                 d.create_version(proposal["id"], "SL-US-v2", "US",
                                  team_id="team-la", parent_id=v_us,
                                  note="替换受限场景"))["id"]
    d.register_asset(v_us2, "lead_actor", "主演确认包",
                     rights_markets=markets, confirmed=ASSET_CONFIRMED, kind="演员")
    d.register_asset(v_us2, "theme_song", "主题曲《长风》",
                     rights_markets=["US", "RU"], confirmed=ASSET_CONFIRMED,
                     kind="音乐")
    d.register_asset(v_us2, "mural_pack", "壁画数字化素材",
                     rights_markets=markets, confirmed=ASSET_CONFIRMED, kind="视觉")
    d.use_element(v_us2, "dunhuang_mural")
    d.log_hours(v_us2, "team-la", 12, activity="受限场景替换返工",
                worked_on="2026-09-15")
    rework_total = d.labor_totals(v_us2)
    assert d.labor_totals(v_us)["hours"] == locked_v1["hours"] == 62.0
    trace.append({
        "step": "返工不改写已核算工时（v1 锁定 62h，v2 另计 12h）",
        "result": {"v1_locked": locked_v1, "v2_rework": rework_total},
    })

    # ---- 9. 交付：跨时区重复提交只入账一次 -------------------------------
    first = step("莫斯科团队交付 RU 成片（莫斯科时区）",
                 d.register_delivery(
                     versions["RU"]["id"], "team-moscow",
                     "hash:sl-ru:20260918",
                     "2026-09-18T22:30:00+03:00"))
    dup = step("北京协调台重复转交同一成片（北京时区，同日深夜）",
               d.register_delivery(
                   versions["RU"]["id"], "team-moscow",
                   "hash:sl-ru:20260918",
                   "2026-09-19T03:30:00+08:00"))
    assert dup["status"] == DELIVERY_DUPLICATE

    d.register_delivery(v_us2, "team-la", "hash:sl-us-v2:20260917",
                        "2026-09-17T18:00:00-07:00")

    # ---- 10. 复核与分级 ---------------------------------------------------
    step("RU/US v2/KR/ID 译审通过",
         [d.submit_review(versions["RU"]["id"], "文化顾问-苏", "passed"),
          d.submit_review(v_us2, "文化顾问-苏", "passed",
                          note="受限场景已替换为壁画叙事"),
          d.submit_review(versions["KR"]["id"], "文化顾问-苏", "passed"),
          d.submit_review(versions["ID"]["id"], "文化顾问-苏", "passed")])
    d.set_rating(versions["RU"]["id"], "12+", body="RU 分级委员会")
    d.set_rating(v_us2, "PG-13", body="MPA")
    d.set_rating(versions["KR"]["id"], "15", body="KMRB")
    d.set_rating(versions["ID"]["id"], "SU", body="LSF 印尼分级")

    # KR 本地配乐版交付
    d.register_delivery(versions["KR"]["id"], "team-seoul",
                        "hash:sl-kr:20260916",
                        "2026-09-16T20:00:00+09:00")
    # ID 撤权前已完成交付（撤权后这成为唯一阻塞）
    d.register_delivery(versions["ID"]["id"], "team-jakarta",
                        "hash:sl-id:20260916",
                        "2026-09-16T21:00:00+07:00")

    # ---- 11. 渠道与权利矩阵 ----------------------------------------------
    channels = [
        ("ru-stream", "俄境流云平台", "RU"),
        ("us-pix", "美洲 PixPlay", "US"),
        ("jp-toei", "JP东映流媒体", "JP"),
        ("kr-wave", "韩流 Wave", "KR"),
        ("id-layar", "印尼 LayarStream", "ID"),
    ]
    for code, name, market in channels:
        d.register_channel(code, name, market)

    # 发行尝试：RU 就绪 → 上线；US 用 v2 上线（v1 因复核未过不能发行）
    clock.value = T_RELEASE
    release_ru = step("RU 发行三关核验并上线",
                      d.request_release(versions["RU"]["id"], ["ru-stream"],
                                        at=T_RELEASE))
    assert release_ru["results"][0]["status"] == DEP_ONLINE
    blocked_us_v1 = step("US v1 发行被拦（复核未过+素材未确认）",
                         d.request_release(v_us, ["us-pix"], at=T_RELEASE))
    assert blocked_us_v1["results"][0]["status"] == DEP_BLOCKED
    release_us = step("US v2 发行三关核验并上线",
                      d.request_release(v_us2, ["us-pix"], at=T_RELEASE))
    assert release_us["results"][0]["status"] == DEP_ONLINE
    release_kr = step("KR 本地配乐版发行三关核验并上线",
                      d.request_release(versions["KR"]["id"], ["kr-wave"],
                                        at=T_RELEASE))
    assert release_kr["results"][0]["status"] == DEP_ONLINE

    # JP：授权窗口未开始 → 阻断并指明渠道
    v_jp = d.create_version(proposal["id"], "SL-JP-v1", "JP",
                            note="待授权窗口开启")
    blocked_jp = step("JP 发行被拦：授权 2026-09-30 才生效",
                      d.request_release(v_jp["id"], ["jp-toei"], at=T_RELEASE))
    assert blocked_jp["results"][0]["status"] == DEP_BLOCKED

    # ID：上线前版权方撤回 ID 授权 → 已就绪版本被准确阻断，权利成为唯一阻塞
    v_id = versions["ID"]["id"]
    assert d.release_check(v_id, at=T_WITHDRAW_BEFORE)["ready"]
    id_withdraw = step("发行前撤回 ID 授权（阻断未发布版本）",
                       d.withdraw_grant(grant[("novel", "ID")]["id"],
                                        reason="地域发行协议重新谈判",
                                        at=T_WITHDRAW_BEFORE))
    blocked_id = step("ID 发行被拦：授权撤回（唯一阻塞）",
                      d.request_release(v_id, ["id-layar"], at=T_RELEASE))
    assert blocked_id["results"][0]["status"] == DEP_BLOCKED
    assert len(blocked_id["results"][0]["blockers"]) == 1

    # ---- 12. 上线后撤权：在线副本下架，波及渠道逐一列出 ------------------
    post_withdraw = step("上线后撤 RU 授权：在线副本下架、团队访问收回",
                         d.withdraw_grant(grant[("novel", "RU")]["id"],
                                          reason="版权方终止合作",
                                          at=T_WITHDRAW_AFTER))
    ru_after = d.version_report(versions["RU"]["id"], at=T_WITHDRAW_AFTER)
    assert all(dep["status"] == DEP_TAKEN_DOWN for dep in ru_after["deployments"])
    # 历史收益依据仍保留上线瞬间冻结的快照
    frozen = ru_after["deployments"][0]["revenue_basis"]

    dashboard = d.dashboard(at=T_WITHDRAW_AFTER)

    # ======================================================================
    # 13. 结算与对账：晚于发行批次到达、按市场/渠道/币种拆笔、
    #     版本化汇率、短款争议双批准、退款更正追加、撤回边界隔离、
    #     外部流水幂等，以及"重启后再次导入"账目核对
    # ======================================================================
    dep_us = next(dep["id"] for dep in d.deployments.values()
                  if dep["version_id"] == v_us2 and dep["status"] == DEP_ONLINE)
    dep_kr = next(dep["id"] for dep in d.deployments.values()
                  if dep["version_id"] == versions["KR"]["id"]
                  and dep["status"] == DEP_ONLINE)
    dep_ru = next(dep["id"] for dep in d.deployments.values()
                  if dep["version_id"] == versions["RU"]["id"]
                  and dep["status"] == DEP_TAKEN_DOWN)

    # 差异阈值：US/KR/RU 分市场设置（比例或绝对额任一突破即建争议）
    d.set_dispute_rule("US", threshold=0.1, absolute="20.00")
    d.set_dispute_rule("KR", threshold=0.05, absolute="50.00")
    d.set_dispute_rule("RU", threshold=0.1, absolute="50.00")

    # 汇率口径按结算日版本化：迟到批次适用到达当日的汇率
    d.set_fx_rate("EUR", "1.08", "2026-10-20")
    d.set_fx_rate("KRW", "0.00072", "2026-10-18")
    d.set_fx_rate("KRW", "0.00071", "2026-11-20")
    d.set_fx_rate("RUB", "0.0110", "2026-10-20")
    d.set_fx_rate("RUB", "0.0105", "2026-11-15")

    # ---- 13a. US：同一版本按渠道/币种拆两笔，且实收短款 ----------------
    us_batch = step(
        "US 平台 10 月结算（USD 订阅 + EUR 区两笔；应收 1108，实收 808）",
        d.import_settlement("us-pix", [
            {"deployment_id": dep_us, "external_ref": "PIX-2026-09-US-USD",
             "kind": "sale", "gross": "700.00", "currency": "USD",
             "report_amount": "1000.00",
             "period_start": "2026-09-01", "period_end": "2026-09-30",
             "note": "美国区订阅分成"},
            {"deployment_id": dep_us, "external_ref": "PIX-2026-09-US-EUR",
             "kind": "sale", "gross": "100.00", "currency": "EUR",
             "report_amount": "100.00",
             "period_start": "2026-09-01", "period_end": "2026-09-30",
             "note": "欧元区漫游收入，按结算日汇率折 USD"},
        ], "2026-10-20", batch_ref="PIX/2026-09"))
    us_dispute_id = us_batch["results"][0]["dispute_id"]
    assert us_dispute_id, "短款 300/1108 超阈值，应自动建立争议"
    assert us_batch["market_totals"]["frozen"]
    assert us_batch["market_totals"]["payable_base"] == "0.00"

    # 外部流水重复到达（平台重试推送）：不重复入账
    us_replay = step(
        "US 平台重推同一外部流水（判重，账目不变）",
        d.import_settlement("us-pix", [
            {"deployment_id": dep_us, "external_ref": "PIX-2026-09-US-USD",
             "gross": "700.00", "currency": "USD",
             "report_amount": "1000.00", "period_end": "2026-09-30"}],
            "2026-10-20"))
    assert us_replay["results"][0]["status"] == "duplicate"

    # 制作方与权利方分别批准：缺一方不释放
    d.approve_dispute(us_dispute_id, "producer", "国际制片主管",
                      note="认可平台账期解释，先释放再追补")
    assert d._market_totals("US")["frozen"]
    step("权利方批准 US 短款争议（双方齐备，解除市场冻结）",
         d.approve_dispute(us_dispute_id, "rights", "长河小说版权",
                           note="同意按补付计划跟进"))
    assert not d._market_totals("US")["frozen"]

    # 迟到的补付更正批次（11 月才到）：只能追加；差异归零，争议闭环
    us_topup = step(
        "US 平台 11 月补付 300 USD（追加更正，争议闭环并最终释放）",
        d.append_correction(dep_us, "PIX-2026-11-TOPUP", "adjustment",
                            "300.00", "USD", "2026-11-25",
                            report_amount="0.00",
                            period_start="2026-09-01",
                            period_end="2026-09-30",
                            dispute_id=us_dispute_id,
                            note="9 月短款补付"))
    assert us_topup["outcome"] == "resolved"
    us_totals = d._market_totals("US")
    assert us_totals["receivable_base"] == "1108.00"
    assert us_totals["received_base"] == "1108.00"
    assert us_totals["payable_base"] == "1108.00"

    # ---- 13b. KR：外币两笔 + 迟到退款批次（新汇率版本） -----------------
    kr_batch = step(
        "KR 平台 10 月结算（订阅/广告两笔 KRW，按 10 月汇率折 USD 1080）",
        d.import_settlement("kr-wave", [
            {"deployment_id": dep_kr, "external_ref": "WAVE-09-SUB",
             "gross": "900000", "currency": "KRW", "report_amount": "900000",
             "period_start": "2026-09-01", "period_end": "2026-09-30"},
            {"deployment_id": dep_kr, "external_ref": "WAVE-09-AD",
             "gross": "600000", "currency": "KRW", "report_amount": "600000",
             "period_start": "2026-09-01", "period_end": "2026-09-30"},
        ], "2026-10-18"))
    assert kr_batch["market_totals"]["received_base"] == "1080.00"
    kr_refund = step(
        "KR 11 月迟到退款（按 11 月汇率版本，负向追加并配平到分）",
        d.append_correction(dep_kr, "WAVE-11-REFUND-01", "refund",
                            "10000", "KRW", "2026-11-20",
                            report_amount="0",
                            period_start="2026-09-01",
                            period_end="2026-09-30",
                            note="9 月订阅退款"))
    kr_refund_line = d.settlement_lines[kr_refund["result"]["line_id"]]
    assert kr_refund_line["base_amount"] == Decimal("-7.10")
    assert sum(Decimal(s["amount"]) for s in kr_refund_line["shares"]) \
        == Decimal("-7.10")

    # ---- 13c. RU：撤回边界前/边界/后的收入分流 --------------------------
    ru_batch = step(
        "RU 平台迟到结算：撤回日（09-22）之前周期正常入账",
        d.import_settlement("ru-stream", [
            {"deployment_id": dep_ru, "external_ref": "RU-09-PRE",
             "gross": "50000", "currency": "RUB", "report_amount": "50000",
             "period_start": "2026-09-01", "period_end": "2026-09-21"}],
            "2026-10-20"))
    assert ru_batch["results"][0]["status"] == "imported"
    ru_split = step(
        "RU 同一周期跨撤回日及撤回后收入：逐笔隔离，不进可分配余额",
        d.import_settlement("ru-stream", [
            {"deployment_id": dep_ru, "external_ref": "RU-09-EDGE",
             "gross": "4000", "currency": "RUB", "report_amount": "4000",
             "period_start": "2026-09-20", "period_end": "2026-09-22"},
            {"deployment_id": dep_ru, "external_ref": "RU-09-POST",
             "gross": "9000", "currency": "RUB", "report_amount": "9000",
             "period_start": "2026-09-23", "period_end": "2026-09-30"}],
            "2026-10-20"))
    assert [r["status"] for r in ru_split["results"]] == [
        "quarantined", "quarantined"]
    ru_late = step(
        "RU 11 月才到的撤回前尾款（适用 11 月新汇率，仍正常入账）",
        d.import_settlement("ru-stream", [
            {"deployment_id": dep_ru, "external_ref": "RU-09-PRE-LATE",
             "gross": "20000", "currency": "RUB", "report_amount": "20000",
             "period_start": "2026-09-01", "period_end": "2026-09-21"}],
            "2026-11-15"))
    assert ru_late["results"][0]["status"] == "imported"
    assert ru_late["results"][0]["base_amount"] == "210.00"
    ru_totals = d._market_totals("RU")
    assert ru_totals["received_base"] == "760.00"      # 550 + 210
    assert ru_totals["payable_base"] == "760.00"

    # 逐笔追溯：从外部流水一路追到版本、冻结分成与可支付余额
    trace_us = d.line_trace(us_batch["results"][0]["line_id"])
    trace_ru = d.line_trace(ru_late["results"][0]["line_id"])
    assert trace_us["path"]["version_code"] == "SL-US-v2"
    assert len(trace_ru["path"]["frozen_grants"]) == 2  # 小说方+音乐方

    # ---- 13d. 重启后再次导入：去重与账目逐分一致 ------------------------
    finance_before = d.finance_report()
    restarted = Domain(now=clock).load_state(d.snapshot())
    for channel, refs in (("us-pix", ["PIX-2026-09-US-USD"]),
                          ("kr-wave", ["WAVE-09-SUB", "WAVE-11-REFUND-01"]),
                          ("ru-stream", ["RU-09-PRE", "RU-09-POST"])):
        date = "2026-11-20" if channel == "kr-wave" else "2026-10-20"
        replay = restarted.import_settlement(channel, [
            {"deployment_id": (dep_us if channel == "us-pix"
                               else dep_kr if channel == "kr-wave" else dep_ru),
             "external_ref": refs[0], "gross": "1", "currency": "USD",
             "report_amount": "1", "period_end": "2026-09-30"}], date)
        assert replay["results"][0]["status"] == "duplicate"
    assert restarted.finance_report()["markets"] == finance_before["markets"]
    restart_check = {
        "markets": finance_before["markets"],
        "duplicate_imports_after_restart": len(restarted.duplicate_imports),
    }

    return {
        "title": "首部作品《丝路长风·破晓》多地上线与结算对账样例",
        "trace": trace,
        "assertions": {
            "RU_v1_locked_hours": locked_v1["hours"],
            "US_v2_rework_hours": rework_total["hours"],
            "duplicate_delivery_status": dup["status"],
            "KR_online_channel": release_kr["results"][0]["channel_code"],
            "RU_online_then_taken_down": ru_after["deployments"][0]["status"],
            "RU_frozen_revenue_basis_kept": frozen,
            "ID_blocked_channels": id_withdraw["affected_channels"],
            "ID_single_blocker": blocked_id["results"][0]["blockers"],
            "JP_blockers": blocked_jp["results"][0]["blockers"],
            "post_withdraw_affected_channels":
                post_withdraw["affected_channels"],
            "post_withdraw_released_assignments":
                post_withdraw["released_assignments"],
            # ---- 结算对账断言 ----
            "US_shortfall_dispute": us_dispute_id,
            "US_final_ledger": us_totals,
            "KR_refund_base_amount": str(kr_refund_line["base_amount"]),
            "RU_ledger": ru_totals,
            "quarantined_refs": [q["external_ref"]
                                 for q in finance_before["quarantined_lines"]],
            "restart_duplicate_imports":
                restart_check["duplicate_imports_after_restart"],
        },
        "finance": finance_before,
        "trace_us_line": trace_us,
        "trace_ru_late": trace_ru,
        "restart_check": restart_check,
        "dashboard": dashboard,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
