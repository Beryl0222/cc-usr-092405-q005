"""HTTP 接口契约：动作入口、错误映射、只读报告与未知路由。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler


def http_json(method, url, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, method=method,
                      headers={"Content-Type": "application/json"})
    return urlopen(request, timeout=3)


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 每个测试类使用干净的领域存储
        Handler.domain = type(Handler.domain)()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _action(self, payload):
        with http_json("POST", f"{self.base_url}/actions", payload) as response:
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return json.load(response)

    def _action_expect_error(self, payload, status):
        with self.assertRaises(HTTPError) as error:
            self._action(payload)
        self.assertEqual(error.exception.code, status)
        body = json.load(error.exception)
        error.exception.close()
        return body

    def test_full_workflow_through_http(self):
        ip = self._action({"action": "register_ip", "title": "HTTP联调IP"})
        self._action({"action": "add_cultural_element", "ip_id": ip["id"],
                      "key": "mural", "name": "壁画"})
        self._action({"action": "register_team", "team_id": "t1",
                      "name": "团队", "hourly_cost": 20})
        lic = self._action({"action": "register_license",
                            "ip_id": ip["id"], "licensor": "版权方"})
        self._action({"action": "add_grant", "license_id": lic["id"],
                      "market": "CN", "starts": "2026-01-01",
                      "expires": "2028-12-31", "share_ratio": 0.5})
        prop = self._action({"action": "create_proposal", "ip_id": ip["id"],
                             "title": "改编", "target_markets": ["CN"]})
        self._action({"action": "decide_proposal", "proposal_id": prop["id"],
                      "decision": "已采纳"})
        version = self._action({"action": "create_version",
                                "proposal_id": prop["id"], "code": "H-CN",
                                "market": "CN", "team_id": "t1"})
        self._action({"action": "register_asset", "version_id": version["id"],
                      "key": "actor", "name": "演员",
                      "rights_markets": ["CN"], "confirmed": "confirmed"})
        self._action({"action": "submit_review", "version_id": version["id"],
                      "reviewer": "顾问", "result": "passed"})
        self._action({"action": "set_rating", "version_id": version["id"],
                      "rating": "12+"})
        self._action({"action": "register_channel", "code": "cn",
                      "name": "渠道", "market": "CN"})
        blocked = self._action({"action": "request_release",
                                "version_id": version["id"],
                                "channel_codes": ["cn"]})
        self.assertEqual(blocked["results"][0]["status"], "blocked")

        delivery = self._action({"action": "register_delivery",
                                 "version_id": version["id"], "team_id": "t1",
                                 "content_hash": "h:1",
                                 "delivered_at": "2026-09-18T10:00:00+08:00"})
        self.assertEqual(delivery["status"], "accepted")
        released = self._action({"action": "request_release",
                                 "version_id": version["id"],
                                 "channel_codes": ["cn"]})
        self.assertEqual(released["results"][0]["status"], "online")

        with http_json("GET", f"{self.base_url}/versions/{version['id']}") as r:
            report = json.load(r)
        self.assertEqual(report["status"], "已发行")
        self.assertEqual(report["revenue_basis"]["grants"][0]["licensor"], "版权方")

        with http_json("GET", f"{self.base_url}/dashboard") as r:
            board = json.load(r)
        self.assertIn("CN", board["market_index"])

    def test_error_mapping_and_unknown_routes(self):
        body = self._action_expect_error(
            {"action": "create_version", "proposal_id": "prop_x",
             "code": "X", "market": "CN"}, 404)
        self.assertIn("error", body)

        body400 = self._action_expect_error({"action": "register_team",
                                             "team_id": "t2"}, 400)
        self.assertIn("error", body400)

        self._action_expect_error({"action": "no_such_action"}, 404)
        self._action_expect_error({}, 400)

        with self.assertRaises(HTTPError) as error:
            http_json("POST", f"{self.base_url}/unknown", {})
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            req = Request(f"{self.base_url}/actions",
                          data=b"{not-json", method="POST",
                          headers={"Content-Type": "application/json"})
            urlopen(req, timeout=3)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
