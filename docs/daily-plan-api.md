# 次日计划与板块异动：API 与 Web 入口

本文说明「板块异动 / 机会选股 / 我的股票 / 助手 / 设置」这组新页面的 API 契约和 Web 行为。功能语义以[核心功能说明](design/core-product-spec.md)为准，交互以[设计稿](design/core-redesign.html)为准。

## 当前状态

- API 层（`api/v1/endpoints/boards.py`、`api/v1/endpoints/daily_plan.py`）和 Web 页面已实现。
- 板块异动服务 `src/services/board_anomaly_service.py`（`BoardAnomalyService`）已实现，读取本地的板块指标和判定结果（见[核心流程后端设计](design/core-backend-design.md)第 2 节），数据由调度器每个交易日 17:00 运行的板块日度流程（`BOARD_PIPELINE_*`，也可用 `scripts/run_board_pipeline.py` 手动运行）产生；没有任何已完成的运行时 `/boards/anomalies` 返回空列表和说明，不返回 503。
- 次日计划服务 `src/services/daily_plan_service.py`（`DailyPlanService`）**尚未实现**。在它存在之前，`/plan/*` 一律返回 `503 {"error": "daily_plan_unavailable", "message": "次日计划服务未就绪"}`，Web 页面显示「次日计划服务未就绪」说明，不影响其他页面。
- API 在每次请求时按需导入服务模块，服务类需支持无参构造。

## 约定

- 字段为 snake_case；Web 端经 `toCamelCase` 转换。阈值条目例外：字段原样返回，保证字典型阈值的键在 PUT 时不被改写。
- 百分比用百分数（`98.7` 表示 98.7%）；日期为 `YYYY-MM-DD`；`trade_date` 缺省为最近交易日。
- 错误体沿用 `{"error", "message", "detail"?}`：

| 状态码 | `error` | 场景 |
| --- | --- | --- |
| 400 | `validation_error` | 服务抛出 `DailyPlanValidationError`；阈值校验失败（`detail` 为问题列表） |
| 404 | `not_found` | 板块、股票计划或运行记录不存在 |
| 409 | `daily_plan_run_conflict` | 已有运行在进行中时再次 `POST /plan/runs` |
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
| PUT | `/boards/thresholds` | 请求体 `{"overrides": {...}}`，空对象表示全部恢复默认；校验通过后写入 `BOARD_ANOMALY_THRESHOLDS`，返回刷新后的 `{"items": [...]}` |
| GET | `/plan/account` | 现金、持仓、总资金、仓位与生效的仓位规则 |
| PUT | `/plan/holdings` | 请求体 `{"cash", "holdings": [{code, shares, cost, buy_date?, horizon?}]}`，整体覆盖持仓快照（最多 50 只） |
| GET | `/plan/stocks?trade_date=` | 全部自选和持仓的计划摘要（持仓在前） |
| GET | `/plan/stocks/{code}?trade_date=` | 单只股票完整计划：短线 / 波段两个周期、条件计划、关键价位、股数测算、换股判断、数据缺口、实际使用的模型 |
| POST | `/plan/sizing` | 请求体 `{code, price, horizon}`，按当前仓位规则试算股数；股数由后端计算，前端只展示 `steps` 与各项上限 |
| GET | `/plan/runs/latest` | `{"run": PlanRun \| null}` |
| GET | `/plan/runs/{run_id}` | 指定运行详情：步骤（板块 / 选股 / 计划 / 换股 / 推送）、计数、失败项、飞书推送状态与预览 |
| POST | `/plan/runs` | 手动运行，返回 202 与运行记录 |

字段的完整定义见 `api/v1/schemas/boards.py` 与 `api/v1/schemas/daily_plan.py`，Web 侧类型见 `apps/dsa-web/src/types/boards.ts` 与 `apps/dsa-web/src/types/dailyPlan.ts`。

## Web 页面

