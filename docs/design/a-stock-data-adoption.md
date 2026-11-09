# 外部数据源借鉴方案（a-stock-data）

- 日期：2026-11-09
- 状态：方案稿，未实现（第 4 节「公告」的巨潮主来源已实现并合入 `main`）
- 依据：[simonlin1212/a-stock-data](https://github.com/simonlin1212/a-stock-data) V3.10.4（Apache-2.0，87 个端点，下文「§x.y」指它的 `SKILL.md` 章节）；本仓库现有数据源盘点（第 1.2 节）；2026-11-09 对 Tushare 账号的权限采样（第 1.3 节）

本文回答两个问题：a-stock-data 里哪些东西值得我们用，以及怎么接进现有系统。它是一个给 AI 用的取数手册（代码片段 + 实测踩坑），不是可安装的库，所以我们**只借鉴端点、参数和坑，不引入它的代码作为依赖**；需要的端点按本仓库的模块结构重写，并补测试。

## 0. 结论

| 优先级 | 事项 | 主来源 | a-stock-data 借鉴的部分 | 主要使用方 |
| --- | --- | --- | --- | --- |
| P0 | 东方财富统一限速和封禁冷却 | — | §东财防封、`em_get()` 的限速 / 403 不重试 / 子域名分开风控 | 所有东方财富调用 |
| P0 | 公司公告 | 巨潮公开检索（已实现） | §7.1 orgId 映射表、§备用源里的深交所官方公告 | 资讯库、选股、助手 |
| P1 | 公司事件（业绩预告、增减持、回购、解禁、质押、机构调研） | Tushare（已开通） | §14.x 东方财富数据中心作备用，字段和坑 | 选股「事件」、次日计划风险、助手 |
| P1 | 投资者问答（互动易、上证 e 互动） | §10.1、§10.3 直连 | 两个平台的请求方式和坑 | 资讯库、选股、助手 |
| P1 | 券商评级和盈利预测 | Tushare `report_rc`（已开通） | §2.4 新浪研报列表作标题补充 | 助手、选股、估值 |
| P1 | 打板和情绪：开盘啦榜单、热榜、风险警示和异常波动名单 | Tushare `kpl_list` / `ths_hot` / `dc_hot`；§8.4 / §8.5 直连 | §8.4 / §8.5 东方财富独有名单 | 板块异动、选股风控、次日计划 |
| P1 | 资金面：龙虎榜席位、两融、北向 | Tushare `top_list` / `margin_detail` / `moneyflow_hsgt` | 交易所官方龙虎榜、新浪资金流作备用 | 助手、选股 |
| P2 | 快讯备用源 | 财联社直连（§5.2 本地签名）、华尔街见闻（§5.4）、Tushare `major_news` | 签名算法、频道参数 | 资讯库 |
| P2 | 板块流程的第二行情来源 | 通达信官网盘后包（§1.3） | 文件格式、发布时间、北交所起始日 | 板块异动流程 |
| P2 | 日 K 复权口径统一 | Tushare `adj_factor`（已在选股使用） | §1.2 前复权差值法可能出现负价的坑 | 分析、助手、次日计划 |
| P3 | 宏观、指数成分 / 权重 / 估值、官方交易日历 | §11.x、§12.x 官方站点 | 各站点的格式和坑 | 大盘复盘、助手 |
| P3 | 估值小工具（前向 PE、PEG） | 本地计算（`report_rc` EPS + 行情） | 公式 | 助手 |

不采纳的见第 12 节。分期安排见第 13 节。

## 1. 背景

### 1.1 为什么要补

现在的数据有三个问题：

1. **缺**：公司事件（增减持、回购、解禁、质押、机构调研）、投资者问答、研报评级、龙虎榜席位、两融、北向、宏观、指数成分都没有接（盘点见 1.2）。选股的「事件」只能靠网页搜索，常搜到行情首页；助手被问到这些只能说查不到。
2. **单一来源**：板块流程、次日计划和形态扫描只靠 Tushare 一个 token；历史分时只靠必盈；快讯、个股资金流、龙虎榜、公告只靠 AkShare 抓东方财富。
3. **东方财富没有统一限速**：各模块各自限速（AkShare 抓取 2–5 秒、选股 1 秒 + 抖动、资讯库每源一个在途请求），合起来可能超过东方财富的风控线；AkShare 走的东方财富接口完全不经过我们的限速。

### 1.2 现有数据源盘点（摘要）

| 能力 | 现有来源 | 问题 |
| --- | --- | --- |
| 实时行情 | Tushare `rt_k` → 必盈 → 腾讯 → 新浪 → efinance → AkShare（`data_provider/base.py` `get_realtime_quote`） | 稳定；腾讯字段下标与 a-stock-data 修正后的一致（43 振幅、44 流通市值、45 总市值、46 市净率，`akshare_fetcher.py:1486-1495`） |
| 日 K | Tushare → efinance → AkShare → … | Tushare 返回不复权，后备来源返回前复权，同一只股票换源后口径不一致 |
| 5 分钟线 | Tushare `rt_min_daily`、必盈；历史日只有必盈 | 无 1 分钟、无逐笔 |
| 板块 | Tushare `dc_index` / `dc_daily` / `dc_member`、`limit_list_d`、`stk_limit` | 只靠 Tushare |
| 资金流 | AkShare `stock_individual_fund_flow`（只有当日主力），Tushare `moneyflow` 补；板块 AkShare | 无北向、两融、大宗 |
| 龙虎榜 | AkShare，只返回是否上榜、次数 | 无席位明细 |
| 打板 / 热榜 | 板块流程自算；AkShare 热榜和涨停池写了但没有调用方 | 无风险警示名单、无异常波动名单 |
| 快讯 | AkShare 财联社、东方财富（新浪 / 富途 / 同花顺备用） | 全是抓取 |
| 公告 | 巨潮公开检索，东方财富按代码备用（已合入） | 只覆盖自选和持仓 |
| 公司事件 | 业绩预告、分红（AkShare 主、Tushare 备用） | 增减持、回购、解禁、质押、调研都没有 |
| 问答 / 研报 | 无 | — |
| 宏观 / 指数 | 只有 FXMacroData（美元口径） | 无国内宏观、指数成分 |

另外盘点发现几处代码问题，和本方案无关，单独记录：`tushare_rate_limit_per_minute`、`akshare_sleep_*` 配置项没有被读取；随机 User-Agent 函数实际什么也没设；Tushare 限速按实例计算，几处直接用裸客户端，合起来可能超过账号限额。

### 1.3 Tushare 权限实测（2026-11-09，每个接口一次采样）

| 结果 | 接口 |
| --- | --- |
| 可用 | `forecast`、`stk_holdertrade`、`repurchase`、`share_float`、`pledge_stat`、`stk_surv`、`report_rc`、`top_list`、`ths_hot`、`dc_hot`、`kpl_list`、`major_news`、`cyq_chips`、`moneyflow_hsgt`、`margin_detail` |
| 无权限（40203，需单独购买） | `anns_d`（公告）、`irm_qa_sh` / `irm_qa_sz`（互动问答）、`research_report`（研报全文）、`news`（短讯）、`stk_auction`（竞价） |

所以：能用 Tushare 的一律用 Tushare（有限速、有字段文档、不会被封）；Tushare 没开通的（公告、问答、研报标题、快讯备用）才用 a-stock-data 里的直连端点。

### 1.4 原则

1. **来源优先级**：已开通的 Tushare > 交易所 / 巨潮 / 官方机构 > 新浪、同花顺、华尔街见闻 > 东方财富。东方财富只用于别处拿不到的数据（a-stock-data 同样的结论，见 §数据源优先级）。
2. **每个新来源一个模块**，按 `src/services/news_akshare_source.py` / `news_cninfo_source.py` 的写法：只负责请求和解析，返回统一结构；超时用 `call_with_timeout`；存储、调度、去重在服务层。
3. **区分「没有数据」和「来源坏了」**：没有数据返回空；接口改版、字段缺失、被封要抛错并记到来源状态里，数据状态面板能看到。不用 `return []` 掩盖失败（a-stock-data 的 V3.9 约定，也是本仓库 AGENTS.md 的要求）。
4. **测试**：每个接口最多一次真实采样，存成脱敏 fixture，之后全部用 fixture；在 HTTP 层打桩，解析、过滤、存储走真实代码（AGENTS.md 6.1）。
5. **暴露方式**：新数据先做成服务和 `/api/v1` 接口。助手已能通过接口文档调用全部接口，所以大多数数据**不需要再写助手工具**，接口和字段说明写清楚即可；选股、次日计划直接调服务。

## 2. 基础设施

### 2.1 东方财富统一限速和封禁冷却（P0）

**问题**：东方财富的风控线（a-stock-data 社区实测，2026-05）是每秒超过 5 次、同一 IP 10 个以上并发、每分钟 200 次或 5 分钟 300 次就会临时封 IP；不同子域名（`push2`、`push2his`、`datacenter-web`）风控分开，一个被封另一个可能还能用。我们的东方财富调用分散在 AkShare、efinance、选股、热点、资讯库几处，各自限速，没有全局约束。

**方案**：新建 `data_provider/eastmoney_gate.py`，所有东方财富请求先过这一道。

- 按子域名分组（`push2`、`push2his`、`push2ex`、`datacenter`、`np-anotice`、`np-weblist`、`search-api` 等），每组一个串行闸门：同一时刻只放行一个请求，两次请求间隔不少于 `EASTMONEY_MIN_INTERVAL_SEC`（默认 1.0 秒）加 0.1–0.5 秒随机抖动。
- 403 视为风控信号：**不重试**，该子域名进入冷却（默认 30 分钟，期间直接抛 `SourceCoolingDown`，让调用方换源）；429 / 5xx 指数退避重试最多 3 次。
- 直接发 HTTP 的地方（选股快照 `screening/snapshot.py`、热点 `screening_service.py` 的 `push2` 调用、以后新接的 §8.4 / §8.5）改用闸门提供的 `session.get`；AkShare / efinance 的东方财富函数在调用前 `with gate.slot("push2"):` 占位，做不到逐请求限速，但能保证同一时刻只有一个东方财富批量函数在跑。
- 冷却状态写进现有的数据状态（`data_capability_service`）里，面板显示「东方财富 push2 冷却中，剩余 N 分钟」。
- 现有的 `SCREENING_EASTMONEY_MIN_INTERVAL_SEC` / `SCREENING_EASTMONEY_JITTER_SEC` 保留，作为选股快照的覆盖值；新增 `EASTMONEY_MIN_INTERVAL_SEC`、`EASTMONEY_BAN_COOLDOWN_MINUTES` 两个配置项（同步 `.env.example` 和文档），并挂到数据状态里「东方财富」一行的 `config_keys` 上，在设置 →「数据状态」里可以直接修改。

**验收**：并发起 10 个东方财富请求，实际是串行、间隔不少于 1 秒；模拟 403 后同一子域名立即冷却、另一个子域名照常；冷却期间调用方拿到明确的异常并切到下一个来源。

### 2.2 新来源的统一写法

每个新来源一个模块，放在 `src/services/sources/`（新建目录；现有 `news_*_source.py` 保持原位，不搬）：

```text
src/services/sources/
  cninfo_irm.py         互动易
  sse_e_interaction.py  上证 e 互动
  szse_disclosure.py    深交所公告（公告第三来源）
  em_risk_lists.py      东方财富风险警示 / 异常波动名单
  cls_roll.py           财联社直连
  wallstreetcn.py       华尔街见闻快讯
  tdx_daily_package.py  通达信盘后包
  official_macro.py     人民银行 / 统计局 / 中债 / 货币网
  index_official.py     中证 / 国证指数成分、权重、估值
```

约定：模块级函数，入参是代码和日期，出参是 dataclass 列表；请求统一带超时；`raise SourceError` 表示来源坏了，空列表表示确实没有；每个模块配一个 `tests/fixtures/<source>/` 和一个测试文件。

### 2.3 代码规范化补漏（P1，小改）

a-stock-data 记录的两个会「静默拿到另一只股票」的坑，需要在 `data_provider/base.py` 的 `normalize_stock_code` / `src/services/stock_list_parser.py` 里核对并补测试：

1. **北交所旧代码**：43 / 83 / 87 开头的旧代码已基本换成 920 开头（2026-07-31 实测 342 只里 336 只），旧代码在腾讯返回「僵尸行情」（成交额 0、价格停在旧值）。需要：识别旧代码并映射到新代码（东方财富 `fs=m:0+t:81+s:2048` 列表可得新旧对照，一天取一次）；无法映射时明确报错。
2. **`000001` 歧义**：个股接口传 `sh000001` 会被当成平安银行。个股类接口（研报、事件、问答）只接受个股代码，传指数代码直接报错。

另外给实时行情加「僵尸报价」标记：成交额为 0、价格等于昨收且价格大于 0 时 `is_stale=True`（§1.1），助手和次日计划看到这个标记就不把它当成当前价。

## 3. 公司事件（P1）

### 3.1 来源

| 事件 | 主来源（Tushare，已开通） | 备用（a-stock-data，东方财富数据中心） |
| --- | --- | --- |
| 业绩预告 | `forecast`（已作基本面备用，改为主来源） | §14.1 `RPT_PUBLIC_OP_NEWPREDICT` |
| 业绩快报 | `express`（未采样，接入前采样一次） | AkShare `stock_yjkb_em`（现有） |
| 股东增减持 | `stk_holdertrade` | §14.3 `RPT_SHARE_HOLDER_INCREASE` |
| 回购 | `repurchase` | §14.4 `RPTA_WEB_GETHGLIST_NEW` |
| 限售解禁 | `share_float`（含未来解禁日） | §3.6 `RPT_LIFT_STAGE` |
| 股权质押 | `pledge_stat`（周度） | §14.5 `RPT_CSDC_LIST`（仅沪深） |
| 机构调研 | `stk_surv` | §14.2 `RPT_ORG_SURVEYNEW` |
| 分红 | `dividend`（现有） | AkShare（现有） |

备用来源只在 Tushare 报错（非「没有数据」）时调用，并经过 2.1 的东方财富闸门。a-stock-data 记录的数据中心坑要照做：排序字段必须能唯一确定一行（只按比例排序会漏行）、`sortTypes` 个数要和排序字段一致（否则返回 9501）、`code=9201` 表示无数据。

### 3.2 存储

新表 `stock_event`：

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `code` | 6 位代码 |
| `kind` | `forecast` / `express` / `holder_trade` / `repurchase` / `share_float` / `pledge` / `survey` / `dividend` |
| `event_date` | 公告日（解禁用解禁日，质押用统计截止日） |
| `title` | 一句话中文摘要，由解析代码生成，例如「高管减持 15 万股，均价 21.83 元」 |
| `payload` | 原始字段（JSON） |
| `source` | `tushare` / `eastmoney` |
| `fetched_at` | 取数时间 |

唯一约束 `(code, kind, event_date, payload_hash)`。保留期沿用公司类资讯的 `NEWS_COMPANY_RETENTION_DAYS`，未来日期的解禁不受保留期影响。

### 3.3 取数时机

- **每日批量**：并入现有的每日资讯任务（`news_pool_service.run_daily_fetch`），对自选和持仓按代码取最近 `_NOTICE_MAX_LOOKBACK_DAYS` 天；解禁额外取未来 30 天。Tushare 这几个接口都支持按日期取全市场，自选加持仓超过 12 只时改为按公告日取全市场再过滤，和公告的 `market` 模式一致。
- **按需**：选股候选、助手问到的股票，先查表，没有或超过 1 天再按代码取一次，写回表里。

### 3.4 使用方

- **选股**：`screening_service._build_dsa_candidate_context` 的「事件」先读 `stock_event`，再读资讯库公告，最后才用网页搜索（现在只有网页搜索）。解禁（未来 30 天占流通股 1% 以上）、大股东减持、质押比例超过 50% 作为确定性风险项，不交给模型判断。
- **次日计划**：持仓股未来 5 个交易日内有解禁、或近 5 天有大股东减持公告时，在计划的风险里写明。
- **接口**：`GET /api/v1/stocks/{code}/events?kinds=&days=`，返回事件列表和来源状态；助手通过接口文档调用。

## 4. 公告（P0，主体已实现）

- **已实现**（已合入 `main`）：巨潮公开检索为主来源，orgId 用官方映射表（6263 只里 4641 只不符合 `gssh0代码` 规则），东方财富按代码备用，两个来源分别记状态。
- **补第三来源**：深交所官方公告 `POST https://www.szse.cn/api/disc/announcement/annList`（JSON 体 `{"channelCode":["listedNotice_disc"],"pageSize":N,"pageNum":1,"stock":[code]}`，PDF 前缀 `https://disc.static.szse.cn/download`），只覆盖 0 / 3 开头的深市股票，用在巨潮和东方财富都失败时。上交所没有可用的官方公告接口（a-stock-data 也没有），沪市只能靠巨潮和东方财富。
- **覆盖选股候选**：选股合并后，候选股的公告在资讯库里没有时按需查巨潮一次（候选上限沿用 `DSA_PRE_RANK_CONTEXT_MAX_CANDIDATES`），写回资讯库。
- **PDF**：只存链接，不下载；需要全文时由助手按需打开链接（以后如果要做公告摘要，再单独设计 PDF 解析）。

## 5. 投资者问答（P1）

Tushare 的 `irm_qa_sh` / `irm_qa_sz` 没有权限，用两个官方平台直连：

| 平台 | 覆盖 | 请求（a-stock-data §10.1 / §10.3） | 坑 |
| --- | --- | --- | --- |
| 巨潮互动易 | 深市 | ① `POST https://irm.cninfo.com.cn/newircs/index/queryKeyboardInfo`（表单 `keyWord=代码`）取 `secid`；② `POST https://irm.cninfo.com.cn/newircs/company/question`，参数放在 query string、请求体为空（否则 400） | 沪市公司返回 0 条；失败和「没有问答」不要混为一谈 |
| 上证 e 互动 | 沪市 | 单家公司：`POST https://sns.sseinfo.com/ajax/userfeeds.do`（表单 `typeCode=company,type,pageSize,uid,page`）；uid 来自 `POST /allcompany.do` 的公司列表（翻页后缓存）；全市场：`GET /ajax/feeds.do?type=11` | 抓 HTML；只有近 1 个月；1 天内的回复时间是「昨天 18:22」这种相对时间，要换算 |

接入方式：

- 存进资讯库，`item_kind="qa"`、`scope_type="symbol"`，标题用问题前 60 字，摘要是「问：……答：……」，链接指向原问答页；保留期沿用公司类资讯。只存有回复的问答（未回复的价值低、量大）。
- 每日任务对自选和持仓各取一次；选股候选、助手问到时按需取。
- 北交所两个平台都不覆盖，接口返回空并在 `coverage` 里写明。
- 新闻分类器（`news_classifier`）对问答同样打板块和重要程度标签，「公司确认 / 否认某项业务」这类回复会成为选股和板块异动的佐证。

## 6. 券商评级和盈利预测（P1）

- **主来源**：Tushare `report_rc`（已开通，采样返回了 2026-10-30 两家券商对特变电工的评级、目标价和 2026–2028 年 EPS 预测）。
- **实现（第 4 期）**：没有新建 `broker_forecast` 表，而是作为公司事件的一类（`stock_event.kind = research`，一份报告一条，各预测年度放在 `payload.forecasts`）：`report_rc` 可以按报告日取全市场，和其他事件一样随每日任务入库（第一次补 90 天），保留期、按需补取单只股票、选股读取都复用第 3 节的逻辑。
- **一致预期**：本地按 `quarter` 汇总近 90 天各机构的 EPS 均值、最高、最低、机构数，代替 a-stock-data 的同花顺一致预期网页（§2.2，抓 HTML、GBK）。
- **研报标题补充（暂缓）**：`report_rc` 只有点评类研报的预测，深度报告标题可从新浪研报列表补（§2.4，`vReport_List`）。第 4 期没有接：新浪要求请求间隔 6 秒以上，选股候选多时太慢，而 `report_rc` 已覆盖评级和预测；等助手确有需要时再按需接入。坑：间隔 1 秒时第二次请求就返回 HTTP 200 的假「没有找到相关内容」页，必须间隔 6 秒以上；北交所要 `bj` 前缀。东方财富研报接口（§2.1）有评级和 PDF，但在东方财富风控范围内，作为可选。
- **接口**：`GET /api/v1/stocks/{code}/research?days=90`，返回评级分布、一致预期和最近研报标题。

## 7. 快讯与新闻（P2）

现在的快讯全部经 AkShare 抓财联社和东方财富，AkShare 或上游改版就一起断。补三个互相独立的来源：

| 来源 | 请求 | 用途 |
| --- | --- | --- |
| 财联社直连（§5.2） | `GET https://www.cls.cn/v1/roll/get_roll_list`，参数 `appName=CailianpressWeb, os=web, sv=7.7.5, last_time, refresh_type=1, rn=N`；签名 `sign = md5(sha1(按键名排序拼接的 query 串))`，不需要 key | AkShare 财联社失败时的直连备用；也可用 `last_time` 翻页补缺口 |
| 华尔街见闻（§5.4） | `GET https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=a-stock-channel&limit=100&cursor=` | 新的快讯来源，和东方财富不在同一风控面；`score=2` 是重要快讯 |
| Tushare `major_news`（已开通） | 按时间段取长篇新闻（来源含财联社、华尔街见闻、同花顺、新浪等） | 长篇新闻，给助手和板块判定作背景；不进 3 天快讯池，单独保留 7 天 |

**第 5 期实现情况**：三者都已接入。与原计划的差别：`major_news` 只取标题和链接，按公告类的保留期（`NEWS_COMPANY_RETENTION_DAYS`）清理，没有单独设 7 天；华尔街见闻排在备用快讯顺序最后，不改变新浪 / 富途 / 同花顺原有的顺序。

接入资讯库：财联社直连作为 `cls` 来源的第二实现（同一个 `source_key`，先 AkShare 后直连，记录用的是哪个）；华尔街见闻作为新的 `wscn` 来源加入备用快讯顺序；`major_news` 作为新 `item_kind="article"`。三者都走现有去重（同源 URL、跨源近似标题）。

## 8. 打板和情绪（P1）

| 数据 | 来源 | 接入 |
| --- | --- | --- |
| 开盘啦涨停 / 炸板 / 跌停榜，题材和连板状态 | Tushare `kpl_list`（`theme`、`status` 如「6 天 5 板」） | 板块流程每日写一份；涨停原因题材补进板块判定的依据 |
| 同花顺 / 东方财富热榜 | Tushare `ths_hot`（含 AI 生成的 `rank_reason`）、`dc_hot` | 每日收盘后存一份；助手回答「为什么涨」时作参考，标注「同花顺 AI 摘要」 |
| 风险警示名单（重点监控池） | §8.4 `GET https://mobappconfig.securities.eastmoney.com/emcfg/stock_monitor.json`（一个文件、全量） | 每日一次；在名单内（约 14 天窗口）的股票选股直接排除，次日计划标风险 |
| 严重异常波动明细 | §8.5 `GET https://dycalchis.eastmoney.com/price-anomaly/list`，必须带固定参数 `team=h5, product=EastMoney, client=WAP, version=9001, name=WAP, user=123` | 每日一次；作为选股和次日计划的风险项 |
| 连板梯队、晋级率、炸板率 | 本地计算：`limit_list_d` + `kpl_list` 的连板状态 | 写进 `market_daily`，板块页和盘前推送显示 |

同花顺 `getharden` 涨停原因（§3.1）和东方财富涨停四池（§8.1）不接，Tushare 已覆盖；只在 Tushare 打板数据缺失时作为应急，暂不实现。

**第 5 期实现情况**：`kpl_list`、`ths_hot` + `dc_hot` 存为公司事件 `limit_board`、`hot_rank`（每日按交易日入库，`/stocks/{code}/events` 和选股候选的「事件」可读）；两个东方财富风险名单存为 `risk_watch`、`price_anomaly`，重点监控名单内的股票选股直接排除，两者都进次日计划和选股的风险。连板梯队和晋级率在板块指标计算时用 `market_stock_daily` 的 `consecutive_up` 本地算（不需要新请求），写进 `market_daily` 新列 `max_consecutive`、`promotion_rate`、`ladder`，通过板块页接口的 `market` 返回（`break_rate_pct`、`max_consecutive`、`promotion_rate_pct`、`ladder`）。还没做：涨停原因题材进入板块判定的依据、板块页和盘前推送的界面展示（接口字段已就绪）。

## 9. 资金面（P1）

| 数据 | 主来源 | 备用 | 接入 |
| --- | --- | --- | --- |
| 龙虎榜明细 | Tushare `top_list`（已开通）；席位 `top_inst`（未采样，接入前采样一次） | 深交所 / 上交所官方龙虎榜（备用源表里的 `dragon_tiger_backup`；上交所返回全文，需自行解析） | 替换现在只有「是否上榜」的 AkShare 实现；`get_capital_flow` 和选股上下文带出机构净买入 |
| 两融 | Tushare `margin_detail` | 交易所官方（`margin_trading_backup`，上交所融券额可能为空，北交所没有） | 个股近 20 日融资余额变化，进入助手和选股上下文 |
| 北向 | Tushare `moneyflow_hsgt`（日度汇总） | 港交所每日统计（§备用源） | 大盘复盘和盘前推送；个股北向持股自 2024-08 起已不按日披露，不做 |
| 个股资金流 | 现有（AkShare，Tushare `moneyflow` 补） | 新浪 `MoneyFlow.ssl_qsfx_zjlrqs`（§备用源 `fund_flow_backup`） | 东方财富冷却时用新浪，补 5 日 / 10 日净额 |

**第 4 期实现情况**：龙虎榜存为公司事件 `dragon_tiger`（每日按交易日 `top_list` + `top_inst` 两次请求，一只股票一天一条，主原因取第一个单日原因，席位按名称 + 买卖额去重），选股候选的「事件」会带出机构和沪深股通净额；分析流程里原有的 AkShare「是否上榜」块没有改。两融、北向不入库，只在 `GET /api/v1/stocks/{code}/capital` 时按需读取，还没有进入选股上下文和大盘复盘（每个候选一次请求，留待需要时再接）。新浪资金流已接为补充链最后一个备用源，并在数据状态里有一行。交易所官方龙虎榜 / 两融备用源没有接。

## 10. 行情与 K 线稳健性（P2）

1. **板块流程的第二来源：通达信盘后包**（§1.3）。`GET https://www.tdx.com.cn/products/data/data/g4day/{YYYYMMDD}.zip`（约 2.7 MB，一次拿到沪深北当日全部证券日线，含成交额）。Tushare 当日数据取不到时，用它填 `market_stock_daily` 的价格、成交量和成交额（换手率、流通市值仍需上一日数据推算，标记来源为 `tdx`）。坑：收盘后几小时才发布，未发布返回 404；北交所从 2022-05-06 起才有。
2. **日 K 复权口径统一**。现在 Tushare 返回不复权、后备来源返回前复权，同一只股票换源后均线会跳。方案：主链路统一为前复权，Tushare 日线用已有的 `adj_factor`（选股 `screening/daily.py` 已实现，抽成公共函数复用）。注意 a-stock-data 记录的坑：腾讯前复权是「减差值」法，很老的价格会变成负数（茅台 2015 年约 −117.6），所以长区间回看用不复权加复权因子自己算，并对价格小于等于 0 报错。
3. **选股日线的腾讯多入口轮换**（§1.2）：腾讯 K 线有三个入口（`web.ifzq.gtimg.cn`、`proxy.finance.qq.com/ifzqgtimg`、`ifzq.gtimg.cn`），各自限流，单入口约 600 次后开始返回空 JSON。`screening/daily.py` 的腾讯直连改为空结果时冷却该入口 120 秒、换下一个。
4. **不做**：逐笔成交（§1.4）和 1 分钟线。当前用户以日线波段为主，没有使用场景。

**第 6 期实现情况**：

- 第 3 条（腾讯多入口轮换）已完成。
- 第 1 条（通达信盘后包）**暂缓**：2026-11-09 采样的 `g4day/20261106.zip` 里是 `sh/sz/bj*.md1` 与 `*.cod` 文件（上海一份约 14 MB），不是文档里说的 `.day` 日线格式，格式没有公开说明。在没有参照解析结果可核对时写解析器，价格解析错会悄悄写进板块指标，风险大于收益；等找到可靠的格式说明或参考实现再接。
- 第 2 条（复权口径统一）**暂缓**：日线历史是逐日累积存进 `stock_daily` 的（每次只刷新约最近 30 天），而前复权价格每次除权后都会整体变化，所以把 Tushare 改成前复权会让库里旧的不复权行和新的前复权行在接缝处跳变，比现在更糟。正确的做法是库里统一存不复权价格加 `adj_factor`、读取时再复权，这要改存储和所有读取方，超出本方案范围，单独立项。选股 `screening/daily.py` 的日线不入库，已经统一为前复权，不受影响。

## 11. 宏观、指数、日历（P3）

| 数据 | 来源（a-stock-data） | 用途 |
| --- | --- | --- |
| 制造业 / 非制造业 PMI | §11.2 国家统计局网页（只有最新一个月） | 大盘复盘、助手 |
| 社会融资规模 | §11.1 人民银行统计数据 xls（2021 年起） | 同上 |
| 国债和信用债收益率曲线 | §11.3 中债（单次查询超过 1 年会静默返回 0 行，要按 360 天切片） | 同上 |
| LPR、回购定盘利率 | §11.5 东方财富 `RPTA_WEB_RATE`；§11.4 货币网 csv | 同上 |
| 指数成分和权重 | §12.1 / §12.2 中证、国证官方 xls（只有当前快照，没有历史） | 助手、选股范围（如「只看中证 1000 成分」） |
| 指数估值（PE、股息率） | §12.3 中证官方 xls（没有 PB） | 大盘复盘 |
| 交易日历 | §12.4 深交所 `monthList` | 和现有 `exchange_calendars`、Tushare `trade_cal` 交叉核对，发现节假日调整 |

这一组每天或每月取一次，结果存表，助手通过接口读取。Tushare 也有 `cn_pmi`、`cn_m`、`shibor_lpr`、`index_weight`，接入前各采样一次，能用就优先用 Tushare。

**第 6 期实现情况**：`cn_pmi`、`cn_m`、`sf_month`、`shibor_lpr`、`index_dailybasic`、`index_weight` 都已采样确认可用，全部改用 Tushare，存进 `macro_series` 表，每天随资讯任务刷新，接口 `GET /api/v1/data/macro`、`GET /api/v1/data/indices/{code}/constituents`。没有接的：中债收益率曲线、回购定盘利率（Tushare 没有对应接口，官方来源需要按 360 天切片抓取，等有使用场景再接）、交易日历交叉核对；指数成分还没有接进选股范围（如「只看中证 1000 成分」），大盘复盘也还没读宏观数据。

## 12. 不采纳

| 内容 | 原因 |
| --- | --- |
| 把 a-stock-data 作为依赖安装 | 它是取数手册，不是维护中的库；我们只借鉴端点 |
| iwencai 语义搜索（§2.3） | 需要单独申请 key |
| mootdx / 通达信 TCP（§1.7） | 2026-09 起 K 线、盘口、逐笔服务器返回空，库已停更 |
| 期权、期货、可转债（§9、§13、§15） | 不在当前产品范围 |
| 新闻联播（§5.5） | 与选股和持仓决策关系弱 |
| 同花顺 `search.10jqka.com.cn` | 需要 `hexin-v` 签名 cookie |
| 东方财富个股新闻搜索（§5.1） | 部分住宅 IP 只返回空结果；我们已有网页搜索 + 资讯库 |
| 东方财富涨停四池、同花顺涨停池（§8.1 / §8.2） | Tushare `limit_list_d` / `kpl_list` 已覆盖 |
| 本地筹码分布算法（§4.6） | 已用 Tushare `cyq_chips`；仅在 Tushare 失效时考虑 |

a-stock-data 自身文档有几处前后矛盾，采纳时以实测为准：东方财富数据中心在一张表里标「低风险」、在防封章节里标「会封」；腾讯 K 线单入口上限一处写约 600 次、一处写 5000 次以上；巨潮公告的路由表写「PDF 下载」，代码只拼了详情页链接。

## 13. 分期实施

每期一个分支、一个 PR，从最新 `main` 拉出；每期独立可回滚（删除新模块和路由注册即可，新表留着不影响旧功能）。

| 期 | 内容 | 涉及 | 验证 |
| --- | --- | --- | --- |
| 1 | 东方财富统一闸门（2.1）；代码规范化补漏和僵尸报价标记（2.3） | `data_provider/eastmoney_gate.py`、选股快照和热点、`normalize_stock_code`、`.env.example` | 闸门的并发 / 403 冷却测试；北交所旧代码、`sh000001` 歧义测试；选股回归 |
| 2 | 公司事件（第 3 节）：`stock_event` 表、Tushare 主来源、每日任务、`/stocks/{code}/events`、选股和次日计划读取 | `src/services/stock_event_service.py`、`news_pool_service`、`screening_service`、`daily_plan_engine` | 每个 Tushare 接口一次采样存 fixture；选股「事件」不再依赖网页搜索的回归 |
| 3 | 投资者问答（第 5 节）、深交所公告第三来源（第 4 节） | `src/services/sources/cninfo_irm.py`、`sse_e_interaction.py`、`szse_disclosure.py` | 三个接口各一次采样；沪深分流、相对时间换算、失败和空结果区分 |
| 4（已完成） | 券商评级和一致预期（第 6 节）、资金面（第 9 节） | `stock_event` 新增 `research` / `dragon_tiger`、`/stocks/{code}/research`、`/stocks/{code}/capital`、`capital_service`、新浪资金流备用 | `top_list` / `top_inst` / `report_rc` / `margin_detail` / `moneyflow_hsgt` / 新浪各一次采样；`get_capital_flow` 回归 |
| 5（已完成） | 打板和情绪（第 8 节）、快讯备用源（第 7 节） | `kpl_list` / 热榜入库、风险名单、财联社直连、华尔街见闻、`major_news` | 签名算法单测；风险名单排除选股的回归 |
| 6（部分完成，见第 10、11 节） | 行情稳健性（第 10 节）、宏观和指数（第 11 节） | 通达信盘后包、复权统一、腾讯多入口、官方宏观 / 指数 | 盘后包解析用一次采样的 zip；复权前后均线一致性测试 |

每期都要：新配置项同步 `.env.example` 和文档，并登记到数据状态对应来源的 `config_keys`（设置 →「数据状态」可直接配置）；新来源在 `data_capability_service` 里有一行状态；`docs/data-sources.md` 加来源说明；`docs/CHANGELOG.md` 加一行。

## 14. 风险与待定

1. **抓取类来源会变**：问答、风险名单、财联社直连、华尔街见闻都是网页接口，不保证稳定。每个来源都要有状态记录和失败告警，失败只影响该来源。
2. **请求量**：新增的每日请求主要是 Tushare（约 10 次）和问答（自选加持仓每只 2–3 次）。东方财富新增调用只有风险名单（每日 2 次），都在闸门内。
3. **待定**：
   - `top_inst`、`express`、`cn_pmi`、`index_weight` 等未采样接口的权限，接入前确认；
   - `stock_event` 是否需要给 Web 做单独页面，还是只在个股计划页和助手里显示；
   - `major_news` 长篇新闻的保留期（暂定 7 天）和是否参与板块判定；
   - 研报标题是否值得为新浪的 6 秒限速付出时间（选股候选多时会慢），可能只在助手按需查询时用。
