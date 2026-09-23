"""领域规则契约测试：逐条锁定业务语义。"""

import unittest

from domain import (
    Domain, DomainError,
    ELEMENT_ALLOWED, ELEMENT_RESTRICTED, ELEMENT_FORBIDDEN,
    PROPOSAL_ACCEPTED,
    ASSET_CONFIRMED, ASSET_UNCONFIRMED,
    DEP_BLOCKED, DEP_ONLINE, DEP_TAKEN_DOWN,
    V_BLOCKED, V_REVIEW_FAILED, V_TAKEN_DOWN,
    DELIVERY_DUPLICATE,
)

NOW = "2026-09-20"
DELIVERY_TS = "2026-09-18T10:00:00+08:00"


class DomainTest(unittest.TestCase):
    def setUp(self):
        self.d = Domain(now=lambda: NOW)
        self.ip = self.d.register_ip("测试IP")
        self.d.add_cultural_element(self.ip["id"], "ok_element", "普通元素")
        self.d.add_cultural_element(self.ip["id"], "sensitive", "敏感元素",
                                    stance=ELEMENT_RESTRICTED,
                                    restricted_markets=["US"])
        self.d.add_cultural_element(self.ip["id"], "banned", "禁用元素",
                                    stance=ELEMENT_FORBIDDEN)
        self.d.register_team("t1", "团队一", hourly_cost=10.0)
        self.lic_ip = self.d.register_license(self.ip["id"], "小说方")
        for market in ("CN", "US"):
            self.d.add_grant(self.lic_ip["id"], market,
                             "2026-01-01", "2028-12-31", share_ratio=0.5)
        self.prop = self.d.create_proposal(
            self.ip["id"], "改编作", target_markets=["CN", "US"])
        self.d.decide_proposal(self.prop["id"], PROPOSAL_ACCEPTED)
        self.d.register_channel("cn-ch", "CN渠道", "CN")
        self.d.register_channel("us-ch", "US渠道", "US")

    def _version(self, market="CN", code=None):
        return self.d.create_version(
            self.prop["id"], code or f"V-{market}", market, team_id="t1")

    def _make_ready(self, market="CN", code=None):
        v = self._version(market, code)
        self.d.register_asset(v["id"], "actor", "演员包",
                              rights_markets=[market],
                              confirmed=ASSET_CONFIRMED)
        self.d.submit_review(v["id"], "顾问", "passed")
        self.d.set_rating(v["id"], "12+", body="分级机构")
        self.d.register_delivery(v["id"], "t1", f"hash:{v['id']}", DELIVERY_TS)
        return v

    # ---- 文化边界 ---------------------------------------------------------

    def test_forbidden_element_cannot_enter_any_version(self):
        v = self._version("US")
        with self.assertRaises(DomainError):
            self.d.use_element(v["id"], "banned")

    def test_restricted_element_blocks_only_listed_markets(self):
        v_cn = self._version("CN", "V-CN")
        v_us = self._version("US", "V-US")
        self.d.use_element(v_cn["id"], "sensitive")
        self.d.use_element(v_us["id"], "sensitive")
        # CN 无文化违规，可取得通过复核；US 含受限元素，不能通过
        self.d.submit_review(v_cn["id"], "顾问", "passed")
        self.assertTrue(self.d.release_check(v_cn["id"])["cultural_ok"])
        check_us = self.d.release_check(v_us["id"])
        self.assertFalse(check_us["cultural_ok"])
        self.assertTrue(any("US" in b and "受限" in b
                            for b in check_us["blockers"]))

    def test_review_cannot_pass_with_cultural_violation(self):
        v = self._version("US")
        self.d.use_element(v["id"], "sensitive")
        with self.assertRaises(DomainError):
            self.d.submit_review(v["id"], "顾问", "passed")

    def test_advisor_tightening_stance_blocks_future_use(self):
        v = self._version("CN")
        self.d.use_element(v["id"], "ok_element")
        self.d.set_element_stance(self.ip["id"], "ok_element",
                                  ELEMENT_FORBIDDEN)
        v2 = self._version("CN", "V-CN-2")
        with self.assertRaises(DomainError):
            self.d.use_element(v2["id"], "ok_element")
        # 已含该元素的旧版本复核随即失败
        self.assertFalse(self.d.release_check(v["id"])["cultural_ok"])

    # ---- 权利矩阵 ---------------------------------------------------------

    def test_market_without_grant_is_blocked(self):
        self.d.add_grant(self.lic_ip["id"], "JP", "2026-01-01", "2028-12-31")
        # JP 不在提案目标市场；CN 有授权，US 有授权；撤销 US 后权利关失败
        self.d.withdraw_grant(
            next(g["id"] for g in self.d.grants.values() if g["market"] == "US"),
            reason="测试")
        v = self._version("US")
        self.assertFalse(self.d.release_check(v["id"])["rights_ok"])

    def test_grant_window_enforced_by_date(self):
        v = self._version("CN")
        self.assertFalse(self.d.release_check(v["id"], at="2025-12-31")["rights_ok"])
        self.assertFalse(self.d.release_check(v["id"], at="2029-01-01")["rights_ok"])
        self.assertTrue(self.d.release_check(v["id"], at="2027-01-01")["rights_ok"])

    def test_asset_level_license_required_only_when_included(self):
        music_lic = self.d.register_license(self.ip["id"], "音乐方",
                                            coverage="asset")
        v = self._make_ready("CN")
        self.d.register_asset(v["id"], "song", "歌曲",
                              rights_markets=["CN"], confirmed=ASSET_CONFIRMED,
                              licensor="音乐方")
        # 默认进入成片但无音乐授权 → 权利关失败
        self.assertFalse(self.d.release_check(v["id"])["rights_ok"])
        self.d.add_grant(music_lic["id"], "CN", "2026-01-01", "2027-12-31",
                         share_ratio=0.1)
        self.assertTrue(self.d.release_check(v["id"])["ready"])
        # US 版排除音乐后不需要音乐授权
        v_us = self._version("US", "V-US")
        self.d.register_asset(v_us["id"], "song", "歌曲",
                              rights_markets=["CN"], confirmed=ASSET_CONFIRMED,
                              licensor="音乐方")
        self.d.set_asset_included(v_us["id"], "song", False,
                                  reason="US 用本地配乐")
        self.d.register_asset(v_us["id"], "actor", "演员",
                              rights_markets=["US"], confirmed=ASSET_CONFIRMED)
        self.d.submit_review(v_us["id"], "顾问", "passed")
        self.d.set_rating(v_us["id"], "PG")
        self.d.register_delivery(v_us["id"], "t1", "hash:us", DELIVERY_TS)
        self.assertTrue(self.d.release_check(v_us["id"])["ready"])

    def test_cannot_create_version_outside_proposal_markets(self):
        with self.assertRaises(DomainError):
            self.d.create_version(self.prop["id"], "V-JP", "JP", team_id="t1")

    def test_overlapping_grant_window_rejected(self):
        with self.assertRaises(DomainError):
            self.d.add_grant(self.lic_ip["id"], "CN",
                             "2027-01-01", "2029-12-31")

    # ---- 素材确认与地区发料 -----------------------------------------------

    def test_unconfirmed_asset_withheld_until_confirmed(self):
        v = self._version("CN")
        self.d.register_asset(v["id"], "song", "歌曲",
                              rights_markets=["CN"],
                              confirmed=ASSET_UNCONFIRMED)
        first = self.d.assign_assets(v["id"], "t1")
        self.assertEqual(first["released_assets"], [])
        self.assertEqual(first["withheld_assets"][0]["reason"], "演员或音乐尚未确认")
        self.d.decide_asset(v["id"], "song", ASSET_CONFIRMED)
        second = self.d.assign_assets(v["id"], "t1", asset_keys=["song"])
        self.assertEqual(second["released_assets"], ["song"])

    def test_asset_rights_not_covering_team_region_withheld(self):
        v = self._version("US")
        self.d.register_asset(v["id"], "song", "歌曲",
                              rights_markets=["CN"],
                              confirmed=ASSET_CONFIRMED)
        assignment = self.d.assign_assets(v["id"], "t1")
        self.assertEqual(assignment["released_assets"], [])
        self.assertIn("US", assignment["withheld_assets"][0]["reason"])

    def test_release_blocked_by_unconfirmed_asset_in_cut(self):
        v = self._version("CN")
        self.d.register_asset(v["id"], "song", "歌曲",
                              rights_markets=["CN"],
                              confirmed=ASSET_UNCONFIRMED)
        self.d.submit_review(v["id"], "顾问", "passed")
        self.d.set_rating(v["id"], "12+")
        self.d.register_delivery(v["id"], "t1", "hash:x", DELIVERY_TS)
        result = self.d.request_release(v["id"], ["cn-ch"])
        self.assertEqual(result["results"][0]["status"], DEP_BLOCKED)
        self.assertTrue(any("尚未确认" in b
                            for b in result["results"][0]["blockers"]))

    # ---- 工时锁定与返工 ---------------------------------------------------

    def test_logged_hours_locked_despite_rate_change(self):
        v = self._version("CN")
        self.d.log_hours(v["id"], "t1", 10, activity="剪辑")
        self.assertEqual(self.d.labor_totals(v["id"])["cost"], 100.0)
        self.d.teams["t1"]["hourly_cost"] = 99.0
        totals = self.d.labor_totals(v["id"])
        self.assertEqual(totals["cost"], 100.0)
        self.assertTrue(totals["locked"])

    def test_rework_does_not_rewrite_locked_hours(self):
        v1 = self._version("CN", "V-CN-v1")
        self.d.log_hours(v1["id"], "t1", 10, activity="初剪")
        self.d.submit_review(v1["id"], "顾问", "failed", note="需要返工")
        self.assertEqual(self.d.versions[v1["id"]]["status"], V_REVIEW_FAILED)

        v2 = self.d.create_version(
            self.prop["id"], "V-CN-v2", "CN", team_id="t1",
            parent_id=v1["id"], note="返工")
        self.d.log_hours(v2["id"], "t1", 3, activity="返工修订")
        self.assertEqual(self.d.labor_totals(v1["id"])["hours"], 10.0)
        self.assertEqual(self.d.labor_totals(v2["id"])["hours"], 3.0)
        self.assertEqual(self.d.versions[v2["id"]]["parent_id"], v1["id"])

    # ---- 跨时区交付幂等 ---------------------------------------------------

    def test_duplicate_delivery_across_timezones_counted_once(self):
        v = self._version("CN")
        first = self.d.register_delivery(
            v["id"], "t1", "hash:same", "2026-09-18T22:30:00+03:00")
        second = self.d.register_delivery(
            v["id"], "t1", "hash:same", "2026-09-19T03:30:00+08:00")
        third = self.d.register_delivery(
            v["id"], "t1", "hash:same", "2026-09-18T15:30:00-04:00")
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], DELIVERY_DUPLICATE)
        self.assertEqual(third["status"], DELIVERY_DUPLICATE)
        self.assertEqual(second["first_received_at"],
                         "2026-09-18T19:30:00+00:00")
        accepted = [d for d in self.d.deliveries.values()
                    if d["version_id"] == v["id"]]
        self.assertEqual(len(accepted), 1)

    def test_delivery_requires_timezone(self):
        v = self._version("CN")
        with self.assertRaises(DomainError):
            self.d.register_delivery(v["id"], "t1", "h", "2026-09-18T10:00:00")

    # ---- 发行三关 ---------------------------------------------------------

    def test_ready_version_releases_and_freezes_revenue_basis(self):
        v = self._make_ready("CN")
        self.d.log_hours(v["id"], "t1", 8)
        result = self.d.request_release(v["id"], ["cn-ch"])
        self.assertEqual(result["results"][0]["status"], DEP_ONLINE)
        report = self.d.version_report(v["id"])
        basis = report["revenue_basis"]
        self.assertEqual(basis["locked_hours"], 8.0)
        self.assertEqual(basis["grants"][0]["licensor"], "小说方")
        self.assertEqual(basis["frozen_at"], NOW)

    def test_release_requires_all_three_gates(self):
        v = self._version("CN")  # 无复核、无分级、无交付
        result = self.d.request_release(v["id"], ["cn-ch"])
        blockers = result["results"][0]["blockers"]
        self.assertEqual(result["results"][0]["status"], DEP_BLOCKED)
        self.assertTrue(any("文化" in b for b in blockers))
        self.assertTrue(any("分级" in b for b in blockers))
        self.assertTrue(any("交付" in b for b in blockers))

    def test_channel_market_must_match_version(self):
        v = self._make_ready("CN")
        with self.assertRaises(DomainError):
            self.d.request_release(v["id"], ["us-ch"])

    def test_repeated_release_request_does_not_duplicate_deployment(self):
        v = self._make_ready("CN")
        first = self.d.request_release(v["id"], ["cn-ch"])
        second = self.d.request_release(v["id"], ["cn-ch"])
        self.assertEqual(first["results"][0]["deployment_id"],
                         second["results"][0]["deployment_id"])
        deps = [d for d in self.d.deployments.values()
                if d["version_id"] == v["id"]]
        self.assertEqual(len(deps), 1)

    # ---- 撤权 -------------------------------------------------------------

    def test_withdraw_grant_blocks_unpublished_and_lists_channels(self):
        v = self._make_ready("CN")
        # 发行前撤权：即使先请求发行被阻断，撤权影响也要列出该市场渠道
        self.d.request_release(v["id"], ["cn-ch"])  # 先上线……
        # 换一个尚未发布的 CN 版本验证阻断
        v2 = self._make_ready("CN", "V-CN-2")
        grant_cn = next(g for g in self.d.grants.values() if g["market"] == "CN")
        impact = self.d.withdraw_grant(grant_cn["id"], reason="谈判破裂")
        # v1 在线 → 下架；v2 未发布 → 阻断
        self.assertEqual(
            self.d.version_report(v["id"])["deployments"][0]["status"],
            DEP_TAKEN_DOWN)
        self.assertEqual(self.d.versions[v2["id"]]["status"], V_BLOCKED)
        self.assertIn("cn-ch", impact["affected_channels"])
        kinds = {c["version_id"]: c["kind"]
                 for c in self.d.dashboard()["affected_copies"]}
        self.assertEqual(kinds[v["id"]], "已上线副本下架")
        self.assertEqual(kinds[v2["id"]], "未发布版本阻断")

    def test_withdraw_grant_takes_down_online_copy_but_keeps_basis(self):
        v = self._make_ready("CN")
        self.d.log_hours(v["id"], "t1", 5)
        self.d.request_release(v["id"], ["cn-ch"])
        grant_cn = next(g for g in self.d.grants.values() if g["market"] == "CN")
        self.d.withdraw_grant(grant_cn["id"], reason="终止合作", at="2026-09-25")
        report = self.d.version_report(v["id"], at="2026-09-25")
        self.assertEqual(report["status"], V_TAKEN_DOWN)
        # 收益依据是上线时冻结快照，撤权不改账
        self.assertEqual(report["revenue_basis"]["locked_hours"], 5.0)
        self.assertIn("冻结", report["revenue_basis"]["state"])

    def test_withdraw_grant_releases_team_assignments(self):
        v = self._version("CN")
        self.d.register_asset(v["id"], "actor", "演员",
                              rights_markets=["CN"], confirmed=ASSET_CONFIRMED)
        self.d.assign_assets(v["id"], "t1")
        grant_cn = next(g for g in self.d.grants.values() if g["market"] == "CN")
        impact = self.d.withdraw_grant(grant_cn["id"], reason="测试")
        self.assertTrue(impact["released_assignments"])
        assignment = self.d.assignments[impact["released_assignments"][0]]
        self.assertEqual(assignment["status"], "released")

    def test_double_withdraw_rejected(self):
        grant_cn = next(g for g in self.d.grants.values() if g["market"] == "CN")
        self.d.withdraw_grant(grant_cn["id"])
        with self.assertRaises(DomainError):
            self.d.withdraw_grant(grant_cn["id"])

    # ---- 看板 -------------------------------------------------------------

    def test_dashboard_shows_markets_blockers_and_basis(self):
        v_ready = self._make_ready("CN", "V-CN")
        self.d.request_release(v_ready["id"], ["cn-ch"])
        v_pending = self._version("US", "V-US")
        board = self.d.dashboard()
        cn_entries = board["market_index"]["CN"]
        self.assertTrue(any(e["state"] == DEP_ONLINE for e in cn_entries))
        us_blockers = next(b for b in board["open_blockers"]
                           if b["version_code"] == "V-US")
        self.assertTrue(us_blockers["blockers"])
        # 撤权后下架副本独立列出
        grant_cn = next(g for g in self.d.grants.values() if g["market"] == "CN")
        self.d.withdraw_grant(grant_cn["id"], reason="x")
        board2 = self.d.dashboard()
        self.assertTrue(any(c["kind"] == "已上线副本下架"
                            for c in board2["affected_copies"]))
        cn_states = {e["state"] for e in board2["market_index"]["CN"]}
        self.assertIn(DEP_TAKEN_DOWN, cn_states)


if __name__ == "__main__":
    unittest.main()
