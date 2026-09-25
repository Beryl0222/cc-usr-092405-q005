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
9. 平台结算迟到且按市场/渠道/币种拆成多笔：逐笔关联已发行副本与冻结分成规则；
10. 退款与更正只追加新行；汇率按结算日版本化，跨币种舍入余差单独入账；
11. 撤权后的新增收入按收入期间拆分并单独隔离，不计入可支付余额；
12. 差异超阈值自动建立争议并冻结该市场可分配余额，制作方与权利方分别批准后结案；
13. 同一外部流水重复到达（含重启后再次导入）只入账一次，台账始终一致；
14. 管理看板：各市场成片、未决阻塞、收益分配依据、撤权波及与财务台账。
"""

from domain import (
    Domain,
    ELEMENT_ALLOWED, ELEMENT_RESTRICTED, ELEMENT_FORBIDDEN,
    PROPOSAL_ACCEPTED, ASSET_CONFIRMED, ASSET_UNCONFIRMED,
    DEP_BLOCKED, DEP_ONLINE, DEP_TAKEN_DOWN, DELIVERY_DUPLICATE,
    ENTRY_REFUND,
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

    # ---- 13. 财务结算：迟到回款、多币种拆分、争议与撤权隔离 ----------------
    # 上线批次是 09-20，平台结算 10 月才陆续到达（迟到结算）；
    # 同一版本按市场/渠道/币种拆成多笔到账。
    dep_ru = release_ru["results"][0]["deployment_id"]
    dep_us = release_us["results"][0]["deployment_id"]
    dep_kr = release_kr["results"][0]["deployment_id"]

    # 汇率按结算日版本化：USD 为基准币；KRW 在 10-01 改版，两版并存
    d.set_fx_rate("KRW", 0.00072, "2026-09-01")
    d.set_fx_rate("KRW", 0.00075, "2026-10-01")
    d.set_fx_rate("RUB", 0.011, "2026-09-01")

    # US：09 月账期的结算 10-05 才到；实收短少 8% 超过默认阈值 2%
    us_line = step("US 迟到结算到账（实收短少 8%，自动建争议）",
                   d.import_settlement(
                       external_txn_id="PIX-202609-US", deployment_id=dep_us,
                       platform="PixPlay", currency="USD",
                       gross_amount=10000, received_amount=9200,
                       period_start="2026-09-20", period_end="2026-09-30",
                       settlement_date="2026-10-05"))
    us_dispute = next(iter(d.disputes.values()))
    assert d.market_balance("US")["frozen"]
    assert d.market_balance("US")["payable_base"] == 0.0

    # 争议须制作方与权利方分别批准；仅一方批准不能结案
    d.approve_dispute(us_dispute["id"], "producer", "制片主任-陈")
    try:
        d.resolve_dispute(us_dispute["id"], "release")
        raise AssertionError("单方批准不应结案")
    except Exception:
        pass
    d.approve_dispute(us_dispute["id"], "rights_holder", "长河小说版权部")
    # 双方批准平台补款凭证后按 adjust 结案：更正行只追加、不改写原行
    step("US 争议双方批准后按平台补款调整结案",
         d.resolve_dispute(us_dispute["id"], "adjust", correction={
             "external_txn_id": "PIX-202609-US-CORR",
             "received_amount": 780,
             "settlement_date": "2026-10-12",
             "note": "平台补发渠道分成差额",
         }))
    assert not d.market_balance("US")["frozen"]

    # US 还有一笔 10 月账期的退款：只追加退款行，原结算行不动
    step("US 退款只追加新行（用户退订回溯）",
         d.import_settlement(
             external_txn_id="PIX-202610-US-RFD", entry_type=ENTRY_REFUND,
             corrects_txn_id="PIX-202609-US", currency="USD",
             gross_amount=300, received_amount=300,
             period_start="2026-10-01", period_end="2026-10-15",
             settlement_date="2026-10-20", note="退订回溯"))

    # KR：韩元结算，跨币种换算产生舍入余差；汇率按结算日取 10-01 改版后的 0.00075
    kr_line = step("KR 韩元结算（跨币种舍入余差入账）",
                   d.import_settlement(
                       external_txn_id="WAV-202610-KR", deployment_id=dep_kr,
                       platform="Wave", currency="KRW",
                       gross_amount=6666667, received_amount=6666667,
                       period_start="2026-09-20", period_end="2026-09-30",
                       settlement_date="2026-10-08"))
    assert kr_line["fx_rate"] == 0.00075
    assert d.market_balance("KR")["rounding_residual_base"] != 0.0

    # RU：上线后撤权（09-22），平台 10 月才结 09 月账期——
    # 收入期间跨过撤回日，按天拆分：09-20/21 正常可分配，09-22 起隔离
    ru_line = step("RU 迟到结算跨撤回日拆分（撤权后收入单独隔离）",
                   d.import_settlement(
                       external_txn_id="RSP-202609-RU", deployment_id=dep_ru,
                       platform="俄境流云平台", currency="RUB",
                       gross_amount=110000, received_amount=110000,
                       period_start="2026-09-20", period_end="2026-09-30",
                       settlement_date="2026-10-06"))
    ru_parts = {p["label"]: p for p in ru_line["parts"]}
    assert ru_parts["pre_withdrawal"]["days"] == 2
    assert ru_parts["post_withdrawal"]["days"] == 9
    assert ru_parts["post_withdrawal"]["quarantined"]
    ru_balance = d.market_balance("RU")
    assert ru_balance["quarantined_base"] == ru_parts["post_withdrawal"]["received_base"]
    assert ru_balance["payable_base"] == ru_parts["pre_withdrawal"]["received_base"]

    # 同一外部流水重复到达（平台重推/补发文件）不重复入账
    dup_line = d.import_settlement(
        external_txn_id="PIX-202609-US", deployment_id=dep_us,
        platform="PixPlay", currency="USD",
        gross_amount=10000, received_amount=9200,
        period_start="2026-09-20", period_end="2026-09-30",
        settlement_date="2026-10-05")
    assert dup_line["id"] == us_line["id"]

    # 重启后再次导入：从快照恢复，外部流水幂等键仍在，台账分毫不差
    before = {m: d.market_balance(m) for m in ("US", "KR", "RU")}
    restored = Domain.restore(d.snapshot(), now=clock)
    again = restored.import_settlement(
        external_txn_id="PIX-202609-US", deployment_id=dep_us,
        platform="PixPlay", currency="USD",
        gross_amount=10000, received_amount=9200,
        period_start="2026-09-20", period_end="2026-09-30",
        settlement_date="2026-10-05")
    assert again["id"] == us_line["id"]
    assert len(restored.settlement_lines) == len(d.settlement_lines)
    for market, balance in before.items():
        assert restored.market_balance(market) == balance
    step("重启后再次导入同一批结算：余额逐市场一致", before)

    # 财务逐笔追溯：从外部流水号追到应收、实收、分成与可支付余额
    us_trace = d.settlement_report("PIX-202609-US")
    assert us_trace["line"]["frozen_basis"]["grants"]
    assert us_trace["corrections"][0]["external_txn_id"] == "PIX-202609-US-CORR"

    dashboard = d.dashboard(at=T_WITHDRAW_AFTER)
    return {
        "title": "首部作品《丝路长风·破晓》多地上线样例",
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
            "US_dispute_variance_ratio": us_dispute["variance_ratio"],
            "US_dispute_resolution": d.disputes[us_dispute["id"]]["status"],
            "US_payable_after_adjust": d.market_balance("US")["payable_base"],
            "KR_rounding_residual": d.market_balance("KR")["rounding_residual_base"],
            "RU_quarantined_base": ru_balance["quarantined_base"],
            "RU_payable_pre_withdrawal_only": ru_balance["payable_base"],
            "restart_reimport_balances": before,
        },
        "dashboard": dashboard,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run(), ensure_ascii=False, indent=2))
