# 次日计划与板块异动：API 与 Web 入口

本文说明「板块异动 / 机会选股 / 我的股票 / 助手 / 设置」这组新页面的 API 契约和 Web 行为。功能语义以[核心功能说明](design/core-product-spec.md)为准，交互以[设计稿](design/core-redesign.html)为准。

## 当前状态

- API 层（`api/v1/endpoints/boards.py`、`api/v1/endpoints/daily_plan.py`）和 Web 页面已实现。
- 板块异动服务 `src/services/board_anomaly_service.py`（`BoardAnomalyService`）已实现，读取本地的板块指标和判定结果（见[核心流程后端设计](design/core-backend-design.md)第 2 节），数据由调度器每个交易日 17:00 运行的板块日度流程（`BOARD_PIPELINE_*`，也可用 `scripts/run_board_pipeline.py` 手动运行）产生；没有任何已完成的运行时 `/boards/anomalies` 返回空列表和说明，不返回 503。
- 次日计划服务 `src/services/daily_plan_service.py`（`DailyPlanService`）已实现，数据流和规则见下面的「次日计划服务」一节。服务模块无法导入时 `/plan/*` 仍返回 `503 {"error": "daily_plan_unavailable", "message": "次日计划服务未就绪"}`，Web 页面显示「次日计划服务未就绪」说明，不影响其他页面。
- API 在每次请求时按需导入服务模块，服务类支持无参构造。

## 次日计划服务

模块分工：`daily_plan_service.py`（账户、读取与按需生成、运行编排）、`daily_plan_engine.py`（单只股票的价位、模型判断、条件计划、换股判断，纯计算）、`daily_plan_sizing.py`（仓位规则、涨跌停与申报单位、股数计算）、`daily_plan_push.py`（飞书摘要）、`daily_plan_schedule.py`（定时任务）、`src/repositories/daily_plan_repo.py`（存储与本地行情读取）。

**数据来源**：只读本地数据，不请求任何数据商。交易日 = `market_stock_daily` 里最新的一天（17:00 板块流程入库当日数据，之后才有当天收盘；运行时本地最新日期早于应有的交易日会写进运行的 `data_gaps`），下一交易日按 A 股交易日历推算（日历不可用时取下一个工作日）。个股行情取最近 60 个交易日的 `market_stock_daily`；`stock_daily`（每日分析抓过的股票才有）里的最高 / 最低价用来估算支撑和压力，没有时用收盘价近似并写进 `data_gaps`。所属板块和今天的异动判定来自最近一次完成的板块运行（`board_membership` + `board_signal`）。

**账户**：现金与持仓快照存在 SQLite（`plan_account`、`plan_holding`），`PUT /plan/holdings` 整体覆盖（代码不能重复，最多 50 只，校验失败返回 400）。持仓行补上名称、板块（主板 / 创业板 / 科创板 / 北交所）、ST、最新收盘与涨跌幅、市值、盈亏；没有行情的持仓按成本计市值。`sellable`（T+1）：买入日期不早于本地最新交易日则为 `false`（今天买入，今天不能卖），没填买入日期时为 `true` 并在 `sellable_note` 提示确认。总资金 = 现金 + 持仓市值。

**自选与持仓范围**：持仓在前，其后是自选（`STOCK_LIST`）。只处理 6 位 A 股代码，指数、港股、美股等其他条目跳过，并在运行的 `data_gaps` 里列出。

**仓位规则**（`POSITION_*`，不写死任何数值，配置缺失或非法时回到默认值）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `POSITION_STOP_LOSS_PCT` | `10` | 止损幅度（%），从买入价（持仓则为成本）起算 |
| `POSITION_MAX_TOTAL_PCT` | `100` | 总仓位上限（%，持仓市值占总资金） |
| `POSITION_MAX_HOLDINGS` | `1` | 最多持股数；达到后新股票不再给「买入」，改为换股判断 |
| `POSITION_MAX_SINGLE_PCT` | 空 | 单只上限（%）；留空 = 总仓位上限 ÷ 最多持股数（`max_single_pct_derived=true`） |
| `POSITION_STOP_MODE` | `fixed` | `fixed` 固定比例；`nearer` 固定比例与技术支撑取较近者（支撑低于买入价时取较高的那个） |

