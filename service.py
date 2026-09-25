"""跨文化内容共制服务入口。

在 /health 之外提供领域接口：

* POST /actions —— 统一动作入口，body 形如 {"action": "...", ...}，
  返回新建或更新后的业务记录；
* GET  /dashboard —— 管理看板：分市场成片、未决阻塞、收益依据与撤权波及；
* GET  /versions/<id> —— 单个版本的完整版本关系与发行核验报告；
* GET  /finance[?market=] —— 应收/实收/可支付总账、争议、隔离与汇率版本；
* GET  /settlements/<id> 与 /settlement-lines/<id> —— 批次报告与逐笔追溯。

带 --state 时，每个成功动作原子落盘，重启后恢复全部账目。

所有业务规则在 domain.Domain 内执行；本模块只负责 HTTP 编解码。
"""

import argparse
import json
import os
import tempfile
from decimal import Decimal
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
    # ---- 结算与对账 ----
    "set_fx_rate": (
        "set_fx_rate", ["currency", "rate", "as_of", "base_currency"]),
    "set_dispute_rule": (
        "set_dispute_rule", ["market", "threshold", "absolute"]),
    "import_settlement": (
        "import_settlement",
        ["channel_code", "lines", "settlement_date", "batch_ref", "at"]),
    "approve_dispute": (
        "approve_dispute",
        ["dispute_id", "role", "approver", "decision", "note", "at"]),
    "append_correction": (
        "append_correction",
        ["deployment_id", "external_ref", "kind", "gross", "currency",
         "settlement_date", "report_amount", "period_start", "period_end",
         "note", "dispute_id", "at"]),
}


class DecimalEncoder(json.JSONEncoder):
    """金额等 Decimal 在 HTTP 层以字符串输出，避免浮点失真。"""

    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


def dumps(payload):
    return json.dumps(payload, ensure_ascii=False, cls=DecimalEncoder)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


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
    state_path = None   # 配置后，每个成功动作原子落盘；重启时恢复

    def _write_json(self, status, data):
        body = dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _persist(self):
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.domain.snapshot(), handle,
                          ensure_ascii=False, cls=DecimalEncoder)
            os.replace(tmp, self.state_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @classmethod
    def load_state(cls, path):
        cls.state_path = path
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                cls.domain = Domain().load_state(json.load(handle))
        return cls.domain

    def do_GET(self):
        if self.path == "/health":
            self._write_json(200, health_payload())
        elif self.path == "/dashboard":
            self._write_json(200, self.domain.dashboard())
        elif self.path == "/finance" or self.path.startswith("/finance?"):
            market = None
            if "?" in self.path:
                from urllib.parse import parse_qs
                market = parse_qs(self.path.split("?", 1)[1]).get("market", [None])[0]
            self._write_json(200, self.domain.finance_report(market=market))
        elif self.path.startswith("/settlements/"):
            settlement_id = self.path[len("/settlements/"):]
            try:
                self._write_json(200, self.domain.settlement_report(settlement_id))
            except DomainError as error:
                self._write_json(error.http_status, error.to_dict())
        elif self.path.startswith("/settlement-lines/"):
            line_id = self.path[len("/settlement-lines/"):]
            try:
                self._write_json(200, self.domain.line_trace(line_id))
            except DomainError as error:
                self._write_json(error.http_status, error.to_dict())
        elif self.path.startswith("/versions/"):
            version_id = self.path[len("/versions/"):]
            try:
                self._write_json(200, self.domain.version_report(version_id))
            except DomainError as error:
                self._write_json(error.http_status, error.to_dict())
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
            self._persist()
        except DomainError as error:
            self._write_json(error.http_status, error.to_dict())
            return
        self._write_json(200, result if result is not None else {"ok": True})

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--state", default="",
                        help="状态文件路径：启动时恢复，每个成功动作后原子落盘")
    parser.add_argument("--check", action="store_true",
                        help="不启动服务，执行基础自检")
    parser.add_argument("--scenario", action="store_true",
                        help="运行首部作品多地上线样例并输出看板 JSON")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        probe = Domain().register_ip("自检IP")
        assert probe["id"].startswith("ip_")
        # 财务快照往返自检：重启后外部流水去重与账目保持一致
        finance = Domain()
        snap = finance.snapshot()
        assert Domain().load_state(snap).snapshot() is not None
        print("基础检查通过")
        return
    if args.scenario:
        import scenario
        print(dumps(scenario.run()))
        return
    if args.state:
        Handler.load_state(args.state)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