| 路由 | 页面 | 数据来源 |
| --- | --- | --- |
| `/` | 跳转到 `/mine` | — |
| `/boards` | 板块异动：市场概况、行业 / 概念切换、状态筛选、板块详情与超额收益走势；可跳到该板块选股 | `/boards/*` |
| `/picks` | 机会选股：策略选择、可选的板块范围（`?board=`）、运行进度与结果、加入自选 | 现有 `/screening/*` 异步任务接口与 `/stocks/watchlist/add`；选股结果中的 `horizons`、`entry_condition`、`invalid_condition`、`risks`、`note` 为可选字段，缺失时回退到已有字段 |
| `/mine` | 我的股票：账户、持仓与自选列表、计划详情（价格梯子、「怎么算的」、换股判断）、持仓与资金录入、记录买入；支持 `?code=` 深链 | `/plan/*`、`/stocks/watchlist/*` |
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

### 设置页写入的配置项

设置页通过现有 `/system/config` 读写以下键（带 `config_version` 冲突检查，掩码值保持不变）：

- 仓位规则：`POSITION_STOP_LOSS_PCT`、`POSITION_MAX_TOTAL_PCT`、`POSITION_MAX_HOLDINGS`、`POSITION_MAX_SINGLE_PCT`、`POSITION_STOP_MODE`
- 运行和推送：`SCHEDULE_TIME`、`DAILY_PLAN_ENABLED`；飞书应用机器人 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_STREAM_ENABLED`（长连接在服务启动时建立，改动后需重启服务），群 Webhook `FEISHU_WEBHOOK_URL`、`FEISHU_WEBHOOK_SECRET`、`FEISHU_WEBHOOK_KEYWORD`；密钥类字段不回显，留空表示不修改
- 模型（按用途映射现有键）：分析模型 `LITELLM_MODEL`、助手模型 `AGENT_LITELLM_MODEL`（空表示跟随分析模型）、备用模型 `LITELLM_FALLBACK_MODELS`、轻量模型 `LITELLM_LIGHT_MODEL`、识图模型 `VISION_MODEL`、温度 `LLM_TEMPERATURE`，推理强度 `LLM_REASONING_EFFORT`、`AGENT_REASONING_EFFORT`、`LITELLM_LIGHT_REASONING_EFFORT`
- 模型渠道（同一页左侧渠道列表、右侧编辑）：`LLM_CHANNELS` 与 `LLM_<NAME>_PROTOCOL`、`_BASE_URL`、`_API_KEY(S)`、`_ENABLED`、`_MODELS`（只保存打开的模型）；渠道名只能用小写字母、数字和下划线（`darren-cpa` 保存为 `darren_cpa`）；OpenAI 兼容、DeepSeek、Anthropic Messages 格式可经 `POST /system/config/llm/discover-models` 获取模型列表，其他格式手动添加；正在被某个用途使用的模型不能关闭，所在渠道不能停用或删除
- 选股：`SCREENING_ENABLED`（选股页「开启选股」按钮）
- 异动阈值：`BOARD_ANOMALY_THRESHOLDS`（经 `PUT /boards/thresholds` 写入）

`BOARD_ANOMALY_THRESHOLDS` 已随板块异动服务落地：值是一个 JSON 对象，只写被覆盖的键（空表示全部默认），字典型阈值可只写其中的字段（如 `{"excess_floor": {"concept": 3}}`）；它在 `src/core/config_registry.py` 中列为不在通用设置页显示的键（由异动阈值页面通过 `PUT /boards/thresholds` 编辑），`.env.example` 里有说明。`POSITION_*`、`DAILY_PLAN_*` 的默认值、注册与 `.env.example` 说明随次日计划服务一起落地；在此之前设置页按普通值读写，保存后不影响任何现有流程。

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

- 删除 `api/v1/router.py` 中 `boards` 与 `daily_plan` 两个路由注册即可下线 API；`api/deps.py` 中的两个依赖函数无副作用。
- Web 回滚：把 `App.tsx` 中 `/` 改回渲染 `HomePage`、去掉 `PlanShell` 路由组，并恢复 `loginRedirect.ts` 默认值和 `SidebarNav` 首页路径。