**股数怎么算**（模型只判断买不买、在什么价位买，股数始终由规则算出，`steps` 列出每一步）：按单只上限 = 总资金 × 单只上限 ÷ 买入价 − 已持股数；按总仓位 = (总资金 × 总仓位上限 − 持仓市值) ÷ 买入价；按现金 = 现金 ÷ 买入价；取三者最小值（不小于 0），再按申报单位向下取整。主板、创业板、北交所每手 100 股；科创板最少 200 股，达到后按 1 股递增。不足一手为 `below_lot`（不硬凑），未持有该股且持股数已满为 `max_holdings`，没有买入价为 `no_price`。止损价 = 买入价 × (1 − 止损幅度)，同时给出到止损的最大亏损、占总资金比例、买后总仓位，以及止损日再跌停的成交价和亏损。示例：总资金 10 万、空仓、47.00 元买入 → 2,100 股、98,700 元、占 98.7%、止损 42.30、亏损 9,870 元。涨跌停幅度：主板 ±10%、创业板与科创板 ±20%、北交所 ±30%、主板 ST ±5%（创业板 / 科创板 ST 沿用本板块的 20%，不按 5% 计算）；价格四舍五入到 0.01。

**每只股票的计划**（`StockPlan`）：

- 模型（`LITELLM_MODEL`，`LLM_REASONING_EFFORT`，`call_type=daily_plan`，每只股票一次调用，短线和波段在同一次回复里）决定结论（未持仓：买入 / 等待 / 放弃；已持仓：持有 / 加仓 / 减仓 / 清仓）和价位（买入价与区间、突破价、跌破放弃价、止盈价、当日情况、风险）。模型给出的价格必须落在下一交易日涨跌停范围内且顺序合理，否则该项沿用规则值。
- 规则始终参与并优先于模型：收盘跌破止损价 → 清仓；最多持股数已满 → 新股票的「买入」降为「等待」；仓位规则下不足一手 → 放弃；明天因 T+1 不能卖出 → 卖出类结论改为持有并提示；停牌 → 结论 `suspended`，没有交易计划，只列复牌后要关注的事项。条件计划的文字、`outside_limit`（价位超出涨跌停范围）、股数和止损都由规则生成。
- 模型没配置、调用失败或回复不可用时，生成规则计划：均线 / 区间高低点 / 平均波幅得到支撑、压力和止盈，趋势向上且贴近支撑为「买入」、离支撑较远为「等待」、趋势向下为「放弃」；`model` 为 `null`，并在 `data_gaps` 写明「这份计划按规则生成」及原因。计划始终能生成。
- 两个周期（短线看 10 个交易日窗口，波段看 30 个交易日窗口）各有价位、结论和股数；`default_horizon` 为持仓的计划周期，未填为 `short`。风险包含止损与涨跌停的提示（主板一个跌停约 10%、创业板科创板可能跳空越过止损）、按止损价亏损与跌停亏损两个数字、涨跌停收盘、T+1。

**换股判断**（`switch`，只在持股数已满时给持仓股）：候选来自自选里「买入 / 等待」的股票和最近一次完成的选股任务的前 3 只（不在自选里的用规则计划评估，不额外调用模型）。两个条件同时满足才换：当前持仓破位（收盘跌破止损价或被判减仓 / 清仓）、候选收盘已在买入区内（盘中企稳要明天才能确认，所以多数情况 `met` 为 `null`）；默认结论是不换（`keep`）。`costs` 列出 T+1、两边止损与卖出盈亏、换入后的最大亏损、最多持股数限制。

