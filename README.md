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

## 运行

```bash
python3 service.py --check       # 基础自检
python3 service.py --scenario    # 首部作品《丝路长风·破晓》六市场端到端样例（JSON）
python3 service.py --port 8000   # 启动 HTTP 服务
npm test                         # 全部契约测试（30 项）
python3 -m compileall -q .       # 编译检查全部 Python 模块
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份健康检查 |
| POST | `/actions` | 统一业务动作入口，body 为 `{"action": "...", ...}` |
| GET | `/versions/<id>` | 版本完整报告：版本关系、三关核验、工时、收益依据、各渠道副本 |
| GET | `/dashboard` | 管理看板：分市场成片索引、未决阻塞、收益分配依据、撤权波及的每个副本 |

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

### 快速示例

```bash
curl -s localhost:8000/actions -H 'Content-Type: application/json' \
  -d '{"action":"register_ip","title":"丝路长风"}'
curl -s localhost:8000/dashboard
```

## 文件

- `domain.py` — 领域核心：实体、版本关系与全部业务规则（无框架、无持久化依赖）
- `service.py` — HTTP 入口与动作编解码，规则全部委托给领域层
- `scenario.py` — 首部作品六市场权利矩阵端到端样例（含撤权前/后两种波及）
- `fixtures/domain.json` — 统一领域称谓与状态词表
- `*_contract.py` — 契约测试（健康入口、领域规则、HTTP API）
