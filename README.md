# 跨文化内容共制

协调丝路题材游戏/短剧出海时的**内容权利、本地化文化复核、境外制作与分市场发行**，
把原始 IP、文化元素、改编提案、地区权利、译审意见、素材确认、制片交付连成版本关系。

## 它强制的业务规则

- **文化边界**：文化顾问可把元素标为受限（指定市场清单）或禁用（全部市场）；
  受限元素进入受限市场版本、禁用元素进入任何版本都会被拦截；口径收紧即时生效。
- **按市场+期限授权**：版权方授权绑定市场与授权窗口；窗口外或撤回即失效。
  授权分 `ip` 级（该 IP 每版必备）与 `asset` 级（如地区音乐，仅当成片使用该授权方素材时才要求）。
- **按地区发料**：境外团队只能领取"已确认且权利覆盖本版本市场且进入成片"的素材，
  未确认演员/音乐、权利不覆盖地区的素材一律扣留并说明原因；撤权同时收回团队访问权。
- **发行三关同时核验**：权利（全部必需授权方在窗口内）、文化复核（译审通过且无违规元素）、
  当地分级；任一不过则逐渠道留下带原因的阻断记录。
- **返工不改账**：译审驳回后切新版本（`parent_id` 指向母版）；工时一经核算即锁定
  （单价随工时冻结），返工另计新版本工时，无法改写历史。
- **跨时区交付幂等**：以 `(版本, 内容哈希)` 归一化到 UTC 去重，重复提交只入账一次。
- **撤权精确波及**：未发布版本被阻断并列出受影响渠道；已上线副本下架；
  团队领料访问收回；上线时冻结的收益分成与工时快照保留不变。
- **迟到结算按结算日取版本化汇率**：平台结算晚于发行、同一版本按市场/渠道/币种
  拆多笔都能逐笔入账；汇率按结算日登记，一经结算行引用即冻结不可改写，差异只能走更正。
- **结算行锚定冻结分成**：每行关联已发行副本及其上线瞬间锁定的分成规则；
  应收（平台报表口径）与实收分别累计，退款与更正只能以新流水追加。
- **外部流水幂等**：以 `external_ref` 去重，平台重推/重启后再次导入只入账一次。
- **撤回边界隔离**：授权撤回后的新增收入（按收入周期末日判定）单独隔离，
  不参与分成分配、不进可支付余额，可逐笔追溯到撤回边界与授权。
- **差异争议双方批准**：实收与应收差异超过市场阈值（比例或绝对额）自动建争议，
  冻结该市场可分配余额；制作方与权利方分别批准后才可释放，双批准后的更正
  消除差异即闭环（仍超阈值则争议重开、批准清零）。
- **逐笔可追溯且重启账目一致**：从外部流水可追到发行副本、冻结快照、
  应收/实收/可支付余额；`--state` 状态文件原子落盘，重启后再导入逐分一致。

## 运行