**存储与读取**：每次运行把每只股票的计划存进 `plan_stock`（含失败的）；`GET /plan/stocks`、`GET /plan/stocks/{code}` 读取该交易日最新一份存档，所以打开页面不调用模型。没有可用存档的股票（刚加入自选，或持仓变动后存档过期）：列表里显示 `status=missing`；打开详情时现算一份（一次模型调用）并存档，下次直接读取。已生成的计划不会随仓位规则的修改而改写，修改后下一次运行才会按新规则重算（历史存档按生成时的规则展示）。不在自选也不在持仓里且没有存档的代码返回 404。

**运行**（`POST /plan/runs`、定时任务）：后台线程依次执行 板块（读取最近一次完成的板块运行）→ 选股（读取最近一次完成的选股任务，不会发起新的选股）→ 个股计划（`MAX_WORKERS` 个工作线程，一只失败不影响其他）→ 换股判断 → 飞书推送，每一步的状态和用时写进 `steps`，`plan_run` 保存运行记录、失败项、推送状态、推送预览和模型调用次数。同一进程同一时间只有一个运行，运行中再触发返回 409；进程重启后遗留的 `running` 记录在下次读取时标为 `failed`。运行状态：全部成功 `succeeded`；有失败股票或板块 / 选股 / 换股步骤降级为 `partial`；没有一只成功为 `failed`；没有自选和持仓为 `skipped`。推送失败只记在 `push` 和 `push` 步骤上，不改变计划与运行状态。

**飞书推送**：只发飞书（复用现有发送逻辑：应用机器人绑定会话 / `FEISHU_CHAT_ID`，或群 Webhook，签名、关键词、超长分段不变）。内容：持仓结论（第一屏）→ 换股判断 → 自选股一行一只（买入在前）→ 板块异动前几名 → 数据提醒（缺失数据、失败股票）→ 网页查看提示。未配置飞书时 `push.status=skipped`、`push.configured=false`，预览照常生成。

**定时**：`DAILY_PLAN_ENABLED=true`（默认 `false`）且开启定时模式（`SCHEDULE_ENABLED=true` 或 `--schedule`）时，调度器每个 A 股交易日（交易所日历）在 `SCHEDULE_TIME`（设置了 `SCHEDULE_TIMES` 时在每个时间点各一次）运行一次。`SCHEDULE_TIME` 同时是每日逐只分析的运行时间，两者是**两个独立的任务**：次日计划放在独立线程里运行，不替换、不阻塞同一时刻的每日分析（需要的话可以在设置里关掉其中一个，此处不改动每日分析的任何行为）；上一次次日计划没跑完时下一次跳过，失败只记日志和运行记录。注意 `SCHEDULE_TIME` 代码默认值是 `18:00`，产品设计的 16:00 需要在设置页「运行和推送」里设置；计划基于本地已入库的最新交易日，而当天数据要等 17:00 板块流程入库后才有，想用当天数据应把时间设在 17:00 之后，否则会基于上一个交易日并在 `data_gaps` 里说明。

**已知限制**：模型实际用的是 `LITELLM_MODEL`（备用模型的回退不会反映在 `model.fallback_from`）；计划只用本地行情和板块信号，没有接入新闻和业绩数据；盘前版（`preopen_push`）还没有读取这里的计划做「基准计划对比」。

## 约定

- 字段为 snake_case；Web 端经 `toCamelCase` 转换。阈值条目例外：字段原样返回，保证字典型阈值的键在 PUT 时不被改写。
- 百分比用百分数（`98.7` 表示 98.7%）；日期为 `YYYY-MM-DD`；`trade_date` 缺省为最近交易日。
- 错误体沿用 `{"error", "message", "detail"?}`：

| 状态码 | `error` | 场景 |
| --- | --- | --- |
| 400 | `validation_error` | 服务抛出 `DailyPlanValidationError`；阈值校验失败（`detail` 为问题列表） |
| 404 | `not_found` | 板块、股票计划或运行记录不存在 |
| 409 | `daily_plan_run_conflict` | 已有运行在进行中时再次 `POST /plan/runs`（手动或定时触发均占用同一个运行名额） |
| 409 | `config_version_conflict` | 保存阈值时配置已被修改（`detail.current_config_version`） |
| 422 | `validation_error` | 请求字段格式错误（股票代码非 6 位、股数 ≤ 0、现金为负等），沿用全局请求校验处理 |
| 500 | `internal_error` | 其他异常（含服务返回的数据不符合响应模型） |
| 503 | `daily_plan_unavailable` | 后端服务模块不可用 |

