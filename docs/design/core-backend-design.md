# 核心流程后端设计（讨论稿）

- 日期：2026-11-07
- 状态：讨论稿，未实现
- 依据：[核心功能说明](core-product-spec.md)

本文记录后端逻辑的讨论结果，按主题逐步补充。已确定的写「决定」，还在讨论的写「待定」。

## 1. 已确定的决定

| 事项 | 决定 |
| --- | --- |
| 手动持仓录入 | 录入快照时，自动往现有账本写一笔「期初持仓」：`portfolio_trades` 写一笔买入（数量、成本，买入日期未填时用录入日并标注「日期未确认」），可用现金差额写入 `portfolio_cash_ledger`。持仓、成本和风险继续由现有的回放逻辑计算，以后加交易流水不需要迁移 |
| 板块历史 | 每天收盘后自己保存快照，表结构见第 2 节 |
| 数据来源 | 板块和全市场数据统一使用 Tushare Pro，复用现有的 `data_provider/tushare_fetcher.py`（限速、超时、重试）；接口说明见 [Tushare 文档](https://tushare.pro/document/2) |
| 助手实现 | 沿用现有的 Python Agent（`src/agent/`，LiteLLM 工具调用），只加一个精简的助手配置和几个新工具，见第 3 节；暂不引入 pi 等 Node 框架 |

## 2. 板块数据库表

### 2.1 设计原则

1. **存原始事实，板块指标自己算。** 每天保存全市场个股的收盘数据和板块成分，板块的成交额、上涨占比、涨停数、炸板率都由这两份数据算出。好处：
   - 行业、概念、不同数据商的板块用同一套口径；
   - Tushare 的个股和板块接口都支持按交易日一次取全市场，每天约 10 次请求，不必逐个板块请求；
   - 阈值校准或口径调整后，可以用本地数据重新计算历史，不再请求外部接口。
2. **数据商的板块指数涨幅和成交额（`dc_daily`）只作参考列**，不参与判定，用来核对自算结果。
3. **成分股用拉链表保存**，记录每只股票进入、退出板块的日期，任何一天的成分都能还原，新概念的上线日期也由此得出。
4. **事实和判定分开**：事实表每个交易日一份，可以重算；判定结果挂在每次运行上，阈值变了不改写旧结论。
5. 沿用现有存储方式：SQLite + SQLAlchemy ORM，新表通过 `create_all` 创建；JSON 存在 `Text` 列；金额统一为「元」（本地指数库里千元、万元的单位在入库时换算）。

### 2.2 表一览

```
board_catalog ─┬─< board_membership >── (股票代码)
               └─< board_daily  ──── 计算自 market_stock_daily + board_membership
market_stock_daily   每个交易日 × 全部 A 股
market_daily         每个交易日一行：基准、全市场宽度
daily_runs ──< board_signal   每次运行的判定结果
```

### 2.3 `board_catalog`：板块维表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | |
| `source` | String(8) | `dc` 东方财富（第一版只用这一家）；预留 `sw` 申万、`ths` 同花顺 |
| `board_type` | String(16) | `industry` / `concept` / `region`，对应 `dc_index.idx_type` 的行业、概念、地域板块 |
| `code` | String(32) | Tushare 板块代码，例如 `BK1184.DC` |
| `name` | String(64) | 当前名称 |
| `level` | String(16)，可空 | 行业层级，来自 `dc_index.level` |
| `first_seen_date` | Date | 第一次出现在板块列表的交易日，用于「新概念冷却」 |
| `last_seen_date` | Date | 最近一次出现的交易日；连续多日不出现则视为下线 |
| `status` | String(16) | `active` / `retired` |
| `merge_group` | String(64)，可空 | 成分重合度 ≥ 0.6 的概念归为一组，组内只展示一个 |
| `merge_computed_on` | Date，可空 | 合并分组的计算日期，随成分刷新一起更新 |
| `membership_checked_date` | Date，可空 | 最近一次核对成分的交易日；用于写准成分退出日期（`valid_to`），并让重复运行不再请求 |
| `created_at` / `updated_at` | DateTime | |

约束：`UNIQUE(source, board_type, code)`。

### 2.4 `board_membership`：成分股拉链表

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | |
| `board_id` | FK → `board_catalog.id` | |
| `stock_code` | String(10) | |
| `valid_from` | Date | 第一次在成分里看到的交易日 |
| `valid_to` | Date，可空 | 最后一次看到的交易日；为空表示当前仍在 |
| `origin` | String(16) | `observed` 来自 `dc_member` 的逐日成分；`backfill` 早于 2024-12-20、用最早可得成分倒推 |
| `fetched_at` | DateTime | |

- 约束：`UNIQUE(board_id, stock_code, valid_from)`；索引 `(board_id, valid_to)`、`(stock_code, valid_to)`。
- 某一天的成分：`valid_from ≤ D` 且 (`valid_to` 为空或 `valid_to ≥ D`)。
- 来源：`dc_member` 提供 2024-12-20 以来的逐日成分，按日期查询，单次最多 5,000 行。逐日结果压缩成拉链表保存：成分不变就不写新行。
- 刷新：**逐板块取**，不用「一天全部板块一次查询」。Tushare 限制翻页偏移不能大于 100,000（报错「offset不能大于100000」），而一天的 `dc_member` 超过 10.5 万行，不带 `ts_code` 的整日查询翻不完；单个板块单日不超过 5,000 行，一次请求取全。
  - **概念板块（约 530 个）每个交易日刷新**：每日流程（2.9）在 D 日 17:00 对 `board_catalog` 里在册（`active`）的概念板块逐个调用 `dc_member(ts_code=<板块>, trade_date=D-1)`，每个板块一次请求，折叠进拉链表（只处理该板块自己的拉链行）。
  - **行业板块（约 500 个）每周刷新一次**：行业板块成分变化慢，由单独的定时任务每周运行一次（默认每周六 10:00，`BOARD_INDUSTRY_MEMBERSHIP_*`，本机时区），不管当天是否交易日，取「今天之前最近一个交易日」的成分，规则与概念板块完全相同；该日行业成分已折叠则整步跳过、不发请求；未配置 `TUSHARE_TOKEN` 时记日志后跳过；也可用 `python scripts/run_board_pipeline.py --industry-membership` 按需运行。
  - D 日的判定使用最新可得成分，即 D-1 的快照（`dc_member` 当天 20:30 才发布），所以新出现的概念板块晚一天进入判定（它在 D-1 的 `dc_index` 里出现，D+1 日 17:00 才作为 D 日的目录补进来，见 2.9）。D 日自己的成分在 D+1 日 17:00 折叠，同时重算 D 日的指标（只改事实，见 2.9）。行业成分最多滞后一周。
  - **单板块失败不动成分**：某个板块的请求失败（限流、超时、权限、翻页不完整）、返回为空，或返回条数不足现有成分（现有 ≥ 10 只时）的 50%，该板块**保留现有成分、不关闭任何拉链行**（`board_catalog.membership_checked_date` 不前进，下次运行会重新取）；不会因为一次截断把大批股票误标为「移出板块」。连续 10 个板块失败判定接口不可用，中止本次运行，剩余板块下次再取。
  - **可断点续跑 / 幂等**：`membership_checked_date ≥ D` 的板块视为已折叠，不再请求；重跑只补没折叠成功的板块。
  - **质量记录**：每次运行把汇总写进 `market_daily.quality["ingestion"]["membership"][<board_type>]`（`concept` / `industry` 各一份，互不覆盖）：`status`（`ok` 有板块折叠且无失败、`partial` 部分失败、`failed` 没有任何板块折叠成功——全部失败或全部空返回，`skipped` 该日已折叠）、`boards`、`ok`、`failed`、`empty`、`skipped`（已是最新）、`rows`、`opened`、`closed`、`aborted`、`remaining`，以及前 20 个失败 / 空返回的板块代码和前 3 条错误。每日流程的「板块成分」步骤按它标为 `succeeded` / `partial` / `failed` / `skipped`。
  - **限速**：逐板块请求走 `TushareFetcher` 现有的限速和限流 / 超时退避（`_market_query`：限流提示等待后重试，权限不足不重试），请求频率单独用 `MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE`（默认 200）。成分接口每次只返回几十到几百行，200 次 / 分钟约 3 分钟取完全部概念板块；其余接口（`daily` 等，每次几千行）仍用 `MARKET_SNAPSHOT_REQUESTS_PER_MINUTE`（默认 60）。只在成分循环里临时抬高上限，结束后恢复。
  - **整日查询仅作可选兜底**：`BOARD_MEMBERSHIP_FULL_DAY_FALLBACK`（默认关闭）开启后，只有当天概念板块逐板块刷新一个都没成功时，才再试一次不带 `ts_code` 的整日查询（`refresh_membership_for_day`，沿用「翻页无进展、返回为空、条数低于上一份 50% 都判 `incomplete`、不动成分」的防护，结果记在 `quality["ingestion"]["membership"]["full_day"]`）。受 100,000 偏移上限限制，它通常取不全，不建议开启。
  - 冷启动和人工修复仍用按板块、按日期区间的取法（`refresh_all_membership`，`scripts/backfill_market_snapshot.py --members-only`）：每个板块一次请求覆盖上次刷新以来的全部日期，可断点续跑，同样使用成分限速。
  - 漏刷的日期以后可以按日期补齐，拉链表随之修正；每日流程只处理上一个交易日，漏跑多天时用回填脚本补。
- 2024-12-20 之前的成分只能倒推，有幸存者偏差，标为 `backfill`，页面和校准时注明。

### 2.5 `market_stock_daily`：全市场个股收盘快照

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| 字段 | 类型 | 来源 | 说明 |
| --- | --- | --- | --- |
| `trade_date` | Date | | 主键之一 |
| `code` | String(10) | | 主键之一，存 6 位代码，交易所由 `exchange_board` 区分 |
| `name` | String(32) | `stock_basic` | 当日名称，用于判断 ST |
| `exchange_board` | String(8) | `stock_basic` | `main` 主板、`gem` 创业板、`star` 科创板、`bse` 北交所 |
| `is_st` | Boolean | 名称 | |
| `is_suspended` | Boolean | `suspend_d` | 停牌股 `daily` 不返回，用停牌列表补一行，价格为空 |
| `list_date` | Date，可空 | `stock_basic` | 判断无涨跌幅限制的新股 |
| `prev_close` / `close` | Float | `daily` | `pre_close` 为除权昨收 |
| `pct_chg` | Float | `daily` | % |
| `amount` | Float | `daily` | 元（接口单位为千元，入库时 × 1000） |
| `turnover_rate` | Float，可空 | `daily_basic`；未发布时推算 | % |
| `float_mv` | Float，可空 | `daily_basic.circ_mv`；未发布时推算 | 流通市值，元（接口单位为万元，× 10000），用于加权 |
| `limit_state` | String(16) | `daily_basic.limit_status`；未发布时由 `daily` + `stk_limit` 推算 | `none` / `up` / `one_word_up` / `down` / `one_word_down`，由接口的 0–6 状态码换算 |
| `touched_limit_up` | Boolean | `limit_list_d`；未发布时推算 | 当日在涨停或炸板榜里；在炸板榜即为炸板 |
| `open_times` | Integer，可空 | `limit_list_d` | 炸板次数；`limit_list_d` 未发布时为空（不推算） |
| `consecutive_up` | Integer，可空 | `limit_list_d.limit_times`；未发布时推算 | 连板数 |
| `data_source` | String(32) | | |
| `ingested_at` | DateTime | | |

- 主键 `(trade_date, code)`；索引 `(code, trade_date)`。
- `limit_list_d` 不统计 ST 股，ST 股的涨停相关字段为空；ST 股本来就不参与板块统计，不影响判定。
- 17:00 运行时 `daily_basic`、`limit_list_d` 还没发布，上面标「未发布时推算」的字段按 2.9 的规则推算，来源记在 `market_daily.quality["ingestion"]`（`basic_source` / `limit_source` 取 `daily_basic` / `limit_list_d` / `derived`），次日由真实值覆盖。
- 次日涨跌停价只在生成个股计划时按规则计算（主板 ±10%、创业板和科创板 ±20%、北交所 ±30%、ST ±5%），不存在这张表里。
- 数据量约每天 5,400 行、每年 135 万行，SQLite 可以承受。保留时长做成配置项（建议默认 250 个交易日），更早的数据可以导出到本地数据湖。
- 不写入现有的 `stock_daily`：那张表只存被分析的股票，并带均线等计算字段，用途和保留策略都不同。

### 2.6 `market_daily`：基准和全市场

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `trade_date` | Date PK | |
| `benchmark_code` | String(16) | 默认中证全指 `000985`，做成配置项 |
| `benchmark_ret` | Float | % |
| `total_amount` | Float | 全市场成交额，元 |
| `up_count` / `down_count` | Integer | |
| `limit_up_count` / `limit_down_count` / `touched_up_count` | Integer | |
| `break_rate` | Float，可空 | 全市场炸板率 |
| `all_a_ret` | Float | 全 A 等权涨幅，用于判断「普涨日」 |
| `quality` | Text JSON | 缺失或延迟的数据项 |

### 2.7 `board_daily`：板块日度指标

每个交易日、每个板块一行，由 2.4–2.6 计算得出，可以整日重算。

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| 键 | `trade_date`、`board_id` | 主键 |
| 成分 | `member_count`、`valid_member_count` | 后者剔除 ST、停牌、无涨跌幅限制的新股 |
| | `excluded_reason` | 可空：`broad`（成分超过 800 只）、`too_few`（不足 10 只）、`cooling`（新概念冷却期） |
| 涨幅 | `ret_cap` | 流通市值加权涨幅，判定使用 |
| | `ret_eq` | 等权涨幅，展示用 |
| | `vendor_ret`、`vendor_amount` | `dc_daily` 的板块涨幅和成交额（元），只作参考和核对 |
| | `excess_ret`、`excess_vs_median` | 相对基准、相对同类板块中位数 |
| | `rank_pct` | 当日超额在同类板块中的百分位 |
| | `ret_5d`、`excess_5d`、`rank_5d_pct` | 近 5 日 |
| 成交 | `amount`、`amount_ma5_prev` | 后者为前 5 日均值，不含当天 |
| | `amount_ratio_5d`、`amount_share`、`amount_peak_20d` | 放大倍数、占全市场比例、近 20 日峰值 |
| 宽度 | `up_count`、`down_count`、`up_ratio` | |
| 涨停 | `limit_up_count`、`limit_down_count`、`one_word_count`、`touched_up_count`、`max_consecutive` | |
| | `break_rate` | 触及涨停不足 5 家时为空 |
| | `drop7_ratio` | 跌幅超过 7% 的个股占比 |
| 驱动 | `top_contrib_code`、`top_contrib_share` | 贡献最大的个股及其占比，用于「单股驱动」 |
| | `leader_code`、`top_amount_code` | 领涨股、成交额最大股 |
| 质量 | `quality` | Text JSON：缺失的数据项、成分来源（`observed` / `backfill`） |
| | `calc_version` | 指标口径版本，口径变化时整表重算 |
| | `computed_at` | |

索引：`(board_id, trade_date)`、`(trade_date, rank_pct)`。

计算口径（实现见 `src/services/board_metrics_service.py`，`calc_version` 在公式变化时递增，整日可重算）：

- 有效成分：剔除停牌、ST、退市整理、无涨跌幅限制的新股（主板 / 创业板 / 科创板上市前 5 个交易日，北交所首日）、复牌首日，以及缺收盘价或涨跌幅的股票；各类剔除的数量写入 `quality.exclusions`，不静默丢弃。
- `ret_cap` 按**前一日流通市值**加权（`float_mv / (1 + pct_chg/100)`，与指数的通行做法一致，涨幅大的股票不会因为涨了而权重变大），至少 80% 的有效成分有市值才计算，否则为空。
- `rank_pct` 为同类板块中的中位秩百分位，100 表示最强（前 10% 即 `rank_pct ≥ 90`）；同类板块不足 5 个时为空。`excess_vs_median` 的中位数取自同一个同类池（不含 `broad`、`too_few`）。
- 比例类字段（`up_ratio`、`break_rate`、`drop7_ratio`、`top_contrib_share`）一律存**百分数**；`amount_share` 存比例（0–1）。
- 近 5 日字段由逐日 `ret_cap`（和基准）复利得到，5 个交易日中任何一天缺失都为空；`amount_ma5_prev` 需前 5 日完整；`amount_peak_20d` 至少 10 日数据。
- 涨停、炸板、连板等字段只在 ≥ 95% 的有效成分带有对应数据时才产出，否则为空并在 `quality.flags` 里标记；「没有涨停」（0）和「没取到」（空）严格区分。
- 一字板计入 `limit_up_count`，同时单列 `one_word_count`。
- 交易日序列优先取交易所日历：某个交易日没有入库时，窗口里会留出这个缺口（相关多日字段为空并标记 `missing_sessions`），而不是悄悄把窗口拉长；没有日历时退回已入库的日期。
- 没有成分数据的板块写 `excluded_reason = too_few` 并在 `quality.flags` 里加 `no_membership`；`cooling` 只用于「首次出现日晚于系统最早入库日」的概念（最早入库日出现的板块真实上线时间未知，不算新概念）。
- 阈值里的 `min_members`、`broad_max_members`、`cooling_days`、`break_min_touched` 在计算指标时生效，修改后需重算（`scripts/run_board_pipeline.py --recompute-days N`）才会作用到历史日期；各日实际使用的值记在 `quality.params`。

### 2.8 `board_signal`：异动判定结果

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | |
| `run_id` | FK → `daily_runs` | |
| `trade_date` | Date | |
| `board_id` | FK → `board_catalog.id` | |
| `signal_type` | String(16) | `first_launch` / `strengthening` / `divergence` / `ebbing` / `abnormal_drop` / `insufficient_history` / `none`（与 API 的 `BoardState` 一致） |
| `flags` | Text JSON | `tags`（`mainline` / `supported` / `single_stock` / `cooling` / `merged`）、`excluded`（未参与判定的原因）、`core_missing`、`blocked_by`、`merge`（合并组角色）等 |
| `evidence` | Text JSON | 逐条条件的实际值、阈值、是否满足（`met` 为 `true` / `false` / `null`，`null` 表示数据不足）、`data_gaps`、`undetermined`（数据不足无法判定的状态）、使用的阈值、驱动因素；页面上的「依据」读 `lines` |
| `driver` | String(16) | `news` / `capital` / `earnings` / `unknown`；目前没有消息 / 业绩数据，只有成交额明显放大（≥ 明显放量阈值）的启动 / 走强会标为 `capital`，其余为 `unknown` |
| `reason` | Text | 一句话原因 |

- 约束：`UNIQUE(run_id, board_id)`。
- 阈值快照存在 `daily_runs.thresholds_snapshot`（`values` 是生效的全部阈值，`overridden` 是被覆盖的键），同一次运行内的所有判定使用同一组阈值。`daily_runs.steps` 是步骤列表（`ingest` / `membership` / `metrics` / `signals`，每步有 `key`、`label`、`status`、`finished_at`、`detail`、`data`），`status` 取 `running` / `succeeded` / `partial` / `failed`；有数据缺口的运行记为 `partial`，之后可以再跑一次补算。
- 同一天再次运行（`--force`）会新增一条运行记录，旧运行的判定原样保留；读取时取该交易日最新的完成运行。
- 实现见 `src/services/board_signal_service.py`，规则与产品说明 3.3 一一对应。

#### 2.8.1 判定实现口径

- **三值逻辑**：每个条件是满足 / 不满足 / 数据不足。「与」「或」按三值运算（任一不满足则整体不满足；否则有数据不足则整体数据不足）。数据不足**不会**被当成不满足：一个状态的结果是「数据不足」，且板块当日显著（强日、弱日或近 5 日排名在前）时，输出 `insufficient_history` 并在 `evidence.data_gaps` 列出缺什么；当日不显著的板块输出 `none`，但 `evidence.undetermined` 仍记录哪些状态无法判定，页面的 `history_note` 汇总历史积累天数和各状态需要的天数。
- **优先级**：异常下跌 > 退潮 > 高位分歧 > 持续走强 > 首次启动。更高优先级的状态数据不足、而较低优先级的状态成立时，输出较低的状态，并把数据缺口写进 `data_gaps`。
- **普涨日保护**：全 A 等权涨幅 ≥ 阈值时，涨停数、炸板率、上涨家数占比类条件不参与（`skipped`），只看排名和超额；证据里会写出被忽略的条件。当日全 A 涨幅缺失时按非普涨日处理并告警。
- **单股驱动**：首次启动 / 持续走强要求最大单股贡献不超过阈值；不满足时输出 `none` 并打 `single_stock` 标签（列表里照常显示这类板块，说明为什么没有报异动）。高位分歧、退潮、异常下跌不受此规则影响。
- **新概念冷却**：只拦截首次启动。其余条件成立（或因数据不足无法判定）且当日显著时输出 `insufficient_history` 并打 `cooling` 标签。API 里的 `cooling` 标签表示「冷却期」，不是「降温」。
- **历史不足的来源**：板块历史由本地逐日积累，所以天数不够、成分尚未刷新的板块天然缺历史；成分在 2024-12-20 之前的日期是倒推的（`quality.member_origin = backfill`），页面和详情里会注明存在幸存者偏差。
- **首次启动的「近 5 日超额不在前 30%」** 取前一交易日的 5 日排名（当日强势不应抬高自己的排名）。**主线的「持续 ≥ 5 天」** 按连续跑赢基准的天数计。**有承接** 只用于概念：成交额不低于前一日且仍有涨停。
- **概念合并**：成分重合度（Jaccard）≥ 阈值的概念以成分最多的板块为锚点分组（`board_catalog.merge_group`）；组内状态最强的板块作为代表显示并打 `merged` 标签，其余成员在列表里隐藏、详情里说明。

### 2.9 每日数据流程

**每个交易日 17:00 运行一次**（调度器注册，见 `BOARD_PIPELINE_*`，本机时区，仅交易日，需要 `TUSHARE_TOKEN`）。行业板块成分不在这里刷新，由每周一次的独立任务处理（2.4）。全部按 `trade_date` 查询，一次取全市场。积分要求、单次行数上限、发布时间摘自 Tushare 文档和接口说明；当前账号有 15,000 积分，满足下表所有接口的要求。

交易日 D 各接口的发布时间：

| 接口 | 发布时间 | 说明 |
| --- | --- | --- |
| `stk_limit` | ≈ 09:00 | D 的涨停价、跌停价，含基金；每次 5,800 行 |
| `daily` | 16:00–17:00 | 每次 6,000 行；17:00 时通常已发布，没发布则当天运行失败（见下） |
| `daily_basic` | 17:00–19:00 | 2,000 积分；每次 6,000 行 |
| `limit_list_d` | 18:30–19:30 | 5,000 积分起；每次 2,500 行 |
| `dc_index`、`dc_daily`、`dc_member` | 20:30 | 6,000 积分；每次 5,000 / 2,000 / 5,000 行 |
| `suspend_d` | 09:00 和 21:00 | 17:00 取到的是早上那一版 |

所以 D 日 17:00 能拿到 D 的 `daily`、`stk_limit`、`suspend_d`，拿不到 D 的 `daily_basic`、`limit_list_d` 和全部 `dc_*`；D-1 的所有接口都已发布。流程因此是「D-1 补全 + D 先推算」：

| 步骤 | 对象 | 接口 | 写入 | 请求 |
| --- | --- | --- | --- | --- |
| 1 补全 D-1 | D-1 个股 | `daily`、`daily_basic`、`limit_list_d`、`suspend_d`、`index_daily`、`stock_basic`（D-1 已是 `complete` 时整步跳过） | 用真实值重写 D-1 的 `market_stock_daily` 和 `market_daily` 基准，替换前一天推算的值 | 约 8 |
| 2 | D-1 板块目录与行情 | `dc_index`（行业、概念各一次）、`dc_daily` | `board_catalog`（新板块在这里出现，晚一天）、`board_daily.vendor_ret/vendor_amount` | 3 |
| 3 | D-1 概念板块成分 | `dc_member(ts_code=<板块>, trade_date=D-1)`，在册概念板块各一次 | 逐板块折叠进 `board_membership`（2.4：失败 / 空返回的板块保留现有成分，已折叠的板块跳过） | 约 530，按成分限速（默认 200 次 / 分钟）约 3 分钟 |
| 4 入库 D | D 个股 | `daily`（必需）、`stk_limit`、`suspend_d`、`index_daily`；`daily_basic`、`limit_list_d` 先取一次，没发布就推算 | `market_stock_daily`（D）、`market_daily` 基准；D 的板块目录和行情不取 | 约 7 |
| 5 重算 D-1 | 无 | 只读本地表 | 步骤 1 或 3 改动了 D-1 的输入时，重算 D-1 的 `board_daily`、`market_daily`（只改事实字段） | 0 |
| 6 计算 D | 无 | 只读本地表 | `board_daily`、`market_daily`（D） | 0 |
| 7 判定 D | 无 | | `board_signal`（D 这次运行） | 0 |
| 8 推荐板块 | 无 | 分析模型一次调用（`LITELLM_MODEL` + `LLM_REASONING_EFFORT`） | `board_recommendation`（这次运行的 3–5 个推荐板块和理由）；模型没配置、调用失败或答非所问时按规则排序（强势状态优先、再看超额收益），并记下原因 | 0 |

步骤 3 以外合计约 25 次请求，默认每分钟 60 次的限速下不到一分钟；步骤 3 约 530 次小请求，单独按 `MARKET_SNAPSHOT_MEMBERS_REQUESTS_PER_MINUTE`（默认 200）限速。**D-1 补全放在入库 D 之前**，这样 D 的推算用的是 D-1 的真实流通市值和真实连板数。`stock_basic`（名称、上市日期、板块）每周刷新一次即可（进程内缓存）。全市场 A 股约 5,400 只，接近 `daily` 和 `daily_basic` 6,000 行的上限，取数时按行数判断是否需要分页，不能假定一次取全。

**推算规则（D 日，仅在真实接口未发布时使用，对非停牌股）**

| 字段 | 规则 |
| --- | --- |
| 涨停收盘 | `close ≥ up_limit − 0.005`（半个最小报价单位的容差，防浮点误差）；`open`、`low` 也在涨停价则为 `one_word_up` |
| 跌停收盘 | `close ≤ down_limit + 0.005`；`open`、`high` 也在跌停价则为 `one_word_down` |
| `limit_state` | 涨停收盘 → `up` / `one_word_up`，跌停收盘 → `down` / `one_word_down`，否则 `none`；ST 股也推算（接口有它们的涨跌停价，和 `daily_basic.limit_status` 口径一致） |
| `touched_limit_up` | 非 ST：`high ≥ up_limit − 0.005`；炸板即触及但未涨停收盘；ST 保持为空（和 `limit_list_d` 一致） |
| `open_times` | 为空，等 `limit_list_d` |
| `consecutive_up` | 非 ST 且涨停收盘时：上一行是上一交易日、未停牌且状态已知——上一日涨停则上一日连板数 + 1（上一日连板数为空则为空），否则 1；其余情况为空（不猜，也不重置为 1）。因为 D-1 已先补全，上一日的连板数通常是真实值 |
| `float_mv` | 上一份快照的 `float_mv × (1 + pct_chg / 100)`，即流通市值权重仍是「前一日流通市值」，和 2.7 的市值加权一致 |
| `turnover_rate` | `vol`（手）÷ 流通股本，流通股本 = 上一份快照的 `float_mv ÷ close`，即 `vol × 10⁴ ÷ 流通股本` 个百分点 |

- 推算只在 `allow_derived=True`（每日流程）时启用，回填和人工入库保持原行为：接口缺失就是质量标记，不推算。上一份快照与 D 相隔超过 10 个自然日时不推算流通市值和换手率。
- `stk_limit` 请求失败时记入 `missing`（状态 `partial`），涨跌停相关字段保持为空，不当作 `none`；真实接口出了错（而不是「未发布」）也保持在 `missing` 里。
- 质量记录：`market_daily.quality["ingestion"]` 里 `derived`（被推算替代的接口）、`basic_source`、`limit_source`、`derived_from`（推算所用快照日期）、`boards`（`done` / `pending`）；`quality["metrics"]["input_sources"]` 带到指标的质量里。入库状态：`complete`（全部真实）、`provisional`（有推算值或板块目录待补，没有失败）、`partial`（有接口失败或翻页不完整）。`provisional` 的日期下次运行会整日重新入库，不会被当作已完成而跳过。
- D 当天的 `daily_basic` / `limit_list_d` 如果已经发布（比如手动在晚上补跑），直接使用，不推算。

**运行结果**

- `daily` 缺失（没发布或非交易日）：当天运行 `failed`，不写信号；D-1 补全和成分折叠已经完成的部分保留，重跑只补缺的。
- 推算本身不算告警：入库步骤显示「已推算」，运行状态仍可为 `succeeded`；步骤失败、成分不完整、质量缺失才是 `partial`。
- **D-1 的信号不改写**：判定属于当时那次运行（一次运行一批信号，见 2.8），D-1 补全只更新 D-1 的事实字段（`board_daily`、`market_daily`），D-1 运行里已经写下的信号和证据保持原样；D 的 5 日涨幅等字段读到的是补全后的 D-1 数据。
- 推荐板块在运行状态写定之后执行，失败只记在步骤里，不影响运行状态；只在实时入库的运行里执行（与隔夜美股联动相同），也可以在板块异动页点「重新生成」（`POST /boards/recommendations/refresh`，后台执行）。输入是当天有异动的板块（最多 40 个）和用户关注的板块（`board_follow`，只作参考）。
- 每步幂等：D-1 已 `complete` 就不再请求，已折叠该日成分的板块不再请求，重跑只补缺的（`--force` 才新增一次运行）。
- 成分步骤的结果：全部成功 `succeeded`；部分板块失败 / 连续失败中止 `partial`，没有任何板块折叠成功（全部失败或全部空返回）`failed`，这些都只是告警，运行继续；失败和空返回的板块保留现有成分。

**代价**：D 的判定用 D-1 的成分（新概念晚一天）、推算的换手率 / 流通市值和没有炸板次数的涨停数据；D 的这些字段在 D+1 日 17:00 被真实值替换，D 的指标随之重算，但 D 当天的信号不重写。

### 2.10 冷启动回填

| 数据 | 接口 | 范围 | 请求数 |
| --- | --- | --- | --- |
| 个股 | `daily`、`daily_basic`、`limit_list_d` 按交易日 | 近 60 个交易日 | 约 180 |
| 板块行情 | `dc_daily` 按交易日 | 近 60 个交易日，最早可到 2020 年 | 约 60 |
| 板块成分 | `dc_member` 按板块、日期区间 | 2024-12-20 以来，逐日真实成分 | 约 1,000，限速 |
| 基准 | 本地数据湖 `index_daily` | 全部 | 0 |

- 回填是一次性任务，限速、记录进度、可以断点续跑；遵守频控规则，不在测试里调用真实接口。
- 2024-12-20 以来的成分是真实逐日数据，判定和校准都没有幸存者偏差。

### 2.11 待定

1. **行业分类：** 改用 Tushare 后，建议行业和概念都用东方财富（`dc_*`）。
   - 理由：同一家的成分接口和行情接口都有历史，和现有「板块选股」口径一致。
   - 申万保留为以后的扩展：`index_member_all` 有纳入、剔除日期，`sw_daily` 有日线，本地数据湖也有历史。
   - 选定后，「在此板块中选股」也改为读 `board_membership`，保证异动和选股用同一份成分。
2. **地域板块：** `dc_index` 也提供地域板块，建议第一版不纳入异动判定。
3. **`market_stock_daily` 保留多久**，以及是否需要和本地数据湖同步。

## 3. 助手的盘中数据

### 3.1 原则

1. **产品口径不变。** 次日计划只在收盘后生成。盘中助手用实时数据回答问题、检查计划里的条件是否已触发，不生成新的买卖建议，也不改写计划。用户追问「要不要改」时，回答写明这是盘中看法，计划没有变。
2. **助手知道现在是什么时候。** 每次提问都带上市场阶段（盘前、集合竞价、盘中、午休、收盘后、非交易日）和当前时间，复用 `src/core/trading_calendar.py` 的 `build_market_phase_context`。
3. **每个实时数字都带时间。** 回答写「截至 10:32」；快照超过时效的标「数据延迟」。
4. **盘中数据只放内存缓存**，不写入 `stock_daily` 和 `market_stock_daily`，避免把未收盘的 K 线当成收盘数据。收盘数据仍由 17:00 的每日运行写入。

### 3.2 数据来源：Tushare 优先

| 问题 | 接口 | 用法 | 权限 |
| --- | --- | --- | --- |
| 个股现价、今日高低、成交额、买一卖一 | `rt_k`，按单只代码 | 提问时取，短时缓存 | 单独开通「A股日线RT」 |
| 今天的分时走势 | `rt_min_daily`，5 分钟 | 用户问到走势时才取 | 单独开通「A股分钟RT」 |
| 板块盘中强弱、涨停家数 | `rt_k` 全市场，按 `6*.SH`、`0*.SZ`、`3*.SZ`、`9*.BJ` 分批 | 全市场快照所有提问共用一份缓存；板块指标用上一交易日的 `board_membership`，按第 2 节同一口径在内存里算 | 同上 |
| 涨停价、跌停价 | `stk_limit` | 每天 09:00 后取一次，用来判断盘中是否涨停、炸板 | 2,000 积分 |
| 大盘指数 | `rt_idx_k` | 上证、深证、创业板等交易所指数 | 单独开通「指数日线RT」 |

- 东方财富板块没有实时接口，所以盘中板块数据只能自己算，正好和收盘后的口径一致。
- 盘中的「全 A 涨幅」用全市场快照按流通市值加权自算，和收盘后 `market_daily.all_a_ret` 的口径一致。
- 现有的 `TushareFetcher.get_realtime_quote` 用的是 `quotation` 接口，`get_market_stats` 已经在用 `rt_k`。实现时把单只报价也改为 `rt_k`，仍走现有的 `REALTIME_SOURCE_PRIORITY`（配置了 `TUSHARE_TOKEN` 时已经自动把 tushare 排在第一位），不另开一条取数链路。
- 缓存时效需要新增配置项（例如全市场快照 60 秒），实现时同步更新 `.env.example` 和文档。

### 3.3 权限不足或请求失败时

- `rt_*` 接口按接口单独开通，不包含在积分里。没开通或请求失败时，先用必盈 API（见下），再退回现有的实时行情链路（腾讯、新浪、efinance、东方财富），回答里写明数据来源。
- **必盈 API 作为第二实时源，按包年版证书设计**（[文档](https://www.biyingapi.com/doc_hs)）。证书放在请求路径里，实现时新增 `BIYING_LICENCE` 配置：和 `TUSHARE_TOKEN` 一样登记到 `src/core/config_registry.py`（`category=data_source`、`ui_control=password`、`is_sensitive=True`），可以写在 `.env` 或环境变量里，也可以在 Web 设置页填写（经 `PUT /api/v1/system/config` 写回 `.env`，页面只显示是否已配置）；日志和请求记录里把路径中的证书打码。包年版每分钟 3,000 次、每日和总次数不限，不依赖深度行情包：

  | 用途 | 接口 | 更新 | 用法 |
  | --- | --- | --- | --- |
  | 个股报价 | `hsstock/real/time`（券商源） | 实时 | 单只提问的首选 |
  | 个股报价（公开源） | `hsrl/ssjy` | 盘中每分钟 | 券商源失败时用；带换手、量比、流通市值、涨速 |
  | 多只股票 | `hsrl/ssjy_more`，每次最多 20 只 | 实时 | 持仓、自选、候选股一起问时用 |
  | 全市场快照 | `hsrl/real/all`（公开源） | 盘中实时 | 盘中板块强弱、全 A 涨幅；单独限流每分钟约 3 次 |
  | 买卖五档 | `hsstock/real/five` | 实时 | 问到盘口时才取 |
  | 分时 K 线 | `hsstock/latest`，5 分钟 | 实时 | 问到今天走势时才取；助手仍用 5 分钟；网页分时图按包年逐笔成交聚合为 1 分钟 |
  | 指数 | `hsindex/real/time` | 实时 | 大盘指数 |

  - 全市场快照每分钟约 3 次，配合「提问时取、60 秒缓存」够用；同一分钟内的并发提问共用一次请求，不重复取。
  - 实现时按 `ssjy/all`（券商源）和 `real/all`（公开源）各取一次样本，确认字段和行数后再定用哪个；第一版先按公开源设计。
  - 必盈的板块是券商口径（如 `101076.BKZS`），每天 16:20 更新，没有盘中板块行情，也和东方财富板块对不上，所以盘中板块仍用全市场快照加 `board_membership` 自算。
  - 字段单位和 Tushare 不同（成交量是「手」、市值是「元」），接入时在 fetcher 里统一换算。
- 全市场快照取不到时，板块问题只回答上一交易日收盘后的判定，并写明「盘中板块数据不可用」。
- 盘中数据缺失时照常回答，但缺失的条件写「数据不足」，不当作「未触发」。

### 3.4 助手要加的工具

在现有工具基础上，助手配置只保留和核心流程相关的工具（行情、K 线、新闻、持仓），再加三个：

| 工具 | 返回 |
| --- | --- |
| `get_next_day_plan` | 某只股票在最近一次运行里的结论、买入条件、失效条件、止损价和股数 |
| `check_plan_conditions` | 用实时价格逐条判断计划里的条件：已触发、未触发、数据不足，附快照时间 |
| `get_board_signal` | 板块最近一次收盘判定；盘中时附上自算的实时强弱 |

### 3.5 待定

1. **实时权限：** Tushare 账号是否已开通「A股日线RT」「A股分钟RT」「指数日线RT」；必盈按包年版设计（已决定）。证书如果不是包年版或白金版，全市场快照和多只股票接口都不可用。两边都取不到全市场快照时，盘中个股报价仍可用，盘中板块强弱不可用。
2. **全市场快照的刷新方式（已决定）：** 只在有人提问时取，缓存时效内的后续提问直接复用；不做盘中定时刷新。代价是缓存过期后的第一次提问要多等几秒。

## 4. 助手的记忆

### 4.1 现状

| 已有 | 作用 | 位置 |
| --- | --- | --- |
| 会话消息 | 每个会话的问答原文 | `conversation_messages` |
| 滚动摘要 | 超过阈值后把早先的对话压缩成摘要，最近几轮原样保留 | `conversation_summaries`，`src/agent/chat_context.py` |
| 会话状态 | 当前讨论的股票、选用的技能 | `conversation_session_states` |
| `AgentMemory` | 过去分析的结果和命中率，用于校准置信度 | `src/agent/memory.py`；只在多 Agent 模式用，`AGENT_MEMORY_ENABLED` 默认关闭 |

缺的是跨会话的记忆：用户说过的偏好、对某只股票的看法、以前聊过的事，换个会话就没了。

### 4.2 原则

1. **系统里已有的事实不进记忆。** 持仓、成本、现金、仓位规则、次日计划、板块判定，每次都通过工具读当前值，不在记忆里另存一份。
2. **规则不进记忆。** 「止损改成 8%」这类话属于仓位规则，不写进记忆；用户要求时助手通过设置接口直接修改（`call_api`，见 `full-guide.md`），否则提示去设置里改。
3. **优先级固定：** 账本和规则 > 笔记 > 回忆。笔记是用户确认过的内容；回忆是从过往对话里自动召回的，只作参考。
4. **带时间的数字不能当成现在的数字。** 历史对话和回忆里的价格、持仓都带上当时的日期，助手回答前重新取当前值。
5. **长期回忆是弱依赖、可插拔。** 不装插件、插件没配置、服务不可用，助手都照常回答，只在依据行注明「未使用长期回忆」。

### 4.3 三层

| 层 | 内容 | 实现 | 依赖 |
| --- | --- | --- | --- |
| 会话（短期） | 本次对话原文和滚动摘要 | 沿用现有三张表和压缩逻辑，不改 | 内置 |
| 笔记 | 偏好；对某只股票、某个板块的看法 | LLM wiki：每个范围一页 Markdown，由助手提议修改、用户确认 | 内置 |
| 回忆（长期） | 过去所有对话里聊过的事 | 回忆插件，第一个实现是 Hindsight | 可选插件 |

另有「回顾」：以前给这只股票的计划、条件有没有触发、之后的涨跌。这是系统事实，不属于记忆，由新工具 `get_plan_history` 从 `daily_runs` 和计划结果里读。

会话按上下文对象划分：从同一只股票或同一个板块打开助手，接着同一个会话，跨天也连续；从页头打开的是不带对象的全局会话。跨交易日后旧对话照常进入滚动摘要，摘要保留日期。

### 4.4 笔记（LLM wiki）

每个范围一页，内容是给人读的 Markdown，例如：

```markdown
# 宁德时代 300750

- 拿着等年报，年报前不因短期波动卖出（2026-11-06）
- 只在回踩 20 日线附近加仓（2026-11-04）
```

表 `assistant_note`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | |
| `scope` | String(16) | `global` / `stock` / `board` |
| `subject` | String(32)，可空 | 股票代码或 `board_catalog.id`；`global` 时为空 |
| `title` | String(64) | 页面标题，例如「偏好」「宁德时代 300750」 |
| `body_md` | Text | 页面正文 |
| `version` | Integer | 每次修改加 1，用于并发保护 |
| `updated_at` | DateTime | |

- 约束：`UNIQUE(scope, subject)`。
- 历史版本存在 `assistant_note_revision`（`note_id`、`version`、`body_md`、`source`、`origin_session_id`、`origin_message_id`、`created_at`），可以查看和回退。
- 待确认的修改存在 `assistant_note_proposal`（基于的版本、修改前后正文、状态），确认时如果页面已被改过则返回冲突，不覆盖。
- 存在数据库里，不写磁盘文件：Web 编辑、备份、Docker 和桌面端走同一条路径；需要时可以导出成 `.md`。

写入流程：

1. 用户说「记住……」，或助手认为某句话值得记（例如用户第二次提到不碰 ST），助手生成对某一页的修改：新增、改写或删除一条。
2. 界面显示修改前后的差异，用户点确认才写入；第一版每天最多主动提议两次。
3. 写入时带上日期。同一页里意思相反的旧条目由助手在同一次修改里改写或删除，不并存。
4. 「整理」：页面过长或有过期条目（例如「这周不加仓」）时，助手提议精简，同样需要用户确认。

读取：每次提问都带上「偏好」页；上下文对象是某只股票或板块时，再带上它的页面。页面总长度设上限，超出时提示用户整理，不截断。

管理：设置里新增「助手笔记」，列出所有页面，可以直接编辑、查看历史版本、删除。原型里还没有这一页。

### 4.5 回忆插件

沿用现有可插拔后端的做法（`src/llm/generation_backend.py` 的 Protocol 加 `backend_registry.py` 的注册表），新增 `src/agent/recall/`：

```python
class RecallProvider(Protocol):
    provider_id: str

    def retain(self, turn: RecallTurn) -> None: ...          # 后台写入一轮对话
    def recall(self, query: RecallQuery) -> list[RecallItem]: ...  # 按问题和上下文对象召回
    def forget(self, document_ids: list[str]) -> None: ...   # 删除会话或消息时同步删除
    def health(self) -> RecallHealth: ...                   # 设置页「数据状态」显示
```

- **默认 `none`**：空实现，什么也不做。
- **`hindsight`**：在 `src/agent/recall/hindsight_provider.py`，只在选中时才导入，Hindsight 客户端不进 `requirements.txt` 的必装依赖，作为可选依赖单独说明。
- 选择方式：`ASSISTANT_RECALL_PROVIDER=none|hindsight`；Hindsight 相关的 `HINDSIGHT_BASE_URL`、`HINDSIGHT_API_KEY`、`HINDSIGHT_BANK_ID`、`ASSISTANT_RECALL_TIMEOUT_S`。不写死服务器地址；这些配置同样登记到 `config_registry.py`，`HINDSIGHT_API_KEY` 标为敏感项，可以在 Web 设置页填写；新增配置时同步更新 `.env.example` 和文档。

调用方式：

| 时机 | 做法 | 失败时 |
| --- | --- | --- |
| 提问时 | `recall`：问题文本 + 上下文对象标签（`stock:300750`、`board:BK1184`）+ 取最近若干条；结果附日期，作为「过往对话」放进上下文 | 超时（默认 2 秒）或报错就跳过，依据行写「未使用长期回忆」 |
| 回答结束后 | `retain`：在后台队列写入本轮问答，文档 ID 用「会话 ID:消息 ID」，带日期、市场阶段、对象标签 | 失败写日志并重试几次，不影响回答 |
| 删除会话或消息时 | `forget` 对应的文档 ID | 记入待删队列，服务恢复后补删 |

Hindsight 的具体约定：

- 用独立的 bank，不和开发用的 Hindsight 记忆混用。
- 写入前把金额、股数、成本打码（例如「持有 [数量] 股，成本 [价格]」），因为持仓事实以账本为准，回忆里不需要这些数字，也避免敏感数据发到远端服务器。打码默认开，可以用配置关闭。
- Hindsight 写入时服务端会调用模型提取事实，这部分费用和延迟在服务端，助手这边是异步的，不影响回答速度。

### 4.6 待定

1. **金额打码（已决定）：** 默认开，可以用配置关闭。
2. **现有 `AgentMemory`：** 助手配置不启用它的置信度校准，保持现状；以前的计划结果用 `get_plan_history` 回答。是否以后把它也改成回忆插件的一个来源，留到第二版再定。

## 5. 隔夜美股、资讯与 8:45 盘前推送（已实现第一版，未经真实接口验证）

### 5.1 已确定

| 事项 | 决定 |
| --- | --- |
| 17:00 运行 | 使用前一晚的美股交易日（北京时间凌晨收盘）：东方财富（AKShare）优先，其次 Tushare（`us_daily` 13:00 已发布，但每分钟只能调 1 次，只作兜底），最后 yfinance |
| 8:45 推送 | 每个交易日 8:45 推送一版自选股分析和其他资讯；需要约 8:15 开始运行；隔夜美股取数顺序为东方财富（AKShare）→ yfinance |
| 美股数据粒度 | 只要日线 |
| 资讯来源 | 以 AKShare 为主（项目已有依赖，无需另外部署）；NewsNow 作为可选备用，不必自建；不购买 Tushare「新闻资讯」权限（账号当前无权限，且按小时更新） |

### 5.2 接口与发布时间（Tushare MCP 工具说明 + 单次采样）

| 数据 | 接口 | 发布时间（北京） | 账号可用 |
| --- | --- | --- | --- |
| 美股日线 | `us_daily` | 交易日 13:00 | 是，但**每分钟只能调用 1 次**（错误 `抱歉，您访问接口(us_daily)频率超限(1次/分钟)`；采样 NVDA 11-06 成功） |
| 国际指数 | `index_global` | 交易日 9:00 | 是（采样 SPX 11-06 成功） |
| 新闻快讯 | `news` | 每小时第 50 分钟 | 否（错误 40203） |

8:45 时 Tushare 还没有前一晚的美股日线。第一版用 yfinance 取数，真实运行时 Yahoo 对每个代码都返回 `YFRateLimitError: Too Many Requests`，一条数据也没取到，所以 8:15 这一步改为**东方财富（AKShare）优先，yfinance 备用**。

东方财富接口（akshare 1.19.3，各采样一次确认）：

| 数据 | AKShare 接口 | 用法 | 备注 |
| --- | --- | --- | --- |
| 美股 / ETF 日线 | `stock_us_hist(symbol="105.NVDA", period="daily", start_date, end_date, adjust="")` | 每个龙头一次，代码为 `<市场>.<代码>` | 列：日期 开盘 收盘 最高 最低 成交量 成交额 振幅 涨跌幅 涨跌额 换手率；NVDA 11-06 收盘 186.64、涨跌幅 2.78，与 Tushare 一致；`105.` = NASDAQ（NVDA、SOXX、PGJ），`106.` = NYSE（TSM）；`107.PGJ` 取不到（PGJ 在 NASDAQ） |
| 国际指数快照 | `index_global_spot_em()` | 每次运行调用一次，取 `SPX` 标普500、`NDX` 纳斯达克100、`DJIA` 道琼斯 | 56 行；列含 代码 最新价 涨跌幅 开盘价 最高价 最低价 昨收价 最新行情时间（北京时间，如 `2026-11-07 05:00:00`）；没有费城半导体指数和纳斯达克中国金龙指数 |

指数是快照，不是日线：把 `最新行情时间` 换算成纽约日期作为交易日，生成当日一根日线（涨跌幅取 `涨跌幅`，昨收取 `昨收价`）。以下两种快照不能当作那一次运行要的收盘数据，丢弃：纽约时间早于 13:00 的盘中快照；以及**比运行基准时间 `as_of` 更新的快照**。后者出现在补跑过去的板块日：例如 2026-11-06 的 17:00 流程在周日补跑，`as_of` 是周五 17:00（纽约 04:00，最近已收盘的交易日是周四 11-05），而周日取到的快照已经是周五收盘（`NDX 24579.42 +1.09%`，行情时间 `2026-11-07 05:00:00`），周五的日期晚于 `as_of` 的收盘时间被过滤，结果就是「东方财富无数据」，再落到 yfinance `^NDX` 也失败（真实运行里 NDX 就是这样缺失的；SPX 当时靠 Tushare `index_global` 才有数据）。这种情况改用 `index_global_hist_em(symbol=<快照里的 名称，如 "纳斯达克">)` 取该指数的日线历史（一次调用，返回全部历史，约 8800 行；列 日期 代码 名称 今开 最新价 最高 最低 振幅，没有昨收 / 涨跌幅 / 成交量，昨收和涨跌幅由前一根的收盘价推出，只保留窗口内的日期），再按 `as_of` 过滤出已收盘的交易日（上例得到周四 11-05，收盘 24315.00，相对 11-04 的 25736.51 下跌 5.52%）。盘中快照同样会落到这条历史路径。所有 AKShare 调用都有超时（复用 `call_with_timeout`），连续 3 次失败后本次运行不再请求东方财富，其余龙头直接走 yfinance。

取数顺序：8:15 盘前版 `("eastmoney", "yfinance")`；17:00 板块流程 `("eastmoney", "tushare", "yfinance")`。实现按「来源」逐轮取数：东方财富对每个龙头取一遍，随后只有还没拿到期望交易日的龙头才进入下一个来源。

Tushare 兜底（本账号 `us_daily` 每分钟 1 次）：

- `us_daily` 每次运行**最多调用一次**，按 `trade_date`（期望的美股交易日）取当日全部美股（一次调用返回所有代码，6000 行一页，用现有分页助手按页取全），再从中取出还缺的龙头；不再按代码逐个调用。期望交易日是休市日时当日没有行，该次调用返回空，不会再换日期重试。`index_global` 仍按指数逐个调用（还没遇到频率问题），排在东方财富之后。
- 频率超限（错误文案含 `频率超限`）不做 30 秒退避重试：`us_daily` / `index_global` 遇到即抛 `TushareRateLimitedError`，本次运行不再请求 Tushare，其余龙头直接走 yfinance（之前每个龙头一次 us_daily、每次 30 秒重试，17:00 流程在 Tushare 上耗了 10 多分钟才落到备用来源）。首页之后的翻页被限流时保留已取到的行（`IncompleteMarketDataError.partial`）。其他接口（`daily` 等）仍保留原来的限流退避。
- Tushare 一次只取一个交易日，所以兜底得到的行是单根日线（没有前几日的均线）。

写入 `stock_daily` 的优先级仍为 Tushare > 东方财富 > yfinance：低优先级来源不覆盖高优先级来源的行（东方财富可以覆盖 yfinance；之后如果有一次运行不得不用 Tushare 兜底，它可以覆盖同一交易日的东方财富 / yfinance 行，东方财富不会覆盖 Tushare 行）。同一交易日有多个来源结果时，先比交易日（新者胜），再比这个优先级。

### 5.3 美股龙头与板块映射

- 用户可编辑的观察列表，约 10–15 只，每只映射到若干 A 股板块（`board_catalog`），例如 NVDA、AVGO、AMD、TSM、费城半导体指数 → 算力 / CPO / 半导体；TSLA → 特斯拉概念 / 汽车零部件；AAPL → 消费电子 / 苹果概念；纳斯达克中国金龙指数 → 互联网服务 / 互联网电商。
- 每项可带一个可选的东方财富代码 `em`（个股 / ETF 如 `105.NVDA`、`106.TSM`，指数如 `SPX`、`NDX`）；没有 `em` 的项只走 Tushare / yfinance。
- 东方财富没有费城半导体指数和纳斯达克中国金龙指数，默认名单用 NASDAQ 上市的 ETF 代替：`SOXX`（跟踪费城半导体指数，→ 半导体 / 算力 / CPO）、`PGJ`（跟踪纳斯达克中国金龙指数，→ 互联网服务 / 互联网电商；东方财富板块目录里没有「中概互联」，最接近的真实板块是概念板块 `互联网服务`（BK0447.DC）和行业板块 `互联网电商`（BK1268.DC）），类型为 `etf`，按指数阈值判断异动。大盘背景指数为 `SPX`（标普500）和 `NDX`（纳斯达克100；原先的纳斯达克综合指数 `IXIC` 东方财富快照里没有，已去掉，用户名单里仍可保留，只走 Tushare / yfinance）。
- Tushare 对应关系：`SOXX` / `PGJ` 作为普通美股代码走 `us_daily`（Tushare 另有 `us_etf_daily`，当前没有接入）；东方财富优先，取不到的龙头在 17:00 流程里由 Tushare（一次 `us_daily`）和 yfinance 顺序兜底；`NDX` 不在 Tushare `index_global` 里，由东方财富取数（快照，补跑过去日期时用 `index_global_hist_em`），最后退到 yfinance 的 `^NDX`。
- 日线存入现有 `stock_daily`（已支持美股代码），不新建行情表；映射关系需要一张小表或配置，实现时定。
- 龙头单日涨跌超过阈值（可配置）时，给映射的板块和计划里的相关股票加「隔夜美股」提示。

### 5.4 资讯

- 来源（AKShare 1.19.3 中均已确认存在）：

  | 用途 | AKShare 接口 | 用法 |
  | --- | --- | --- |
  | 全市场突发快讯 | `stock_info_global_cls`（财联社电报，可只取「重点」） | 交易日定时轮询，按发布时间和内容去重写入资讯池 |
  | 快讯备用 | `stock_info_global_em`、`stock_info_global_sina`、`stock_info_global_futu`、`stock_info_global_ths` | 财联社失败时按顺序切换 |
  | 自选股公告 | 巨潮资讯公开公告检索（`src/services/news_cninfo_source.py`，逐只、主来源）；东方财富 `stock_notice_report(symbol, date)` 作备用 | 每天按日期取一次全市场公告，再按自选股和持仓过滤；`stock_individual_notice_report` 用于单只补历史 |
  | 个股新闻 | `stock_news_em` | 已在选股中使用，助手按需调用 |
  | 盘前材料 | `stock_info_cjzc_em`（财经早餐） | 8:45 推送的盘前摘要 |
  | 研报 | `stock_research_report_em` | 可选 |

- **抓取节奏（已决定）：** 快讯接口不能按时间段查询，每次只返回最近一批（财联社 20 条、东方财富 200 条、新浪 20 条、富途 50 条），所以按比一批覆盖时长更短的间隔轮询：
  - 交易日 08:00–15:30：每 10 分钟同时取财联社电报和东方财富快讯，合并去重；东方财富一次 200 条，用来补财联社的空档。新浪、富途、同花顺在其中一个失败时备用。
  - 其他时段和非交易日：每 30 分钟一次。
  - 交易日 08:15 前：自选股公告和财经早餐各一次。
  - 每个来源记录已取到的最新位置；新一批和上次没有重叠时，记为「可能漏抓的时段」并在状态里显示，不静默忽略。
  - 间隔可配置；遵守东方财富、财联社的频控。测试一律 mock。
- **存储（已决定）：** 快讯保留 3 天，公告等公司事件保留 30 天。分析（8:45 推送、17:00 板块判定、助手）一律从存储读取，不在分析时临时搜索；现有搜索接口只在存储为空或过期时作为可选兜底。板块判定的依据里直接保存新闻标题，新闻过期删除后判定依据仍可查看。
- 这些接口都是爬虫，上游改版会失效：每个来源失败时记录并切换备用，不拖垮其他步骤；NewsNow（已有接入代码，`NEWS_INTEL_AUTO_FETCH_ENABLED`、`NEWSNOW_BASE_URL`）保留为可选备用。
- 若后端部署在境外服务器，上线前从该服务器对每个来源各请求一次，确认没有境外访问限制。
- 现有资讯池（`intelligence_items`）改为定时抓取，不再只在分析时顺带抓取。
- 每条资讯用轻量模型分类（新增通用配置 `LITELLM_LIGHT_MODEL`，供分类、打标签、短摘要等杂活共用；留空时用主模型，调用失败时退回主模型一次）：范围（全市场 / 板块 / 个股）、重要程度、关联的 A 股板块、关联的美股龙头、是否突发。分类结果用于板块判定的驱动因素（`driver_kind=news`）、助手上下文、8:45 和 17:00 的推送。
- 自选股的公司事件以公告为主；Tushare 结构化接口（业绩预告 `forecast`、业绩快报 `express`、增减持 `stk_holdertrade`、解禁 `share_float`、回购 `repurchase`）作为补充，用于直接取数字，实现前各采样一次确认权限。

### 5.5 8:45 盘前推送

- 复用现有的每日个股分析和飞书推送，把隔夜美股、龙头映射结果和新资讯作为附加上下文，不另建分析流程。
- 17:00 的计划（价位、条件、股数）保持不变，作为基准；盘前版给出盘前看法。若隔夜变化足以改变结论，标为「盘前建议调整」并写明原因，不覆盖原计划。

### 5.6 待定

1. 盘中突发资讯是否即时推送飞书。核心功能说明目前把「盘中盯盘推送」列为不做的事。
2. 美股观察列表的默认名单和涨跌阈值。

回滚：删除本文并撤销 CHANGELOG 对应条目即可。
