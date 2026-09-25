"""财务结算契约测试：迟到结算、幂等、撤权隔离、争议双批准与重启一致性。"""

import unittest

from domain import (
    Domain, DomainError,
    PROPOSAL_ACCEPTED, ASSET_CONFIRMED,
    ENTRY_REFUND, ENTRY_CORRECTION,
    DISPUTE_OPEN, DISPUTE_RELEASED, DISPUTE_ADJUSTED,
)

NOW = "2026-09-20"
DELIVERY_TS = "2026-09-18T10:00:00+08:00"


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.d = Domain(now=lambda: NOW)
        self.ip = self.d.register_ip("结算测试IP")
        self.d.register_team("t1", "团队", hourly_cost=10.0)
        self.lic = self.d.register_license(self.ip["id"], "小说方")
        self.grant = self.d.add_grant(self.lic["id"], "CN", "2026-01-01",
                                      "2028-12-31", share_ratio=0.5)
        self.d.add_grant(self.lic["id"], "US", "2026-01-01",
                         "2028-12-31", share_ratio=0.6)
        prop = self.d.create_proposal(self.ip["id"], "改编",
                                      target_markets=["CN", "US"])
        self.d.decide_proposal(prop["id"], PROPOSAL_ACCEPTED)
        self.d.register_channel("cn-ch", "CN渠道", "CN")
        self.d.register_channel("us-ch", "US渠道", "US")
        self.dep_cn = self._release("CN")
        self.dep_us = self._release("US")

    def _release(self, market):
        v = self.d.create_version(self._prop_id(), f"V-{market}", market,
                                  team_id="t1")
        self.d.register_asset(v["id"], "actor", "演员",
                              rights_markets=[market], confirmed=ASSET_CONFIRMED)
        self.d.submit_review(v["id"], "顾问", "passed")
        self.d.set_rating(v["id"], "12+")
        self.d.register_delivery(v["id"], "t1", f"hash:{market}", DELIVERY_TS)
        channel = "cn-ch" if market == "CN" else "us-ch"
        result = self.d.request_release(v["id"], [channel], at=NOW)
        assert result["results"][0]["status"] == "online"
        return result["results"][0]["deployment_id"]

    def _prop_id(self):
        return next(p["id"] for p in self.d.proposals.values())

    def _import(self, txn, dep=None, market="CN", gross=1000, received=1000,
                currency="USD", period=("2026-09-01", "2026-09-15"),
                settled="2026-09-25", **kwargs):
        return self.d.import_settlement(
            external_txn_id=txn, deployment_id=dep or self.dep_cn,
            market=market, currency=currency,
            gross_amount=gross, received_amount=received,
            period_start=period[0], period_end=period[1],
            settlement_date=settled, **kwargs)

    # ---- 关联与冻结规则 -----------------------------------------------------

    def test_line_links_deployment_and_frozen_basis(self):
        line = self._import("T-1")
        self.assertEqual(line["deployment_id"], self.dep_cn)
        self.assertEqual(line["frozen_basis"]["grants"][0]["licensor"], "小说方")
        shares = {a["party"]: a["share_ratio"] for a in line["allocations"]}
        self.assertEqual(shares["小说方"], 0.5)
        self.assertEqual(shares["制作方"], 0.5)
        receivable = {a["party"]: a["receivable_base"] for a in line["allocations"]}
        self.assertEqual(receivable["小说方"], 500.0)

    def test_blocked_deployment_has_no_basis_to_settle(self):
        v = self.d.create_version(self._prop_id(), "V-CN-late", "CN", team_id="t1")
        blocked = self.d.request_release(v["id"], ["cn-ch"])
        dep_id = blocked["results"][0]["deployment_id"]
        with self.assertRaises(DomainError):
            self._import("T-blocked", dep=dep_id)

    def test_market_mismatch_rejected(self):
        with self.assertRaises(DomainError):
            self._import("T-mm", market="US")   # dep_cn 属于 CN 市场

    # ---- 幂等：同一外部流水只入账一次 ----------------------------------------

    def test_duplicate_external_txn_counted_once(self):
        first = self._import("T-1")
        second = self._import("T-1", gross=9999, received=1)  # 内容不同也不入账
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.d.settlement_lines), 1)
        self.assertEqual(self.d.market_balance("CN")["received_base"], 1000.0)

    # ---- 汇率按结算日版本化 ---------------------------------------------------

    def test_fx_rate_versioned_by_settlement_date(self):
        self.d.set_fx_rate("KRW", 0.00070, "2026-09-01")
        self.d.set_fx_rate("KRW", 0.00080, "2026-10-01")
        sep = self._import("T-sep", currency="KRW", gross=1000000,
                           received=1000000, settled="2026-09-15")
        oct_ = self._import("T-oct", currency="KRW", gross=1000000,
                            received=1000000, settled="2026-10-02")
        self.assertEqual(sep["fx_rate"], 0.00070)
        self.assertEqual(oct_["fx_rate"], 0.00080)
        self.assertEqual(sep["gross_base"], 700.0)
        self.assertEqual(oct_["gross_base"], 800.0)

    def test_fx_rate_immutable_and_missing_rate_rejected(self):
        self.d.set_fx_rate("KRW", 0.00070, "2026-09-01")
        with self.assertRaises(DomainError):
            self.d.set_fx_rate("KRW", 0.00099, "2026-09-01")  # 同日改版被拒
        with self.assertRaises(DomainError):
            self._import("T-nofx", currency="JPY")            # 无汇率无法换算

    def test_cross_currency_rounding_residual_tracked(self):
        self.d.set_fx_rate("KRW", 0.00075, "2026-09-01")
        line = self._import("T-round", currency="KRW", gross=6666667,
                            received=6666667)
        self.assertNotEqual(line["rounding_residual"], 0.0)
        residual = self.d.market_balance("CN")["rounding_residual_base"]
        self.assertAlmostEqual(residual, line["rounding_residual"])

    # ---- 退款与更正只追加 -----------------------------------------------------

    def test_refund_appends_and_reduces_balance(self):
        self._import("T-1", gross=1000, received=1000)
        self._import("T-1-RF", entry_type=ENTRY_REFUND, corrects_txn_id="T-1",
                     gross=200, received=200)
        balance = self.d.market_balance("CN")
        self.assertEqual(balance["receivable_base"], 800.0)
        self.assertEqual(balance["received_base"], 800.0)
        # 原行不被改写
        self.assertEqual(self.d.settlement_lines["T-1"]["received_base"], 1000.0)

    def test_refund_requires_original_settlement(self):
        with self.assertRaises(DomainError):
            self._import("T-rf-x", entry_type=ENTRY_REFUND,
                         corrects_txn_id="NO-SUCH", gross=1, received=1)
        with self.assertRaises(DomainError):
            self._import("T-rf-y", entry_type=ENTRY_REFUND, gross=1, received=1)

    # ---- 撤权边界：撤回后新增收入单独隔离 --------------------------------------

    def test_post_withdrawal_revenue_quarantined_by_period(self):
        self.d.withdraw_grant(self.grant["id"], reason="终止合作",
                              at="2026-09-10")
        line = self._import("T-wd", gross=1500, received=1500,
                            period=("2026-09-01", "2026-09-15"))
        parts = {p["label"]: p for p in line["parts"]}
        self.assertEqual(parts["pre_withdrawal"]["days"], 9)
        self.assertEqual(parts["post_withdrawal"]["days"], 6)
        self.assertTrue(parts["post_withdrawal"]["quarantined"])
        balance = self.d.market_balance("CN")
        self.assertEqual(balance["quarantined_base"], 600.0)
        # 可支付只含撤回前部分
        self.assertEqual(balance["payable_base"], 900.0)

    def test_period_fully_after_withdrawal_all_quarantined(self):
        self.d.withdraw_grant(self.grant["id"], reason="终止合作",
                              at="2026-09-01")
        self._import("T-wd-all", gross=100, received=100,
                     period=("2026-09-05", "2026-09-08"))
        balance = self.d.market_balance("CN")
        self.assertEqual(balance["quarantined_base"], 100.0)
        self.assertEqual(balance["payable_base"], 0.0)

    # ---- 争议：超阈值自动建立、冻结市场、双批准结案 ------------------------------

    def test_variance_over_threshold_opens_dispute_and_freezes_market(self):
        self._import("T-ok", gross=1000, received=990)      # 1% 阈值内
        self.assertFalse(self.d.market_balance("CN")["frozen"])
        self._import("T-bad", gross=1000, received=900,
                     period=("2026-09-16", "2026-09-30"))   # 10% 超阈值
        balance = self.d.market_balance("CN")
        self.assertTrue(balance["frozen"])
        self.assertEqual(balance["payable_base"], 0.0)      # 全市场冻结
        dispute = next(iter(self.d.disputes.values()))
        self.assertEqual(dispute["status"], DISPUTE_OPEN)
        self.assertEqual(dispute["variance_ratio"], 0.1)

    def test_dispute_requires_both_parties_to_release(self):
        self._import("T-bad", gross=1000, received=900)
        dispute = next(iter(self.d.disputes.values()))
        self.d.approve_dispute(dispute["id"], "producer", "制片-王")
        with self.assertRaises(DomainError):
            self.d.resolve_dispute(dispute["id"], "release")
        self.d.approve_dispute(dispute["id"], "rights_holder", "小说方-李")
        self.d.resolve_dispute(dispute["id"], "release")
        self.assertEqual(dispute["status"], DISPUTE_RELEASED)
        self.assertEqual(self.d.market_balance("CN")["payable_base"], 900.0)
        with self.assertRaises(DomainError):                # 已结案不能再批
            self.d.approve_dispute(dispute["id"], "producer", "制片-王")

    def test_adjust_resolution_appends_correction_line(self):
        self._import("T-bad", gross=1000, received=900)
        dispute = next(iter(self.d.disputes.values()))
        self.d.approve_dispute(dispute["id"], "producer", "制片-王")
        self.d.approve_dispute(dispute["id"], "rights_holder", "小说方-李")
        self.d.resolve_dispute(dispute["id"], "adjust", correction={
            "external_txn_id": "T-bad-CORR", "received_amount": 95,
            "settlement_date": "2026-09-28"})
        self.assertEqual(dispute["status"], DISPUTE_ADJUSTED)
        corr = self.d.settlement_lines["T-bad-CORR"]
        self.assertEqual(corr["entry_type"], ENTRY_CORRECTION)
        self.assertEqual(corr["dispute_id"], dispute["id"])
        balance = self.d.market_balance("CN")
        self.assertFalse(balance["frozen"])
        self.assertEqual(balance["received_base"], 995.0)

    def test_market_threshold_override(self):
        self.d.set_variance_rule(0.5, market="CN")
        self._import("T-1", gross=1000, received=600)   # 40% 但阈值 50%
        self.assertFalse(self.d.market_balance("CN")["frozen"])

    # ---- 逐笔追溯 -------------------------------------------------------------

    def test_settlement_report_traces_line_to_ledger(self):
        self._import("T-1", gross=1000, received=1000)
        self._import("T-1-RF", entry_type=ENTRY_REFUND, corrects_txn_id="T-1",
                     gross=100, received=100)
        report = self.d.settlement_report("T-1")
        self.assertEqual(report["line"]["external_txn_id"], "T-1")
        self.assertEqual(report["corrections"][0]["external_txn_id"], "T-1-RF")
        self.assertEqual(report["market_ledger"]["received_base"], 900.0)
        # 版本报告也能追到结算行
        version_id = self.d.deployments[self.dep_cn]["version_id"]
        v_report = self.d.version_report(version_id)
        self.assertIn(self.dep_cn, v_report["settlements"])

    # ---- 重启后再次导入仍一致 ---------------------------------------------------

    def test_restore_then_reimport_is_idempotent_and_consistent(self):
        self.d.set_fx_rate("KRW", 0.00075, "2026-09-01")
        self._import("T-1", gross=1000, received=900)       # 触发争议
        self._import("T-2", currency="KRW", gross=777777, received=777777,
                     period=("2026-09-16", "2026-09-30"))
        before = self.d.market_balance("CN")
        restored = Domain.restore(self.d.snapshot(), now=lambda: NOW)
        again = restored.import_settlement(
            external_txn_id="T-1", deployment_id=self.dep_cn,
            currency="USD", gross_amount=1000, received_amount=900,
            period_start="2026-09-01", period_end="2026-09-15",
            settlement_date="2026-09-25")
        self.assertEqual(again["id"], self.d.settlement_lines["T-1"]["id"])
        self.assertEqual(len(restored.settlement_lines), 2)
        self.assertEqual(restored.market_balance("CN"), before)
        # 恢复后新结算照常入账，ID 序列不冲突
        new_line = restored.import_settlement(
            external_txn_id="T-3", deployment_id=self.dep_cn,
            currency="USD", gross_amount=50, received_amount=50,
            period_start="2026-10-01", period_end="2026-10-05",
            settlement_date="2026-10-06")
        self.assertNotIn(new_line["id"],
                         [l["id"] for l in self.d.settlement_lines.values()])


if __name__ == "__main__":
    unittest.main()