## 接口（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/boards/anomalies?trade_date=&board_type=industry\|concept&state=` | 市场概况与板块列表（含状态、标签、涨幅、超额、成交额放大、上涨占比、涨停数） |
| GET | `/boards/{board_id}?trade_date=` | 板块详情：6 项指标、近 8 日超额收益、代表股票（龙头 / 成交额 / 跟风）、判定理由、`can_pick` |
| GET | `/boards/thresholds` | `{"items": [...]}`：每个阈值的说明、默认值、当前值、依据等级（`medium` / `low` / `inferred`） |
| GET | `/boards/follows` | 我关注的板块：`{"trade_date", "boards": [{"id", "name", "board_type", "followed_at", "today"}]}`，`today` 是最近一次运行里该板块的列表行（没有时为 null） |
| PUT / DELETE | `/boards/follows/{board_id}` | 关注 / 取消关注（重复操作无副作用），返回最新的关注列表；未知板块 404 |
| GET | `/boards/recommendations?trade_date=` | 推荐重点跟踪的板块：`status`（`ready` / `generating` / `none` / `no_run`）、`source`（`model` 模型挑选 / `rule` 按规则排序）、`model`、`note`（按规则排序的原因）、`items: [{rank, reason, board}]` |
| POST | `/boards/recommendations/refresh` | 后台重新生成最近一次运行的推荐（返回 202 和当前状态，用 GET 轮询） |
| PUT | `/boards/thresholds` | 请求体 `{"overrides": {...}}`，空对象表示全部恢复默认；校验通过后写入 `BOARD_ANOMALY_THRESHOLDS`，返回刷新后的 `{"items": [...]}` |
| GET | `/plan/account` | 现金、持仓、总资金、仓位与生效的仓位规则 |
| PUT | `/plan/holdings` | 请求体 `{"cash", "holdings": [{code, shares, cost, buy_date?, horizon?}]}`，整体覆盖持仓快照（最多 50 只） |
| GET | `/plan/stocks?trade_date=` | 全部自选和持仓的计划摘要（持仓在前） |
| GET | `/plan/stocks/{code}?trade_date=` | 单只股票完整计划：短线 / 波段两个周期、条件计划、关键价位、股数测算、换股判断、数据缺口、实际使用的模型 |
| POST | `/plan/sizing` | 请求体 `{code, price, horizon}`，按当前仓位规则试算股数；股数由后端计算，前端只展示 `steps` 与各项上限 |
| GET | `/plan/runs/latest` | `{"run": PlanRun \| null}` |
| GET | `/plan/runs/{run_id}` | 指定运行详情：步骤（板块 / 选股 / 计划 / 换股 / 推送）、计数、失败项、飞书推送状态与预览 |
| POST | `/plan/runs` | 手动运行，返回 202 与运行记录 |
| GET | `/stocks/{code}/qa?days=30` | 个股已回复的投资者问答：深市来自巨潮互动易、沪市来自上证e互动（`platform`、`coverage`、`items: [{question, answer, asked_at, answered_at, platform}]`）；北交所返回空列表并说明原因 |
| GET | `/stocks/{code}/events?kinds=&days=30` | 个股公司事件（业绩预告 / 快报、增减持、回购、解禁、质押、调研、券商研报 `research`、龙虎榜 `dragon_tiger`、开盘啦打板 `limit_board`、热股榜 `hot_rank`、东方财富重点监控名单 `risk_watch`、严重异常波动 `price_anomaly`）与按固定规则算出的风险；只接受 A 股个股代码，指数代码返回 400 |
| GET | `/stocks/{code}/research?days=90` | 券商研报（Tushare `report_rc`）：每份报告的机构、评级、目标价、各年度 EPS / 净利润预测，以及每家机构只算最新一份的一致预期（`summary.consensus`，EPS 元、净利润亿元）和评级分布 |
| GET | `/stocks/{code}/capital?days=20` | 资金面：两融（`margin`）、新浪主力资金（`fund_flow`）、近 30 天龙虎榜（`dragon_tiger`，含席位、机构和沪深股通净额）、全市场北向 / 南向（`northbound`，口径见 `note`）；每部分独立，某一来源失败时该部分为 `{status: failed, error}` |

