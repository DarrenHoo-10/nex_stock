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
- 刷新：
  - 每天刷新当天进入判定候选的板块（排名前后各 10%、新出现的板块，约几十个）。
  - 每周全量刷新一次。全量按板块分批，每个板块一次请求覆盖上次刷新以来的全部日期，限速、带超时。
  - 漏刷的日期以后可以按日期补齐，拉链表随之修正。
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
| `turnover_rate` | Float，可空 | `daily_basic` | % |
| `float_mv` | Float，可空 | `daily_basic.circ_mv` | 流通市值，元（接口单位为万元，× 10000），用于加权 |
| `limit_state` | String(16) | `daily_basic.limit_status` | `none` / `up` / `one_word_up` / `down` / `one_word_down`，由接口的 0–6 状态码换算 |
| `touched_limit_up` | Boolean | `limit_list_d` | 当日在涨停或炸板榜里；在炸板榜即为炸板 |
| `open_times` | Integer，可空 | `limit_list_d` | 炸板次数 |
| `consecutive_up` | Integer，可空 | `limit_list_d.limit_times` | 连板数 |
| `data_source` | String(32) | | |
| `ingested_at` | DateTime | | |

- 主键 `(trade_date, code)`；索引 `(code, trade_date)`。
- `limit_list_d` 不统计 ST 股，ST 股的涨停相关字段为空；ST 股本来就不参与板块统计，不影响判定。
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

### 2.8 `board_signal`：异动判定结果

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | Integer PK | |
| `run_id` | FK → `daily_runs` | |
| `trade_date` | Date | |
| `board_id` | FK → `board_catalog.id` | |
| `signal_type` | String(16) | `first_launch` / `sustained` / `divergence` / `ebb` / `abnormal_drop` |
| `flags` | Text JSON | 主线、有承接、单股驱动、历史数据不足等标注 |
| `evidence` | Text JSON | 每条条件的实际值、阈值和是否满足，页面上的「依据」直接读这里 |
| `driver` | String(16) | `news` / `capital` / `earnings` / `unknown` |
| `reason` | Text | 一句话原因 |

- 约束：`UNIQUE(run_id, board_id)`。
- 阈值快照存在 `daily_runs` 上（结构见每日运行的设计），同一次运行内的所有判定使用同一组阈值。

### 2.9 每日数据流程

全部按 `trade_date` 查询，一次取全市场。积分要求和单次行数上限摘自 Tushare 文档。当前账号有 15,000 积分，满足下表所有接口的要求；实现前每个接口用一次真实请求确认返回字段和行数。

| 步骤 | Tushare 接口 | 写入 | 请求 | 权限 / 上限 |
| --- | --- | --- | --- | --- |
| 1 | `daily` | `market_stock_daily` 的价格和成交额 | 1 | 15:00–16:00 入库；每次 6,000 行 |
| 2 | `daily_basic` | 换手率、流通市值、`limit_state` | 1 | 2,000 积分；15:00–17:00 更新；每次 6,000 行 |
| 3 | `limit_list_d` | `touched_limit_up`、`open_times`、`consecutive_up` | 1 | 5,000 积分起；每次 2,500 行 |
| 4 | `suspend_d` | 停牌股 | 1 | |
| 5 | `index_daily`（`000985.CSI`） | `market_daily` 的基准；本地数据湖有数据时不请求 | 0–1 | |
| 6 | `dc_index`（行业、概念各一次） | `board_catalog`，发现新板块和下线板块 | 2 | 6,000 积分；每次 5,000 行 |
| 7 | `dc_daily` | `board_daily.vendor_ret`、`vendor_amount` | 1 | 6,000 积分；每次 2,000 行；2020 年起 |
| 8 | `dc_member` | 判定候选板块的当日成分 | 约几十 | 6,000 积分；每周另有一次全量刷新 |
| 9 | 无 | 计算 `board_daily`、`market_daily` 的宽度字段 | 0 | 只读本地表 |
| 10 | 无 | 判定写入 `board_signal` | 0 | |

- `stock_basic`（名称、上市日期、板块）每周刷新一次即可。
- 全市场 A 股约 5,400 只，接近 `daily` 和 `daily_basic` 6,000 行的上限，取数时按行数判断是否需要分页，不能假定一次取全。
- 16:00 时 `daily_basic` 可能还没更新完（文档写 15:00–17:00），缺失的项写进 `quality`，相关条件判为「数据不足」，不当作不满足。这里需要和每日运行一起设计：16:00 先跑，缺数据时是否在 17:00 后补算一次。

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
4. **盘中数据只放内存缓存**，不写入 `stock_daily` 和 `market_stock_daily`，避免把未收盘的 K 线当成收盘数据。收盘数据仍由 16:00 的每日运行写入。

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
- **必盈 API 作为第二实时源，按包年版证书设计**（[文档](https://www.biyingapi.com/doc_hs)）。证书放在请求路径里，实现时新增 `BIYING_LICENCE` 配置，只写在 `.env`，日志里打码。包年版每分钟 3,000 次、每日和总次数不限，不依赖深度行情包：

  | 用途 | 接口 | 更新 | 用法 |
  | --- | --- | --- | --- |
  | 个股报价 | `hsstock/real/time`（券商源） | 实时 | 单只提问的首选 |
  | 个股报价（公开源） | `hsrl/ssjy` | 盘中每分钟 | 券商源失败时用；带换手、量比、流通市值、涨速 |
  | 多只股票 | `hsrl/ssjy_more`，每次最多 20 只 | 实时 | 持仓、自选、候选股一起问时用 |
  | 全市场快照 | `hsrl/real/all`（公开源） | 盘中实时 | 盘中板块强弱、全 A 涨幅；单独限流每分钟约 3 次 |
  | 买卖五档 | `hsstock/real/five` | 实时 | 问到盘口时才取 |
  | 分时 K 线 | `hsstock/latest`，5 分钟 | 实时 | 问到今天走势时才取；1 分钟 K 线要深度行情包，不用 |
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
2. **规则不进记忆。** 「止损改成 8%」这类话属于仓位规则，助手提示去设置里修改（第一版助手不代替用户执行操作）。
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
- 选择方式：`ASSISTANT_RECALL_PROVIDER=none|hindsight`；Hindsight 相关的 `HINDSIGHT_BASE_URL`、`HINDSIGHT_API_KEY`、`HINDSIGHT_BANK_ID`、`ASSISTANT_RECALL_TIMEOUT_S`。不写死服务器地址；新增配置时同步更新 `.env.example` 和文档。

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

回滚：删除本文并撤销 CHANGELOG 对应条目即可。
