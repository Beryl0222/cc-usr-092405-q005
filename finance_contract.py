"""结算与对账契约测试。

覆盖：
* 迟到结算按结算日取用版本化汇率，已引用汇率不可改写；
* 结算行关联已发行副本与上线时冻结的分成规则，未发行不能结算；
* 同一外部流水重复到达只入账一次（含进程重启后）；
* 退款/更正只能追加，金额负向分配，各方舍入后逐分配平；
* 授权撤回边界后的新增收入隔离，不进应收/实收/可支付；
* 差异超阈值自动建争议并冻结市场可分配余额；
* 制作方与权利方分别批准后才释放；任一方拒绝维持冻结、可翻案；
* 双批准后更正闭环释放；更正不足则争议重开、批准清零；
* 逐笔可追到应收、实收、可支付；
* 快照/恢复与 HTTP 进程重启后再次导入账目始终一致。
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from domain import (
    Domain, DomainError,
    PROPOSAL_ACCEPTED, ASSET_CONFIRMED,
    DEP_ONLINE, GRANT_WITHDRAWN,
    SETTLE_DUPLICATE, SETTLE_QUARANTINED, SETTLE_IMPORTED,
    DISPUTE_OPEN, DISPUTE_APPROVED, DISPUTE_RESOLVED, DISPUTE_REJECTED,
    BASE_CURRENCY,
)

NOW = "2026-09-25"
DELIVERY_TS = "2026-09-18T10:00:00+08:00"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http_json(url, payload=None, method=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = method or "POST"
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        body = json.load(error)
        error.close()
        return error.code, body


class FinanceDomainTest(unittest.TestCase):
    def setUp(self):
        self.d = Domain(now=lambda: NOW)
        self.ip = self.d.register_ip("财务测试IP")
        self.d.register_team("t1", "制片团队", hourly_cost=40.0)
        self.lic_novel = self.d.register_license(self.ip["id"], "小说方")
        self.d.add_grant(self.lic_novel["id"], "US",
                         "2026-01-01", "2028-12-31",
                         share_ratio=0.6, currency="USD")
        self.prop = self.d.create_proposal(
            self.ip["id"], "改编作", target_markets=["US"])
        self.d.decide_proposal(self.prop["id"], PROPOSAL_ACCEPTED)
        self.d.register_channel("us-pix", "美洲 PixPlay", "US")
        self.d.set_dispute_rule("US", threshold=0.1, absolute="5.00")
        self.version = self._release("V-US", "us-pix")
        self.dep_id = self._dep(self.version["id"], "us-pix")

    def _release(self, code, channel):
        v = self.d.create_version(
            self.prop["id"], code, "US", team_id="t1")
        self.d.register_asset(v["id"], "actor", "演员包",
                              rights_markets=["US"],
                              confirmed=ASSET_CONFIRMED)
        self.d.submit_review(v["id"], "顾问", "passed")
        self.d.set_rating(v["id"], "PG-13")
        self.d.register_delivery(v["id"], "t1", f"hash:{v['id']}", DELIVERY_TS)
        self.d.request_release(v["id"], [channel])
        return v

    def _dep(self, version_id, channel):
        return next(d["id"] for d in self.d.deployments.values()
                    if d["version_id"] == version_id
                    and d["channel_code"] == channel)

    def _line(self, ref, **over):
        line = {
            "deployment_id": self.dep_id,
            "external_ref": ref,
            "gross": "100.00",
            "currency": BASE_CURRENCY,
            "report_amount": "100.00",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
        }
        line.update(over)
        return line

    def _import(self, lines, date="2026-10-15", channel="us-pix"):
        return self.d.import_settlement(channel, lines, date)

    # ---- 汇率版本化与迟到结算 ---------------------------------------------

    def test_settlement_requires_fx_rate_for_settlement_date(self):
        with self.assertRaises(DomainError) as caught:
            self._import([self._line("e1", currency="EUR")], "2026-10-15")
        self.assertEqual(caught.exception.code, "MissingFxRate")

    def test_late_settlement_uses_its_own_settlement_date_fx(self):
        self.d.set_fx_rate("EUR", "1.08", "2026-10-15")
        self.d.set_fx_rate("EUR", "1.10", "2026-11-15")
        first = self._import([self._line("e1", currency="EUR")], "2026-10-15")
        late = self._import([self._line(
            "e2", currency="EUR", gross="50.00", report_amount="50.00")],
            "2026-11-15")
        self.assertEqual(first["results"][0]["base_amount"], "108.00")
        self.assertEqual(late["results"][0]["base_amount"], "55.00")
        line2 = self.d.settlement_lines[late["results"][0]["line_id"]]
        self.assertEqual(line2["fx"]["as_of"], "2026-11-15")
        totals = self.d._market_totals("US")
        self.assertEqual(totals["received_base"], "163.00")
        self.assertEqual(totals["receivable_base"], "163.00")

    def test_fx_rate_is_immutable_once_referenced(self):
        self.d.set_fx_rate("EUR", "1.08", "2026-10-15")
        self._import([self._line("e1", currency="EUR")], "2026-10-15")
        with self.assertRaises(DomainError):
            self.d.set_fx_rate("EUR", "9.99", "2026-10-15")
        # 未被引用的结算日仍可登记
        self.d.set_fx_rate("EUR", "1.09", "2026-10-16")

    # ---- 关联已发行版本与冻结分成 -----------------------------------------

    def test_settlement_requires_released_deployment(self):
        v2 = self.d.create_version(
            self.prop["id"], "V-US-2", "US", team_id="t1")
        with self.assertRaises(DomainError):
            self.d.import_settlement(
                "us-pix",
                [{"deployment_id": "dep_does_not_exist",
                  "external_ref": "x", "gross": "1", "currency": "USD"}],
                "2026-10-15")
        self.assertIsNotNone(v2)

    def test_line_trace_links_version_frozen_rules_and_ledger(self):
        out = self._import([self._line("t1")])
        line_id = out["results"][0]["line_id"]
        trace = self.d.line_trace(line_id)
        path = trace["path"]
        self.assertEqual(path["deployment_id"], self.dep_id)
        self.assertEqual(path["deployment_status"], DEP_ONLINE)
        self.assertEqual(path["version_code"], "V-US")
        self.assertEqual(path["frozen_grants"][0]["licensor"], "小说方")
        self.assertEqual(path["frozen_grants"][0]["share_ratio"], 0.6)
        self.assertEqual(path["basis_frozen_at"], NOW)
        self.assertEqual(trace["ledger"]["receivable_base"], "100.00")
        self.assertEqual(trace["ledger"]["received_base"], "100.00")
        self.assertEqual(trace["ledger"]["payable_base"], "100.00")
        self.assertFalse(trace["ledger"]["market_frozen"])
        # 撤回授权不改变已冻结快照
        grant = next(g for g in self.d.grants.values() if g["market"] == "US")
        self.d.withdraw_grant(grant["id"], reason="x", at="2026-12-01")
        trace2 = self.d.line_trace(line_id)
        self.assertEqual(trace2["path"]["frozen_grants"][0]["share_ratio"], 0.6)

    def test_settlement_channel_must_match_deployment(self):
        self.d.register_channel("us-wave", "Wave", "US")
        with self.assertRaises(DomainError):
            self.d.import_settlement(
                "us-wave", [self._line("x")], "2026-10-15")

    # ---- 外部流水幂等 -----------------------------------------------------

    def test_duplicate_external_ref_never_posts_twice(self):
        first = self._import([self._line("dup-1")])
        self.assertEqual(first["results"][0]["status"], SETTLE_IMPORTED)
        again = self._import([self._line(
            "dup-1", gross="999.00", report_amount="999.00")])
        self.assertEqual(again["results"][0]["status"], SETTLE_DUPLICATE)
        self.assertEqual(again["results"][0]["line_id"],
                         first["results"][0]["line_id"])
        totals = self.d._market_totals("US")
        self.assertEqual(totals["received_base"], "100.00")
        self.assertEqual(len(self.d.settlement_lines), 1)
        self.assertEqual(len(self.d.duplicate_imports), 1)

    def test_duplicate_replay_after_snapshot_restore_still_consistent(self):
        self._import([self._line("snap-1", gross="80.00",
                                 report_amount="100.00")])
        before = self.d.finance_report()
        restored = Domain(now=lambda: NOW).load_state(self.d.snapshot())
        replay = restored.import_settlement(
            "us-pix", [self._line("snap-1")], "2026-10-15")
        self.assertEqual(replay["results"][0]["status"], SETTLE_DUPLICATE)
        self.assertEqual(restored.finance_report()["markets"],
                         before["markets"])

    # ---- 退款/更正追加与舍入配平 ------------------------------------------

    def test_refunds_are_append_only_negative_and_balanced_to_cent(self):
        self._import([self._line("sale-1", gross="100.00")])
        refund = self._import([self._line(
            "ref-1", kind="refund", gross="3.33", report_amount="0.00")])
        line = self.d.settlement_lines[refund["results"][0]["line_id"]]
        self.assertEqual(str(line["gross_amount"]), "-3.33")
        self.assertEqual(len(self.d.settlement_lines), 2)
        total = sum(float(s["amount"]) for s in line["shares"])
        self.assertAlmostEqual(total, -3.33, places=2)
        # 历史行不可改写：同流水重放（哪怕金额改成 999）只判重，账目不动
        replay = self._import([self._line(
            "ref-1", kind="refund", gross="999.00", report_amount="0.00")])
        self.assertEqual(replay["results"][0]["status"], SETTLE_DUPLICATE)
        self.assertEqual(len(self.d.settlement_lines), 2)
        self.assertEqual(str(line["gross_amount"]), "-3.33")
        # 更正只能以新流水追加；复用已入账流水只判重，不新增账目
        replay_correction = self.d.append_correction(
            self.dep_id, "ref-1", "adjustment", "1", "USD", "2026-10-16")
        self.assertEqual(replay_correction["result"]["status"],
                         SETTLE_DUPLICATE)
        self.assertEqual(len(self.d.settlement_lines), 2)

    def test_cross_currency_rounding_is_absorbed_by_producer_and_balances(self):
        lic_music = self.d.register_license(self.ip["id"], "音乐方")
        self.d.add_grant(lic_music["id"], "US", "2026-01-01", "2027-12-31",
                         share_ratio=0.333, currency="USD")
        # 音乐方素材授权进入核验：新副本重走发行
        v = self.d.create_version(
            self.prop["id"], "V-US-music", "US", team_id="t1")
        self.d.register_asset(v["id"], "actor", "演员",
                              rights_markets=["US"],
                              confirmed=ASSET_CONFIRMED)
        self.d.register_asset(v["id"], "song", "主题曲",
                              rights_markets=["US"], confirmed=ASSET_CONFIRMED,
                              licensor="音乐方")
        self.d.submit_review(v["id"], "顾问", "passed")
        self.d.set_rating(v["id"], "PG-13")
        self.d.register_delivery(v["id"], "t1", "hash:music", DELIVERY_TS)
        self.d.request_release(v["id"], ["us-pix"])
        dep_id = self._dep(v["id"], "us-pix")
        self.d.set_fx_rate("JPY", "0.00675", "2026-10-15")
        out = self.d.import_settlement("us-pix", [{
            "deployment_id": dep_id, "external_ref": "jpy-1",
            "gross": "10003", "currency": "JPY",
            "report_amount": "10003",
            "period_end": "2026-09-30"}], "2026-10-15")
        line = self.d.settlement_lines[out["results"][0]["line_id"]]
        self.assertEqual(str(line["base_amount"]), "67.52")
        shares = {s["party"]: s["amount"] for s in line["shares"]}
        # 小说方 0.6 → 40.51、音乐方 0.333 → 22.48；权利方各自四舍五入，
        # 制作方（0.067）理论 4.52，实分 4.53 吸收 1 分尾差，三方精确配平
        self.assertEqual(shares["小说方"], "40.51")
        self.assertEqual(shares["音乐方"], "22.48")
        self.assertEqual(shares["制作方"], "4.53")
        producer = next(s for s in line["shares"] if s["party"] == "制作方")
        self.assertEqual(producer["rounding_absorbed"], "0.01")
        total = sum(float(s["amount"]) for s in line["shares"])
        self.assertAlmostEqual(total, 67.52, places=2)

    # ---- 撤回边界隔离 ------------------------------------------------------

    def test_revenue_after_withdrawal_boundary_is_quarantined(self):
        grant = next(g for g in self.d.grants.values() if g["market"] == "US")
        self.d.withdraw_grant(grant["id"], reason="终止合作", at="2026-10-01")

        before = self._import([self._line(
            "pre-1", period_end="2026-09-30")], "2026-10-20")
        self.assertEqual(before["results"][0]["status"], SETTLE_IMPORTED)

        # 周期末日恰为撤回日：无法证明收入产生于撤回之前，隔离
        edge = self._import([self._line(
            "edge-1", period_start="2026-09-20", period_end="2026-10-01")],
            "2026-10-20")
        self.assertEqual(edge["results"][0]["status"], SETTLE_QUARANTINED)

        after = self._import([self._line(
            "post-1", period_start="2026-10-02", period_end="2026-10-10")],
            "2026-10-20")
        self.assertEqual(after["results"][0]["status"], SETTLE_QUARANTINED)

        # 不带周期时按结算日判定
        no_period = self.d.import_settlement("us-pix", [{
            "deployment_id": self.dep_id, "external_ref": "np-1",
            "gross": "10", "currency": "USD"}], "2026-10-20")
        self.assertEqual(no_period["results"][0]["status"],
                         SETTLE_QUARANTINED)

        totals = self.d._market_totals("US")
        self.assertEqual(totals["received_base"], "100.00")
        self.assertEqual(totals["payable_base"], "100.00")
        quarantined = self.d.finance_report()["quarantined_lines"]
        self.assertEqual({q["external_ref"] for q in quarantined},
                         {"edge-1", "post-1", "np-1"})
        trace = self.d.line_trace(after["results"][0]["line_id"])
        self.assertTrue(trace["quarantine"]["active"])
        self.assertEqual(trace["quarantine"]["boundary"], "2026-10-01")
        self.assertEqual(trace["shares"], [])

    # ---- 差异争议、冻结与双批准 -------------------------------------------

    def test_shortfall_above_threshold_opens_dispute_and_freezes_market(self):
        out = self._import([self._line(
            "short-1", gross="70.00", report_amount="100.00")])
        dispute_id = out["results"][0]["dispute_id"]
        self.assertIsNotNone(dispute_id)
        dispute = self.d.disputes[dispute_id]
        self.assertEqual(dispute["status"], DISPUTE_OPEN)
        self.assertEqual(str(dispute["diff_base"]), "30.00")
        totals = out["market_totals"]
        self.assertTrue(totals["frozen"])
        self.assertEqual(totals["payable_base"], "0.00")
        # 实收仍登记并可追到，只是不可支付
        self.assertEqual(totals["received_base"], "70.00")

    def test_within_threshold_does_not_open_dispute(self):
        out = self._import([self._line(
            "ok-1", gross="96.00", report_amount="100.00")])  # 差 4%
        self.assertIsNone(out["results"][0]["dispute_id"])
        self.assertFalse(out["market_totals"]["frozen"])

    def test_single_approval_does_not_release(self):
        out = self._import([self._line(
            "s1", gross="70", report_amount="100")])
        dsp = out["results"][0]["dispute_id"]
        self.d.approve_dispute(dsp, "producer", "制作方-赵")
        self.assertTrue(self.d._market_totals("US")["frozen"])
        with self.assertRaises(DomainError):
            self.d.approve_dispute(dsp, "auditor", "无关人")

    def test_dual_approval_releases_payable_balance(self):
        out = self._import([self._line(
            "s1", gross="70", report_amount="100")])
        dsp = out["results"][0]["dispute_id"]
        self.d.approve_dispute(dsp, "producer", "制作方-赵")
        self.d.approve_dispute(dsp, "rights", "小说方-钱")
        dispute = self.d.disputes[dsp]
        self.assertEqual(dispute["status"], DISPUTE_APPROVED)
        totals = self.d._market_totals("US")
        self.assertFalse(totals["frozen"])
        self.assertEqual(totals["payable_base"], "70.00")

    def test_rejection_keeps_freeze_and_can_be_overturned(self):
        out = self._import([self._line(
            "s1", gross="70", report_amount="100")])
        dsp = out["results"][0]["dispute_id"]
        self.d.approve_dispute(dsp, "rights", "小说方-钱")
        self.d.approve_dispute(dsp, "producer", "制作方-赵",
                               decision="reject", note="材料不足")
        self.assertEqual(self.d.disputes[dsp]["status"], DISPUTE_REJECTED)
        self.assertTrue(self.d._market_totals("US")["frozen"])
        # 制作方补充材料后改投批准：双批准达成，释放
        self.d.approve_dispute(dsp, "producer", "制作方-赵", note="材料补齐")
        self.assertEqual(self.d.disputes[dsp]["status"], DISPUTE_APPROVED)
        self.assertFalse(self.d._market_totals("US")["frozen"])

    def test_correction_needs_dual_approval_then_resolves_dispute(self):
        out = self._import([self._line(
            "s1", gross="70", report_amount="100")])
        dsp = out["results"][0]["dispute_id"]
        # 未双批准：可登记退款类追加，但不能调整争议
        with self.assertRaises(DomainError):
            self.d.append_correction(
                self.dep_id, "adj-1", "adjustment", "30", "USD",
                "2026-10-20", report_amount="0", dispute_id=dsp)
        self.d.approve_dispute(dsp, "producer", "制作方-赵")
        self.d.approve_dispute(dsp, "rights", "小说方-钱")
        result = self.d.append_correction(
            self.dep_id, "adj-1", "adjustment", "30", "USD",
            "2026-10-20", report_amount="0", dispute_id=dsp)
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(self.d.disputes[dsp]["status"], DISPUTE_RESOLVED)
        totals = self.d._market_totals("US")
        self.assertEqual(totals["received_base"], "100.00")
        self.assertEqual(totals["receivable_base"], "100.00")
        self.assertEqual(totals["payable_base"], "100.00")
        self.assertFalse(totals["frozen"])

    def test_insufficient_correction_reopens_dispute_and_clears_approvals(self):
        out = self._import([self._line(
            "s1", gross="70", report_amount="100")])
        dsp = out["results"][0]["dispute_id"]
        self.d.approve_dispute(dsp, "producer", "p")
        self.d.approve_dispute(dsp, "rights", "r")
        result = self.d.append_correction(
            self.dep_id, "adj-1", "adjustment", "10", "USD",
            "2026-10-20", report_amount="0", dispute_id=dsp)
        self.assertEqual(result["outcome"], "reopened")
        dispute = self.d.disputes[dsp]
        self.assertEqual(dispute["status"], DISPUTE_OPEN)
        self.assertTrue(dispute["frozen"])
        self.assertEqual(dispute["approvals"],
                         {"producer": None, "rights": None})
        self.assertEqual(self.d._market_totals("US")["payable_base"], "0.00")

    # ---- 批次原子性 -------------------------------------------------------

    def test_batch_rolls_back_completely_when_any_line_fails(self):
        with self.assertRaises(DomainError):
            self.d.import_settlement("us-pix", [
                self._line("ok-in-bad-batch", gross="50.00"),
                self._line("bad-fx", currency="EUR"),  # 缺 EUR 汇率
            ], "2026-10-15")
        # 批次整体回滚：第一行也不留账、流水可重新使用
        self.assertEqual(len(self.d.settlement_lines), 0)
        self.assertNotIn("ok-in-bad-batch", self.d.external_refs)
        out = self._import([self._line("ok-in-bad-batch", gross="50.00")])
        self.assertEqual(out["results"][0]["status"], SETTLE_IMPORTED)

    # ---- 版本报告上的财务视图 ---------------------------------------------

    def test_version_report_shows_receivable_received_payable_per_deployment(self):
        self._import([self._line("vr-1", gross="70.00",
                                 report_amount="100.00")])
        report = self.d.version_report(self.version["id"])
        finance = report["finance"][0]
        self.assertEqual(finance["deployment_id"], self.dep_id)
        self.assertEqual(finance["receivable_base"], "100.00")
        self.assertEqual(finance["received_base"], "70.00")
        self.assertTrue(finance["market_frozen"])
        self.assertEqual(finance["payable_base"], "0.00")
        self.assertTrue(finance["disputes"])

    def test_finance_report_rolls_up_markets_disputes_and_fx(self):
        self.d.set_fx_rate("EUR", "1.08", "2026-10-15")
        self._import([
            self._line("s1", gross="70", report_amount="100"),
            self._line("s2", currency="EUR", gross="50",
                       report_amount="50"),
        ])
        report = self.d.finance_report()
        us = report["markets"]["US"]
        # 应收 100 + 54 EUR 折 USD；实收 70 + 54
        self.assertEqual(us["receivable_base"], "154.00")
        self.assertEqual(us["received_base"], "124.00")
        self.assertEqual(us["currency_breakdown"],
                         {"EUR": "50.00", "USD": "70.00"})
        self.assertTrue(us["frozen"])
        self.assertEqual(len(report["disputes"]), 1)
        fx = {(r["currency"], r["as_of"]): r["rate"]
              for r in report["fx_versions"]}
        self.assertEqual(fx[("EUR", "2026-10-15")], "1.080000")


class FinanceRestartHttpTest(unittest.TestCase):
    """真实进程重启：状态文件恢复后，重复流水与账目必须逐分一致。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def _start(self):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__),
                                          "service.py"),
             "--port", str(self.port), "--state", self.state_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                status, _ = http_json(f"{self.base}/health", method="GET")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        self.fail("服务未能在时限内启动")

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def _restart(self):
        self._stop()
        self._start()

    def _action(self, payload):
        status, body = http_json(f"{self.base}/actions", payload)
        self.assertEqual(status, 200, body)
        return body

    def _setup_released_us(self):
        ip = self._action({"action": "register_ip", "title": "重启财务IP"})
        lic = self._action({"action": "register_license",
                            "ip_id": ip["id"], "licensor": "小说方"})
        self._action({"action": "add_grant", "license_id": lic["id"],
                      "market": "US", "starts": "2026-01-01",
                      "expires": "2028-12-31", "share_ratio": 0.6,
                      "currency": "USD"})
        self._action({"action": "register_team", "team_id": "t1",
                      "name": "团队", "hourly_cost": 40})
        prop = self._action({"action": "create_proposal", "ip_id": ip["id"],
                             "title": "改编", "target_markets": ["US"]})
        self._action({"action": "decide_proposal",
                      "proposal_id": prop["id"], "decision": "已采纳"})
        version = self._action({"action": "create_version",
                                "proposal_id": prop["id"], "code": "H-US",
                                "market": "US", "team_id": "t1"})
        self._action({"action": "register_asset",
                      "version_id": version["id"], "key": "actor",
                      "name": "演员", "rights_markets": ["US"],
                      "confirmed": "confirmed"})
        self._action({"action": "submit_review",
                      "version_id": version["id"], "reviewer": "顾问",
                      "result": "passed"})
        self._action({"action": "set_rating",
                      "version_id": version["id"], "rating": "PG-13"})
        self._action({"action": "register_delivery",
                      "version_id": version["id"], "team_id": "t1",
                      "content_hash": "h:us",
                      "delivered_at": DELIVERY_TS})
        self._action({"action": "register_channel", "code": "us-pix",
                      "name": "Pix", "market": "US"})
        release = self._action({"action": "request_release",
                                "version_id": version["id"],
                                "channel_codes": ["us-pix"]})
        return release["results"][0]["deployment_id"]

    def test_restart_reimport_is_idempotent_and_ledger_stays_consistent(self):
        self._start()
        dep_id = self._setup_released_us()
        self._action({"action": "set_dispute_rule", "market": "US",
                      "threshold": 0.1, "absolute": "5.00"})
        settlement = self._action({
            "action": "import_settlement", "channel_code": "us-pix",
            "settlement_date": "2026-10-15",
            "lines": [{
                "deployment_id": dep_id, "external_ref": "plat-0001",
                "gross": "70.00", "currency": "USD",
                "report_amount": "100.00",
                "period_start": "2026-09-01",
                "period_end": "2026-09-30"}]})
        line_id = settlement["results"][0]["line_id"]
        dispute_id = settlement["results"][0]["dispute_id"]
        self.assertIsNotNone(dispute_id)

        status, finance_before = http_json(
            f"{self.base}/finance", method="GET")
        self.assertEqual(status, 200)
        us_before = finance_before["markets"]["US"]
        self.assertTrue(us_before["frozen"])
        self.assertEqual(us_before["payable_base"], "0.00")

        # 逐笔追溯接口
        status, trace = http_json(
            f"{self.base}/settlement-lines/{line_id}", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(trace["path"]["version_code"], "H-US")
        self.assertEqual(trace["ledger"]["received_base"], "70.00")

        # 批次报告接口
        batch_id = settlement["batch_id"]
        status, batch = http_json(
            f"{self.base}/settlements/{batch_id}", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(batch["lines"][0]["external_ref"], "plat-0001")

        # ---- 进程重启 ----
        self._restart()

        # 同一外部流水再次到达：仍判重，不二次入账
        replay = self._action({
            "action": "import_settlement", "channel_code": "us-pix",
            "settlement_date": "2026-10-15",
            "lines": [{
                "deployment_id": dep_id, "external_ref": "plat-0001",
                "gross": "70.00", "currency": "USD",
                "report_amount": "100.00",
                "period_end": "2026-09-30"}]})
        self.assertEqual(replay["results"][0]["status"], SETTLE_DUPLICATE)

        status, finance_after = http_json(
            f"{self.base}/finance", method="GET")
        self.assertEqual(finance_after["markets"]["US"], us_before)

        # 重启后双批准才释放；单方批准仍冻结
        self._action({"action": "approve_dispute", "dispute_id": dispute_id,
                      "role": "producer", "approver": "制作方-赵"})
        status, fin = http_json(f"{self.base}/finance", method="GET")
        self.assertTrue(fin["markets"]["US"]["frozen"])
        self._action({"action": "approve_dispute", "dispute_id": dispute_id,
                      "role": "rights", "approver": "小说方-钱"})
        status, fin = http_json(f"{self.base}/finance", method="GET")
        self.assertFalse(fin["markets"]["US"]["frozen"])
        self.assertEqual(fin["markets"]["US"]["payable_base"], "70.00")

        # 再重启：批准与可支付余额持久保留
        self._restart()
        status, fin = http_json(f"{self.base}/finance", method="GET")
        self.assertFalse(fin["markets"]["US"]["frozen"])
        self.assertEqual(fin["markets"]["US"]["payable_base"], "70.00")
        self.assertEqual(fin["markets"]["US"]["receivable_base"], "100.00")
        self.assertEqual(fin["markets"]["US"]["received_base"], "70.00")


if __name__ == "__main__":
    unittest.main()