字段的完整定义见 `api/v1/schemas/boards.py` 与 `api/v1/schemas/daily_plan.py`，Web 侧类型见 `apps/dsa-web/src/types/boards.ts` 与 `apps/dsa-web/src/types/dailyPlan.ts`。

### 公司事件与计划风险

`src/services/stock_event_service.py` 每天把全市场的公司事件存进 `stock_event`（Tushare，见 [数据源说明](data-sources.md) 2.1）。生成次日计划时，每只股票按固定规则读出风险，写进计划的 `risks`，同时作为「公司事件」一节写进给模型的提示词：

- 未来 30 天解禁占流通股 1% 以上；
- 近 30 天单个股东减持占总股本 0.5% 以上；
- 最新一期股权质押比例 50% 以上；
- 近 60 天业绩预告为预减、首亏、续亏或略减。

生成计划时只读本地表，不发请求；本地没有数据（例如没有配置 Tushare）时这几条风险为空。

## Web 页面

| 路由 | 页面 | 数据来源 |
| --- | --- | --- |
| `/` | 跳转到 `/mine` | — |
| `/boards` | 板块异动：市场概况；「今日建议」（模型推荐的板块和理由，可重新生成）与「我关注的」（关注板块的当日状态，点击打开详情）；行业 / 概念切换、状态筛选（关注的板块带 ★）、板块详情（可关注 / 取消关注）与超额收益走势；可跳到该板块选股 | `/boards/*` |
| `/picks` | 机会选股：新建选股（策略 + 可选 1–5 个板块，`?board=` 预选该板块）、选股任务列表、点进任务（`?task=`）看进度或结果、加入自选 | `/screening/screen/tasks`（发起、列表、单个任务）、`/boards/anomalies`、`/boards/recommendations`、`/boards/follows`（板块选择：模型推荐、我关注的、今日异动，可搜索全部板块）与 `/stocks/watchlist/add`；选股结果中的 `horizons`、`entry_condition`、`invalid_condition`、`risks`、`note` 为可选字段，缺失时回退到已有字段 |
| `/mine` | 我的股票：次日计划服务未就绪时显示自选列表；账户、持仓与自选列表、计划详情（价格梯子、「怎么算的」、换股判断）、持仓与资金录入、记录买入；支持 `?code=` 深链 | `/plan/*`、`/stocks/watchlist/*` |
| `/preferences` | 设置：仓位规则、模型、异动阈值、运行和推送、数据状态；支持 `?tab=` 深链 | `/system/config`、`/boards/thresholds`、`/plan/*`、`/system/scheduler/status`、`/data/overview` |
| `/home` | 原首页（从 `/` 移到这里） | 不变 |

- 新页面不再链接任何旧页面：导航只有异动、选股、我的、设置（用户中心登录时多一个「退出」）；模型渠道、飞书推送、开启选股都在新页面里完成。原有页面（首页、问股、持仓台账、信号、选股工具、回测、提醒、数据中心、用量、`/settings`）的路由暂时保留，只能直接输入地址打开；桌面端深链 `/settings?category=system#desktop-version-info` 不受影响。
- 登录后默认进入 `/mine`。
- 新页面使用深色玻璃样式，样式只作用于 `.plan-shell` 容器，旧页面主题不变；动效遵守系统「减少动态效果」设置。

### 助手

页头「助手」和各页「问助手」按钮打开右侧助手抽屉，复用 `/agent/chat/stream` 与现有会话，并在 `context.daily_plan` 中带上当前页面上下文：

