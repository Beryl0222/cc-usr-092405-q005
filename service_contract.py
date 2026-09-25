"""验证基础服务在领域模块开发前保持可运行。"""

import json
import os
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from domain import Domain
from service import (
    Handler, SERVICE_ID, SERVICE_NAME, health_payload, load_state, save_state,
)


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_state_roundtrip_keeps_settlement_idempotency(self):
        domain = Domain(now=lambda: "2026-09-20")
        ip = domain.register_ip("持久化IP")
        lic = domain.register_license(ip["id"], "版权方")
        domain.add_grant(lic["id"], "CN", "2026-01-01", "2028-12-31",
                         share_ratio=0.5)
        domain.register_team("t1", "团队", hourly_cost=10)
        prop = domain.create_proposal(ip["id"], "改编", target_markets=["CN"])
        domain.decide_proposal(prop["id"], "已采纳")
        version = domain.create_version(prop["id"], "P-CN", "CN", team_id="t1")
        domain.register_asset(version["id"], "actor", "演员",
                              rights_markets=["CN"], confirmed="confirmed")
        domain.submit_review(version["id"], "顾问", "passed")
        domain.set_rating(version["id"], "12+")
        domain.register_delivery(version["id"], "t1", "h:p",
                                 "2026-09-18T10:00:00+08:00")
        domain.register_channel("p-ch", "渠道", "CN")
        dep_id = domain.request_release(
            version["id"], ["p-ch"])["results"][0]["deployment_id"]
        line = domain.import_settlement(
            external_txn_id="PERSIST-1", deployment_id=dep_id,
            currency="USD", gross_amount=100, received_amount=100,
            period_start="2026-09-01", period_end="2026-09-10",
            settlement_date="2026-09-20")

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            save_state(domain, path)
            restored = load_state(path)
        self.assertIsNotNone(restored)
        again = restored.import_settlement(
            external_txn_id="PERSIST-1", deployment_id=dep_id,
            currency="USD", gross_amount=100, received_amount=100,
            period_start="2026-09-01", period_end="2026-09-10",
            settlement_date="2026-09-20")
        self.assertEqual(again["id"], line["id"])
        self.assertEqual(len(restored.settlement_lines), 1)
        self.assertEqual(restored.market_balance("CN"),
                         domain.market_balance("CN"))

    def test_load_state_missing_file_returns_none(self):
        self.assertIsNone(load_state("/nonexistent/state.json"))
        self.assertIsNone(load_state(None))


if __name__ == "__main__":
    unittest.main()
