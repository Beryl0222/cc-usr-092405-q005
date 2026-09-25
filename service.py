"""跨文化内容共制服务入口。

在 /health 之外提供领域接口：

* POST /actions —— 统一动作入口，body 形如 {"action": "...", ...}，
  返回新建或更新后的业务记录；
* GET  /dashboard —— 管理看板：分市场成片、未决阻塞、收益依据与撤权波及；
* GET  /versions/<id> —— 单个版本的完整版本关系与发行核验报告。

所有业务规则在 domain.Domain 内执行；本模块只负责 HTTP 编解码。
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from domain import Domain, DomainError, NotFoundError

SERVICE_ID = "cultural-coproduction"
SERVICE_NAME = "跨文化内容共制"

# 动作 -> (领域方法, 允许透传的字段)；业务默认值与校验在领域层。
ACTION_FIELDS = {
    "register_ip": ("register_ip", ["title", "origin", "note"]),
    "add_cultural_element": (
        "add_cultural_element",
        ["ip_id", "key", "name", "stance", "restricted_markets", "note", "reviewer"]),
    "set_element_stance": (
        "set_element_stance",
        ["ip_id", "key", "stance", "restricted_markets", "note", "reviewer"]),
    "create_proposal": (
        "create_proposal",
        ["ip_id", "title", "summary", "creator", "target_markets"]),
    "decide_proposal": (
        "decide_proposal", ["proposal_id", "decision", "note", "reviewer"]),
    "create_version": (
        "create_version",
        ["proposal_id", "code", "market", "team_id", "parent_id", "note"]),
    "use_element": ("use_element", ["version_id", "element_key"]),
    "remove_element": ("remove_element", ["version_id", "element_key"]),
    "register_license": (
        "register_license",
        ["ip_id", "licensor", "scope", "note", "coverage"]),
    "add_grant": (
        "add_grant",
        ["license_id", "market", "starts", "expires", "share_ratio",
         "currency", "note"]),
    "withdraw_grant": ("withdraw_grant", ["grant_id", "reason", "at"]),
    "register_asset": (
        "register_asset",
        ["version_id", "key", "name", "rights_markets", "confirmed",
         "licensor", "kind"]),
    "decide_asset": (
        "decide_asset", ["version_id", "key", "decision", "note"]),
    "remove_asset": (
        "remove_asset", ["version_id", "key", "reason"]),
    "set_asset_included": (
        "set_asset_included",
        ["version_id", "key", "included", "reason"]),
    "register_team": (
        "register_team",
        ["team_id", "name", "region", "timezone", "hourly_cost"]),
    "assign_assets": (
        "assign_assets", ["version_id", "team_id", "asset_keys"]),
    "log_hours": (
        "log_hours",
        ["version_id", "team_id", "hours", "activity", "worked_on"]),
    "register_delivery": (
        "register_delivery",
        ["version_id", "team_id", "content_hash", "delivered_at", "note"]),
    "submit_review": (
        "submit_review", ["version_id", "reviewer", "result", "note"]),
    "set_rating": (
        "set_rating", ["version_id", "rating", "body", "note"]),
    "register_channel": (
        "register_channel", ["code", "name", "market"]),
    "request_release": (
        "request_release", ["version_id", "channel_codes", "at"]),
    # ---- 财务结算 ----
    "set_base_currency": ("set_base_currency", ["currency"]),
    "set_fx_rate": (
        "set_fx_rate", ["currency", "rate", "effective_date"]),
    "set_variance_rule": (
        "set_variance_rule", ["threshold", "market"]),
    "import_settlement": (
        "import_settlement",
        ["external_txn_id", "entry_type", "deployment_id", "version_id",
         "channel_code", "platform", "market", "currency", "gross_amount",
         "received_amount", "period_start", "period_end", "settlement_date",
         "corrects_txn_id", "dispute_id", "note"]),
    "approve_dispute": (
        "approve_dispute", ["dispute_id", "party", "approver", "note"]),
    "resolve_dispute": (
        "resolve_dispute", ["dispute_id", "resolution", "correction"]),
}


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def save_state(domain, path):
    """把领域状态落盘；重启后再次导入同一外部流水仍只入账一次。"""
    import os
    import tempfile
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(domain.snapshot(), handle, ensure_ascii=False)
    os.replace(tmp, path)


def load_state(path):
    """从快照恢复领域状态；文件不存在或损坏时返回 None（全新启动）。"""
    import os
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return Domain.restore(json.load(handle))
    except (ValueError, OSError, KeyError):
        return None


def dispatch(domain, payload):
    """执行一个领域动作，返回可序列化结果。"""
    import inspect

    action = payload.get("action")
    if not action:
        raise DomainError("请求缺少 action 字段", code="MissingAction")
    if action not in ACTION_FIELDS:
        raise NotFoundError(f"未知动作: {action}", code="UnknownAction")
    method_name, fields = ACTION_FIELDS[action]
    method = getattr(domain, method_name)
    kwargs = {field: payload[field] for field in fields if field in payload}
    try:
        inspect.signature(method).bind(**kwargs)
    except TypeError as error:
        # 缺少必填参数：归入 400 而非 500（参数类型错误由 JSON 校验或领域报错处理）
        raise DomainError(str(error), code="InvalidArguments")
    return method(**kwargs)


class Handler(BaseHTTPRequestHandler):
    """提供健康检查、领域动作与只读看板。"""

    domain = Domain()
    state_path = None       # 设置后每个成功动作都会落盘快照，重启可恢复

    def _write_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, health_payload())
        elif self.path == "/dashboard":
            self._write_json(200, self.domain.dashboard())
        elif self.path.startswith("/versions/"):
            version_id = self.path[len("/versions/"):]
            try:
                self._write_json(200, self.domain.version_report(version_id))
            except DomainError as error:
                self._write_json(error.http_status, error.to_dict())
        elif self.path.startswith("/settlements/"):
            txn_id = self.path[len("/settlements/"):]
            try:
                self._write_json(200, self.domain.settlement_report(txn_id))
            except DomainError as error:
                self._write_json(error.http_status, error.to_dict())
        elif self.path.startswith("/ledger/market/"):
            market = self.path[len("/ledger/market/"):]
            self._write_json(200, self.domain.market_ledger(market))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/actions":
            self.send_error(404)
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._write_json(400, {"error": "BadJson",
                                   "message": "请求体必须是 JSON 对象"})
            return
        try:
            result = dispatch(self.domain, payload)
        except DomainError as error:
            self._write_json(error.http_status, error.to_dict())
            return
        self._write_json(200, result if result is not None else {"ok": True})
        if self.state_path:
            save_state(self.domain, self.state_path)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true",
                        help="不启动服务，执行基础自检")
    parser.add_argument("--scenario", action="store_true",
                        help="运行首部作品多地上线样例并输出看板 JSON")
    parser.add_argument("--state", default=None,
                        help="状态快照文件路径：每个动作落盘，重启自动恢复")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        probe = Domain().register_ip("自检IP")
        assert probe["id"].startswith("ip_")
        print("基础检查通过")
        return
    if args.scenario:
        import scenario
        print(json.dumps(scenario.run(), ensure_ascii=False, indent=2))
        return
    if args.state:
        restored = load_state(args.state)
        if restored is not None:
            Handler.domain = restored
        Handler.state_path = args.state
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