```json
{"daily_plan": {"trade_date": "2026-11-06", "kind": "board|stock|none", "board_id": "...", "stock_code": "000988", "horizon": "short|swing"}}
```

后端（`src/services/agent_chat_session_service.py`）对 `daily_plan` 的处理：

- `kind=stock`：这只股票是当次提问的默认对象。会话里没有用文字确认过的股票时，没点名对象的问题（如「明天怎么看」「风险呢」）按它回答，工具也只允许查它；问题里点名了别的股票时以点名的为准，并去掉 `daily_plan`。它不写入会话状态，所以取消抽屉里的对象后，后续提问不再带它。顶层的 `stock_code` 仍只是提示，不会单独选定对象。
- `kind=board`：板块 id、交易日写进提示词，模型用 `get_board_signal` 查询判定。
- 交易日和周期（短线 / 波段）以一行文字带进对话上下文（`src/agent/executor.py::format_daily_plan_context_line`）。
- 助手可以通过 `read_api_doc` / `call_api` 读写本文档里的全部接口（账户与持仓 `/plan/account`、`/plan/holdings`，计划 `/plan/stocks/{code}`，自选 `/stocks/watchlist` 等），包括改持仓、加自选、手动运行计划；说明见 `full-guide.md` 的「Agent 工具数据缓存与持久化」。
- `kind=none` 且问题里也没有对象时（如「寒潮有哪些股票值得关注」），个股工具可以查任何能校验通过的代码（`open` 作用域），题材新闻用 `search_market_news` 查询。

### 设置页写入的配置项

设置页通过现有 `/system/config` 读写以下键（带 `config_version` 冲突检查，掩码值保持不变）：

- 仓位规则：`POSITION_STOP_LOSS_PCT`、`POSITION_MAX_TOTAL_PCT`、`POSITION_MAX_HOLDINGS`、`POSITION_MAX_SINGLE_PCT`、`POSITION_STOP_MODE`
- 运行和推送：`SCHEDULE_TIME`、`DAILY_PLAN_ENABLED`；飞书应用机器人 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_STREAM_ENABLED`（长连接在服务启动时建立，改动后需重启服务），群 Webhook `FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、`FEISHU_WEBHOOK_KEYWORD`；密钥类字段不回显，留空表示不修改
- 模型（按用途映射现有键）：分析模型 `LITELLM_MODEL`、助手模型 `AGENT_LITELLM_MODEL`（空表示跟随分析模型）、备用模型 `LITELLM_FALLBACK_MODELS`、轻量模型 `LITELLM_LIGHT_MODEL`、识图模型 `VISION_MODEL`、温度 `LLM_TEMPERATURE`，推理强度 `LLM_REASONING_EFFORT`、`AGENT_REASONING_EFFORT`、`LITELLM_LIGHT_REASONING_EFFORT`
- 模型渠道（同一页左侧渠道列表、右侧编辑）：`LLM_CHANNELS` 与 `LLM_<NAME>_PROTOCOL`、`_BASE_URL`、`_API_KEY(S)`、`_ENABLED`、`_MODELS`（只保存打开的模型）；渠道名只能用小写字母、数字和下划线（`darren-cpa` 保存为 `darren_cpa`）；OpenAI 兼容、DeepSeek、Anthropic Messages 格式可经 `POST /system/config/llm/discover-models` 获取模型列表，其他格式手动添加；正在被某个用途使用的模型不能关闭，所在渠道不能停用或删除
- 选股：`SCREENING_ENABLED`（选股页「开启选股」按钮）
- 异动阈值：`BOARD_ANOMALY_THRESHOLDS`（经 `PUT /boards/thresholds` 写入）
- 数据状态（数据源行的「配置 / 修改」）：每个需要配置的数据源编辑 `GET /data/overview` 里该数据源的 `config_keys`——Tushare `TUSHARE_TOKEN`；TickFlow `TICKFLOW_API_KEY`、`TICKFLOW_PRIORITY`；必盈 `BIYING_LICENCE`、`BIYING_TIMEOUT_SECONDS`；Longbridge `LONGBRIDGE_APP_KEY`、`LONGBRIDGE_APP_SECRET`、`LONGBRIDGE_ACCESS_TOKEN`、`LONGBRIDGE_OAUTH_CLIENT_ID`；富途 `FUTU_OPEND_HOST`、`FUTU_OPEND_PORT`；Finnhub `FINNHUB_API_KEY`；Alpha Vantage `ALPHAVANTAGE_API_KEY`。内置数据源（AkShare、Efinance 等）没有可配置项。密钥类字段不回显，留空表示不修改；保存时带 `reload_now`，立即生效，随后刷新数据源状态