```bash
python3 service.py --check       # 基础自检
python3 service.py --scenario    # 首部作品《丝路长风·破晓》六市场端到端样例（JSON，含结算对账）
python3 service.py --port 8000   # 启动 HTTP 服务
python3 service.py --port 8000 --state data/state.json  # 带持久化：动作后原子落盘，重启恢复
npm test                         # 全部契约测试（52 项）
python3 -m compileall -q .       # 编译检查全部 Python 模块
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份健康检查 |
| POST | `/actions` | 统一业务动作入口，body 为 `{"action": "...", ...}` |
| GET | `/versions/<id>` | 版本完整报告：版本关系、三关核验、工时、收益依据、各渠道副本 |
| GET | `/dashboard` | 管理看板：分市场成片索引、未决阻塞、收益分配依据、撤权波及的每个副本 |
| GET | `/finance[?market=US]` | 财务总账：分市场应收/实收/可支付、争议、隔离行、重复流水与汇率版本 |
| GET | `/settlements/<batch_id>` | 结算批次报告：批次头与逐行明细 |
| GET | `/settlement-lines/<line_id>` | 逐笔追溯：流水→副本→冻结分成→应收/实收/可支付台账 |

业务错误返回 400（规则冲突/参数问题）或 404（对象或动作不存在），
统一形如 `{"error": "...", "message": "...", "details": {}}`。

### 动作一览（POST /actions）

| action | 关键字段 | 用途 |
| --- | --- | --- |
| `register_ip` | `title` | 登记原始 IP |
| `add_cultural_element` / `set_element_stance` | `ip_id,key,stance(restricted/forbidden/allowed),restricted_markets` | 文化顾问限定/收紧元素 |
| `create_proposal` / `decide_proposal` | `target_markets`、`decision=已采纳/已驳回` | 改编提案与决议 |
| `create_version` | `proposal_id,market,team_id,parent_id` | 切分市场成片版本；`parent_id` 表示返工 |
| `use_element` / `remove_element` | `version_id,element_key` | 版本选用文化元素（禁用元素被拒） |
| `register_license` | `ip_id,licensor,coverage=ip/asset` | 授权合同；素材级授权用于地区音乐等 |
| `add_grant` | `license_id,market,starts,expires,share_ratio` | 按市场和期限授权，重叠窗口被拒 |
| `withdraw_grant` | `grant_id,reason,at` | 撤权：阻断/下架/收回领料，返回波及清单 |
| `register_asset` / `decide_asset` | `version_id,key,rights_markets,confirmed` | 演员/音乐登记与确认 |
| `set_asset_included` | `included=false,reason` | 声明素材不进入某市场成片（如本地配乐替换） |
| `register_team` / `assign_assets` | `team_id` 及地区/时薪；`version_id,asset_keys` | 团队登记与按地区发料 |
| `log_hours` | `version_id,team_id,hours,activity` | 工时入账（即锁定，单价冻结） |
| `register_delivery` | `content_hash,delivered_at(带时区)` | 成片交付；跨时区同哈希只入账一次 |
| `submit_review` / `set_rating` | `result=passed/failed`、`rating` | 译审文化复核与当地分级 |
| `register_channel` / `request_release` | `code,market`；`channel_codes` | 渠道登记与发行三关核验 |
| `set_fx_rate` | `currency,rate,as_of` | 登记结算日汇率口径；已被结算行引用不可改写 |
| `set_dispute_rule` | `market,threshold,absolute` | 市场差异阈值（比例和/或本位币绝对额） |
| `import_settlement` | `channel_code,lines,settlement_date` | 导入结算批次：逐行 `deployment_id,external_ref,kind,gross,currency,report_amount,period_start,period_end`；重复流水判重、撤回后隔离、超阈自动建争议 |
| `approve_dispute` | `dispute_id,role(producer/rights),approver,decision(approve/reject)` | 制作方与权利方分别表态，双批准释放冻结 |
| `append_correction` | `deployment_id,external_ref,kind(refund/adjustment),gross,currency,settlement_date,dispute_id` | 退款/更正只追加；带争议须先双批准，差异消除则闭环 |

### 结算行字段与口径

- `kind`：`sale`（销售，金额为正）/ `refund`（退款，一律按负向）/
  `adjustment`（更正，金额可正可负）；退款与更正只能以**新 `external_ref` 追加**。
- `gross` 为实收（平台实际结算原币金额），`report_amount` 为应收（平台报表口径原币）；
  两者都按 `settlement_date` 的汇率折本位币（USD）。退款行的 `report_amount` 通常为 0。
- 跨币种与分成比例产生的舍入尾差全部由制作方吸收，逐行各方金额之和与实收精确到分配平。
- 撤回隔离按**收入周期末日**判定：`period_end >= 授权撤回日`（或无周期时结算日已撤回）
  即进入隔离区；撤回之前周期的迟到结算（哪怕 11 月才到达）仍正常入账。

### 快速示例

```bash
curl -s localhost:8000/actions -H 'Content-Type: application/json' \
  -d '{"action":"register_ip","title":"丝路长风"}'
curl -s localhost:8000/dashboard
```

## 文件

- `domain.py` — 领域核心：实体、版本关系与全部业务规则（无框架、无持久化依赖）
- `service.py` — HTTP 入口与动作编解码，规则全部委托给领域层
- `scenario.py` — 首部作品六市场权利矩阵端到端样例（含撤权前/后两种波及与迟到结算对账）
- `fixtures/domain.json` — 统一领域称谓与状态词表
- `*_contract.py` — 契约测试（健康入口、领域规则、HTTP API、结算对账与真实进程重启）