`BOARD_ANOMALY_THRESHOLDS` 已随板块异动服务落地：值是一个 JSON 对象，只写被覆盖的键（空表示全部默认），字典型阈值可只写其中的字段（如 `{"excess_floor": {"concept": 3}}`）；它在 `src/core/config_registry.py` 中列为不在通用设置页显示的键（由异动阈值页面通过 `PUT /boards/thresholds` 编辑），`.env.example` 里有说明。`POSITION_*` 与 `DAILY_PLAN_ENABLED` 同样列为不在通用设置页显示的键（由新设置页的「仓位规则」「运行和推送」编辑），默认值、取值范围和含义见上面的「次日计划服务」，`.env.example` 里有说明；保存 `DAILY_PLAN_ENABLED` 会重建运行时调度器，其余键下一次读取即生效（非法值回到默认值并写日志）。

### 板块异动阈值键

`GET /boards/thresholds` 返回下列键（`key` / 默认值 / 依据等级）。数值边界由 `PUT /boards/thresholds` 校验，不合法时返回 400 和问题列表；非法或互相矛盾的已保存值会整体退回默认值并写日志。

| 键 | 默认值 | 依据 |
| --- | --- | --- |
| `amount_ratio_mild` / `amount_ratio_strong` | 1.5 / 2.5（倍） | 中 |
| `rank_strong_top_pct` / `rank_weak_bottom_pct` | 10 / 10（%） | 中 / 推断 |
| `excess_floor` | `{"industry": 1.5, "concept": 2.5}`（%） | 推断 |
| `breadth_pct` | `{"strong": 70, "weak": 30}`（%） | 低 |
| `limit_up_effect` | 3（家，概念） | 低 |
| `mainline` | `{"limit_up": 5, "days": 2, "max_consecutive": 3, "min_days": 5}` | 低 |
| `break_rate_warn` / `break_min_touched` | 40（%） / 5（家） | 中 / 推断 |
| `confirm` | `{"days": 3, "strong_days": 2}` | 低 |
| `strengthening_rank_5d_top_pct` | 10（%） | 低 |
| `first_launch` | `{"prior_days": 5, "not_top_pct": 30, "breadth_pct": 60}` | 推断 |
| `divergence` | `{"min_conditions": 2, "breadth_pct": 50, "limit_up_drop_pct": 50}` | 推断 |
| `ebb_peak_pct` / `ebb` | 50（%） / `{"lookback_days": 10, "consecutive_days": 2, "min_conditions": 2, "limit_up_max": 1, "rank_5d_out_pct": 50}` | 低 / 推断 |
| `abnormal_drop` | `{"limit_down_count": 3, "drop7_ratio_pct": 30}`（概念补充条件） | 推断 |
| `broad_rally_pct` | 2（%） | 低 |
| `min_members` / `broad_max_members` | 10 / 800（只） | 推断 |
| `single_stock_share_pct` | 50（%） | 推断 |
| `cooling_days` | 10（交易日） | 推断 |
| `merge_jaccard` | 0.6 | 推断 |

`break_min_touched`、`min_members`、`broad_max_members`、`cooling_days` 在计算板块指标时生效，修改后需重算历史指标；其余键在判定时生效，下一次运行即用新值，旧运行的判定和阈值快照不变。

### 板块日度流程

```bash
python scripts/backfill_market_snapshot.py            # 首次：回填个股、板块与成分
python scripts/run_board_pipeline.py                  # 补全上一交易日 -> 上一日概念板块成分（逐板块）-> 入库 -> 指标 -> 判定，写 daily_runs
python scripts/run_board_pipeline.py --date 2026-11-06 --skip-ingest   # 只用本地数据重新计算
python scripts/backfill_market_snapshot.py --members-only              # 人工修复：按板块、按区间重刷成分
python scripts/run_board_pipeline.py --industry-membership             # 按需运行每周的行业板块成分刷新
```

**行业板块成分每周刷新**：行业板块成分变化慢，不放进每日流程，由独立的定时任务每周刷新一次（`BOARD_INDUSTRY_MEMBERSHIP_ENABLED` 默认开启，`BOARD_INDUSTRY_MEMBERSHIP_WEEKDAY` 默认 `6` = 周六，ISO 星期：1 周一 … 7 周日，`BOARD_INDUSTRY_MEMBERSHIP_TIME` 默认 `10:00`，本机时区）；任务每天在设定时间触发一次但只在设定的星期执行，不管当天是否交易日，取今天之前最近一个交易日的成分，逐个行业板块请求，已折叠的板块不再请求，未配置 `TUSHARE_TOKEN` 时记日志跳过。

**定时运行**：在开启定时模式（`SCHEDULE_ENABLED=true` 或 `--schedule`，Web / API 常驻进程或命令行定时循环均可）时，调度器每个交易日（A 股交易日历）按 `BOARD_PIPELINE_SCHEDULE_TIME`（默认 `17:00`，本机时区，与 `SCHEDULE_TIME` 相同；不影响每日分析的 `SCHEDULE_TIME`）运行一次；`BOARD_PIPELINE_ENABLED=false` 可关闭。需要配置 `TUSHARE_TOKEN`，未配置时当次运行跳过并写一行日志；每次运行都重新读取令牌，在 Web 设置里补上后无需重启。运行放在独立线程，上一次没跑完则跳过下一次；失败只记日志，不影响调度器和每日分析，`partial` 记为警告。调度器在 17:00 之后才启动时当天不会补跑，手动运行脚本即可（幂等）。

流程入口是 `src/services/board_pipeline.py::run_board_pipeline(trade_date, force=False, skip_ingest=False, skip_members=False, recompute_days=0)`，步骤和推算规则见[核心流程后端设计](design/core-backend-design.md) 2.9：D 日 17:00 `daily_basic`、`limit_list_d`、`dc_*` 还没发布，所以先补全 D-1（真实值替换前一天的推算值、取 D-1 的板块目录 / 行情 / 概念板块成分（逐板块 `dc_member`，失败或空返回的板块保留现有成分），并重算 D-1 的指标，D-1 已写下的信号不改写），再入库 D（换手率、流通市值、涨跌停由 `daily` + `stk_limit` + 上一日数据推算，`open_times` 留空，来源记在 `market_daily.quality`）。已有成功运行的交易日默认跳过；数据不完整的运行记为 `partial`，补齐数据后可直接再跑。助手工具 `get_board_signal` 读取同一份结果。

## 回滚

- 次日计划后端：关闭 `DAILY_PLAN_ENABLED` 即停止定时运行（已存计划保留）；彻底下线时删除 `src/services/daily_plan_*.py`、`src/repositories/daily_plan_repo.py`，并去掉 `runtime_scheduler.py` / `main.py` 里对 `build_daily_plan_jobs` 的调用；`plan_account`、`plan_holding`、`plan_run`、`plan_stock` 四张表是新增表，不影响其他功能，可留着也可删除。
- 删除 `api/v1/router.py` 中 `boards` 与 `daily_plan` 两个路由注册即可下线 API；`api/deps.py` 中的两个依赖函数无副作用。
- Web 回滚：把 `App.tsx` 中 `/` 改回渲染 `HomePage`、去掉 `PlanShell` 路由组，并恢复 `loginRedirect.ts` 默认值和 `SidebarNav` 首页路径。
