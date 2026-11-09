# 选股引擎

DSA 将选股能力作为主项目的一部分维护。实现参考 [AlphaSift](https://github.com/ZhuLinsen/alphasift) 提交 [`9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf`](https://github.com/ZhuLinsen/alphasift/commit/9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf)，并按 Apache License 2.0 修改和分发。衍生文件保留来源头，许可证位于 `src/services/screening/LICENSE`，第三方声明见根目录 `THIRD_PARTY_NOTICES.md`。

## 代码边界

- `src/services/screening/`：快照、日 K、策略加载、过滤、评分、风险、LLM 重排与热点实现。
- `src/services/screening/strategies/`：随 DSA 版本发布的策略 YAML。
- `src/services/screening/pipeline.py`：筛选流程的直接入口。
- `src/services/screening_service.py`：DSA 业务编排，直接调用 pipeline，负责配置、数据源上下文、响应归一化、缓存与错误映射。
- `src/services/screening_setup_scan.py`：DSA 原生的全市场形态预扫描（`setup_scan`，见下文），由 pipeline 调用，不属于 AlphaSift 衍生代码。
- `src/storage.py`：使用 DSA 现有 SQLAlchemy/SQLite 基础设施持久化已完成的选股运行，不另建文件数据库。
- `api/v1/endpoints/screening.py`：`/api/v1/screening` API。
- `apps/dsa-web/src/api/screening.ts` 与 `StockScreeningPage.tsx`：Web 调用与展示。

服务层静态调用 `screening.pipeline`、`screening.strategy` 和 `screening.hotspot`。核心逻辑不通过模块名探测、动态适配器或多套路由分发，因此代码结构、错误边界和打包收集目标均由主项目直接定义。

## 配置

默认关闭：

```dotenv
SCREENING_ENABLED=false
```

Web“基础设置”页展示“选股”开关。导航始终保留“选股”入口；关闭时页面展示开启提示，选股 API 继续拒绝业务请求。页面加载状态时不会显示“未开启”或允许开启操作；状态请求失败时显示“状态未知”和重试入口，不把网络失败当作配置关闭。只有确认已开启且引擎可用后，才允许运行策略和热点任务。打开入口或重试状态不会自动修改配置或提交选股任务。

常用可选项：

```dotenv
SCREENING_DATA_DIR=data/screening
SCREENING_SNAPSHOT_CACHE_TTL_SEC=300
SCREENING_SOURCE_CALL_TIMEOUT_SEC=
SCREENING_HOTSPOT_CALL_TIMEOUT_SEC=8
SCREENING_HOTSPOT_SEARCH_TIMEOUT_SEC=12
SCREENING_SNAPSHOT_CALL_TIMEOUT_SEC=60
SCREENING_DAILY_CALL_TIMEOUT_SEC=20
SCREENING_EASTMONEY_MIN_INTERVAL_SEC=1.0
SCREENING_EASTMONEY_JITTER_SEC=0.3
SCREENING_EXTRA_STRATEGIES_DIR=
```

`SCREENING_EXTRA_STRATEGIES_DIR` 指定用户自定义选股策略目录（可选）：目录下的 YAML 会与内置策略合并，同名时覆盖内置策略。目录格式与内置 `strategies` 目录一致（策略 YAM schema 相同），且不受 AlphaSift 派生出处声明约束，适合放置用户自建策略。

硬过滤 DSL 支持板块与题材维度的可选字段（写入策略 YAML 的 `screening.hard_filters`）：

- `board_whitelist`：板块白名单，取值 `main_board`（沪深主板）/ `chinext`（创业板）/ `star_market`（科创板）/ `bse`（北交所），也接受中文别名（主板/创业板/科创板/北交所）；按证券代码前缀判定，无法识别的代码视为不匹配。
- `concept_keywords`：概念题材关键词列表，快照概念文本包含任一关键词即通过。快照不含概念列时（如部分数据源 fallback）该条件自动放行（fail-open）；概念列存在但匹配不到关键词的个股会被排除。

两个字段均可选，不配置时不改变任何现有策略行为。

路径、缓存、超时和限流项只影响选股链路。`SCREENING_SNAPSHOT_CACHE_TTL_SEC` 默认 300 秒，设为 `0` 可关闭新鲜快照复用。`SCREENING_HOTSPOT_CALL_TIMEOUT_SEC` 的正数值是默认热点 provider 单次板块、成分股或直接详情 fallback 调用的总预算，后续 fallback 和并行成分股源共用同一截止时间，并把剩余时间传入可终止的 AkShare 子进程与 HTTP socket；设为 `0/off/disabled` 只关闭这层总预算，各真实数据源仍保留自身硬超时。`SCREENING_HOTSPOT_SEARCH_TIMEOUT_SEC` 是用户主动新闻搜索的端到端截止时间，缓存 owner 等待、重新竞争和 provider 子进程共享同一个绝对 deadline；`0/off/disabled` 会回退到安全默认值 12 秒，而不是无限等待。完整示例以 `.env.example` 为准。

## API 契约

### 页面选股范围

选股页先显示热门题材推荐，按**涨停股票数**降序排列。点击推荐卡片/折叠标签直接加入筛选，重复点击不重复添加；详情使用独立按钮。用户也可输入任意题材，按 Enter 或“添加题材”加入，最多 5 个。添加、移除、匹配方式切换都在本地完成，不调用题材目录搜索、不自动运行选股。

交易板块支持沪深主板、创业板、科创板、北交所；全部 A 股不额外限制板块。同步与后台接口共用请求：

```json
{"market":"cn","strategy":"volume_breakout","max_results":10,
 "scope":{"boards":["main_board"],"topics":[{"name":"机器人","source":"custom"}],"topic_match":"any"}}
```

- `topics[].source` 新增 `custom` 并作为默认值，兼容原 `concept` / `industry`。名称只作为模型的数据，不具有指令权限。名称去空白后最长 80 字；同名重复、超过 5 个、非法板块等返回 `422`。空题材数组不限制题材，非 A 股不允许这些范围条件。
- 快照硬过滤之后，先去掉在东方财富重点监控名单有效期内的 A 股（读本地 `stock_event` 表，名单由每天的资讯任务更新；表不可用时跳过），去掉的股票记在结果的 `degradation` 里。
- **顺序：交易板块 → 策略快照/日线硬过滤与因子评分 → 有限候选池 → 模型判断题材并排序 → 原有风险/组合约束 → 返回上限。** 不再按目录名称或成分股预先排除股票，也不读取热点详情中的抽样股票作范围。策略 YAML 的 `concept_keywords` 原语义保留，不等于页面自定义题材。
- 沿用候选池预算：按策略评分截取候选，DSA 默认最多 12 只交给模型，模型判断不宣称覆盖全部符合策略的股票。每个送审代码（包括排除者）都须有 `matched_topics` 和 `topic_reason`，匹配项还须有评分；要求 100% 覆盖，缺项、重复代码、未知题材等响应无效。`any` 至少匹配一个，`all` 匹配所有题材；证据不足不入选。模型全拒绝是合法空结果。
- 有题材时，模型未配置或全部模型失败将任务标记失败，不退回未筛题材的因子结果。不选题材时仍保留原因子降级。模型推断不是权威成分归属；候选展示 `matched_topics`、`topic_reason`，运行保存 `topic_filter_mode=model`。
- `scope_counts.board_count` 为交易板块过滤后数量，`after_filter_count` 为策略过滤后数量，`model_reviewed_count` 为本次实际送审数量，`topic_count` 为模型题材过滤后数量（风险/组合约束前）。不传题材时没有题材过滤。结果和历史保存原请求范围；修改表单不改变已有结果，无数据库迁移。旧记录的 `topic_evidence` 继续按旧成分来源语义显示。

推荐数据通过 `GET /hotspots?sort_by=limit_up_count` 获取，默认只读独立缓存 `data/screening/hotspot_recommendations.json`，手动刷新时一次读取东财涨停池，30 秒冷却合并同进程刷新，不逐题材抓取成分股。统计日期使用最近已完成交易日并在卡片显示。按股票代码去重统计，绝不使用 9.8% 涨幅阈值、异动次数、热度分或龙头样本数冒充涨停股数。东财池不保证全市场覆盖（来源注明排除 ST、科创板及部分新股）。

行业取涨停池的所属行业，概念取已配置本地题材映射及有效的现有行业/概念缓存，概念计数只表示这些映射内的已知涨停数；没有概念映射时明确提示仅推荐行业，仍可输入任何题材。刷新失败保留旧统计日期并提示降级，不用旧热度榜伪装涨停榜。原 `sort_by=heat` 和题材详情接口保留以兼容旧调用方。

#### Tushare 题材接口评估

已有东财/同花顺题材目录与详情能力保留供旧 API 使用，目录成功缓存 5 分钟、失败冷却 30 秒。当前页面添加题材及选股执行不再依赖该目录，也不触发同花顺成分股分页。推荐的概念覆盖取决于已有映射，缺失时明确降级为行业统计。

Tushare 的接口能力能够覆盖“题材 ∩ 沪深主板 ∩ 策略”：[`stock_basic`](https://tushare.pro/document/2?doc_id=25) 提供 `market`、`exchange` 和上市状态；[`ths_index`](https://tushare.pro/document/2?doc_id=259) 与 [`ths_member`](https://tushare.pro/document/2?doc_id=261) 提供概念/行业目录及最新成分股；[`dc_index`](https://tushare.pro/document/2?doc_id=362) 与 [`dc_member`](https://tushare.pro/document/2?doc_id=363) 支持按交易日期查询板块及成分。题材接口在官方文档中标注需要 6000 积分，基础信息接口可用不代表题材接口也有权限。

当前页面范围链路尚未接入 Tushare 题材接口，行情快照已有的 Tushare 优先级不等于题材来源优先级。启用前必须分别验证目录和成分股权限；有权限时应优先采用同一套目录与成分接口，并保存题材代码、来源和数据日期。不得仅凭同名跨来源替换题材，也不得把权限拒绝或空成分解释为“全部股票”。

#### API 路径

| 路径 | 方法 | 行为 |
| --- | --- | --- |
| `/api/v1/screening/status` | GET | 返回开关、引擎状态、契约版本、参考项目和数据源健康信息 |
| `/api/v1/screening/strategies` | GET | 返回选股策略 |
| `/api/v1/screening/topics?q=关键词` | GET | 搜索概念/行业目录，最多返回 50 项；`warnings` 标识部分目录失败，全部失败返回 `424` |
| `/api/v1/screening/hotspots` | GET | 读取缓存或显式刷新热点题材 |
| `/api/v1/screening/hotspots/{topic}` | GET | 返回题材路线、成分股与核心股；`include_search=true` 时按需搜索近期消息 |
| `/api/v1/screening/screen/check` | POST | 用调用方提供的单条快照，逐项检查策略硬过滤条件；不抓取行情 |
| `/api/v1/screening/screen` | POST | 同步执行选股；可传匿名 `variant_seed` 在每次运行中生成有界的近分候选组合 |
| `/api/v1/screening/screen/tasks` | POST | 提交后台选股任务；请求字段与同步接口一致 |
| `/api/v1/screening/screen/tasks` | GET | 已保存的选股任务列表（新到旧，`limit` / `offset` 分页，返回 `tasks` 与 `total`，不含结果） |
| `/api/v1/screening/screen/tasks/{task_id}` | GET | 查询任务进度、错误或最终结果；任务同时写入 `screening_tasks` 表（状态 `pending` / `processing` / `completed` / `failed` / `interrupted`，完成后用 `run_id` 关联 `screening_runs` 的结果），服务重启后仍可查询，重启时未完成的任务标为 `interrupted` |
| `/api/v1/screening/history` | GET | 按策略、市场查询最近完成的选股运行摘要 |
| `/api/v1/screening/history/{run_id}` | GET | 读取一条持久化的完整选股结果 |
| `/api/v1/screening/source-history` | GET | 汇总历史运行中的快照源命中、错误和降级次数 |

后台任务使用 `report_type=screening_screen`，Web 会保存活动任务 ID，并在页面恢复时继续轮询。任务状态会分别提示全市场快照、候选上下文、LLM 重排、最终评分和新闻事件增强等阶段；完成后的结果同时写入 DSA 数据库，因此服务重启后仍可按 `run_id` 查询。新运行的同步/后台响应及历史详情保留 `strategy_version`、`strategy_category` 和 `effective_factor_weights`，记录执行时的策略版本、类别和实际归一化因子权重；旧记录不回填，也不依据升级后的策略文件改写历史。

## 单股条件检查（提供快照）

快照或日线硬过滤后没有候选的运行也保留当次策略的归一化因子权重，与非空结果一致；权重由实际执行策略在管线出口生成，服务响应和历史记录不重新读取策略目录补填缺失权重。这些元数据记录选股配置，不表示被淘汰的候选已经完成评分。

`POST /api/v1/screening/screen/check` 供脚本/API 客户端解释任意一条快照为什么通过或未通过策略硬过滤。需要开启 `SCREENING_ENABLED`；与已有选股接口共用管理员认证中间件，启用认证时无有效会话返回 `401`。策略 ID 从当前启用的策略目录查找，并校验 `market`（`cn`/`us`）是否在该策略的 `market_scope` 内。

```json
{
  "strategy": "dual_low",
  "market": "cn",
  "snapshot": {
    "code": "600000", "name": "Example", "price": 10,
    "amount": 100000000, "total_mv": 10000000000,
    "pe_ratio": 8, "pb_ratio": 1, "change_pct": 0
  }
}
```

- 返回 `strategy`、`strategy_version`、`market`、`provenance: "supplied_snapshot"`、整体 `passed` 和 `checks`。每项包含 `filter`、标准 `field`、实际命中的 `source_field`、`threshold`、`current_value`、`status`（`pass`/`fail`/`missing`）与 `passed`（布尔值或 `null`）；`missing_reason` 区分缺列、空值、非法数值/文本和非有限值
- 所有启用的硬过滤条件按配置模型字段顺序独立执行，即使第一项失败也继续检查后续项。复用现有过滤谓词；数值上下界含边界，`0` 阈值仍生效，空白名单和关闭的布尔条件不返回
- 缺列、`null`、空文本、不可解析数字和非有限数字输出 `missing`，`current_value`/`passed` 为 `null`。缺失不是通过；整体 `passed` 仅在所有条件通过时为 `true`（无启用条件时为 `true`）。这是诊断接口对不可用数据的保守未知标记：旧管线可能允许空名称通过 ST 检查或允许正无穷通过数值下界，本接口仍返回 `missing`；不改写旧管线行为
- 输入须为 1–64 个字段的扁平 JSON 对象；键长 1–64 字符，文本值最多 256 字符；嵌套值和超出有限浮点范围的整数返回有界 `422`。使用现有标准字段/中文别名、已归一化单位，不做币种或百分比换算；同时提供标准名和别名时，标准名优先
- 日 K 条件直接使用提供的特征，未提供则为 `missing`，不会补算或补抓。响应没有实时、时间戳或数据源可信度声明；股票代码和市场归属由调用方负责，不能把该结果当成已验证的证券身份或实时选股结果
- 不评分、不排名、不调用 LLM、不写运行历史、不交易。已有同步/后台选股、水瀑统计和 Web 页面保持原行为；此小接口尚未添加 Web UI

该交互参考 [Koyfin Check Ticker](https://www.koyfin.com/help/release-notes/check-ticker/) 的逐条件值与通过/失败展示，仅借鉴解释方式；不接入其数据或服务。

## 核心流程

```text
策略加载
  -> 全市场快照与字段标准化
  -> 形态预扫描（可选，策略声明 setup_scan 时）
  -> 硬过滤
  -> 因子评分与风险调整
  -> 有界候选行情/基本面/新闻与事件上下文补充
  -> LLM 重排（可降级）
  -> 风险/组合约束与近分候选轮换
  -> Top 候选 DSA 行情/基本面/新闻增强
  -> API 归一化响应与 DSA 数据库持久化
  -> 用户按需进入 DSA 单股深度分析
```

- 全市场快照在短 TTL 内优先复用最近成功结果；新缓存会记录完整且有序的数据源优先级，只在当前优先级与写入时一致时复用，因此同一来源链中的后备源结果可以加速后续请求，修改来源配置后则会重新读取实时数据。缓存过期后按配置优先级逐源尝试，单一数据源失败后继续降级，并记录 source health 与 last-good 缓存。当前 Sina、Efinance、AkShare/东财和 Tushare 快照接口均不提供增量游标或变更序列，因此 TTL 内可以零请求复用，TTL 到期后仍需重新读取全表；本地比较前后差异不能减少上游传输量，不作为“增量拉取”宣传。
- 有 `TUSHARE_TOKEN` 时默认优先 Tushare，否则默认从 Sina 开始；显式 `SNAPSHOT_SOURCE_PRIORITY` 始终优先。
- Tushare 快照与日 K 使用 DSA 数据源层的 Tushare HTTP 客户端（`data_provider/tushare_fetcher.py`），不依赖 tushare SDK。默认请求官方经典端点 `http://api.tushare.pro`；配置 `TUSHARE_HTTP_URL` 时与数据源层含义一致，即接口名放在 POST 请求体中、直接请求该地址，不要填写 SDK 风格的 `.../dataapi` 地址。选股不再读取未文档化的 `TUSHARE_API_URL`。HTTP 错误、接口错误码，以及快照 `trade_cal`/`daily`/`daily_basic`/`stock_basic`、日 K `daily`/`adj_factor` 的空表都按该来源失败处理：遇到第一张空表即停止后续请求，失败计入 source health，并按既有顺序降级到下一来源，不再静默返回空表。快照取最近开市日的收盘数据，当日数据入库前（含盘中）会因 `daily` 为空降级到 Sina 等实时源。
- 日 K 优先通过请求级 fetcher 复用 DSA 历史行情链路，无结果时再走筛选引擎的数据源降级；该桥接不会替换进程级函数，因此重叠选股请求之间不会共享 wrapper 或阻塞彼此。
- 存在可用的 LLM 配置时，重排前对快照评分前 3 个候选补充 DSA 行情、基本面、新闻与事件（每类最多 3 条）。新闻先取资讯库里最近 30 天关联到该股（分类得到的 `stock_codes`，或标题里有股票名）的财联社 / 东方财富快讯，事件先取 `stock_event` 表里该股的结构化公司事件（严重异常波动、重点监控名单、解禁、增减持、业绩预告 / 快报、质押、龙虎榜、开盘啦打板、热股榜、回购、券商研报、调研，按这个顺序；龙虎榜标题带机构和沪深股通净额，研报标题带评级、目标价和最近年度 EPS），再取资讯库里该股的公告，不够时再用网页搜索补；网页结果只保留文字可读（不是乱码）且标题或摘要里出现股票代码或名称的条目，被丢掉的条数记在结果的 `dropped` 字段，没有可用条目时新闻 / 事件为空，模型据此写「新闻事件未采集」而不是读到乱码或别家公司的新闻。让事件风险可以进入既有 LLM `risk/risk_flags` 与后续风险评分。名单确定后，每个入选候选还会按固定规则从 `stock_event` 读出事件风险（近期解禁、大股东减持、高质押、业绩预告转差、重点监控名单、近 10 天严重异常波动），追加到该候选的 `risk_flags`，原文另存 `event_risks`；这一步只读本地表。仅因子排序时继续使用 `pre_rank_light`，新闻和事件仍在名单确定后获取；不新增关键词否决或风险阈值。
- 提示词中的新闻/事件证据包含有界标题、摘要、来源、链接，并保留已有的原始发布时间与检索时间；搜索适配器将上游结果的 `retrieved_at` 原样传递至排序、最终响应与运行历史，缺少时间字段时保持未知，不使用适配或缓存读取时刻补造检索时间。真实搜索结果模型包含可空 `retrieved_at`：供应商实际查询成功时记录 UTC 检索完成时间，后续日期归一化、过滤、相关性排序及缓存复用均保留原值；已有时间不会覆盖，旧结果缺少时间仍为未知。检索时间不代表事件发生或发布的时间。未采集、无结果、查询失败都不代表已排除风险，模型应区分证据覆盖差异与真实风险。已有提示词长度预算仍可能压缩证据，并记录裁剪说明。
- 市场上下文及候选资料（包括名称、摘要、新闻、事件和链接）作为不可信 JSON 数据传递，独立 system 消息要求模型不执行其中的排序命令或伪造指令。直接调用、渠道、Router、备用模型和参数恢复使用相同边界；长度预算包含 system 消息及覆盖率重试提示，裁剪不会截断外部数据的 JSON 边界，预算不足时交由既有覆盖率保护降级。此措施降低提示词注入风险，不保证模型一定忽略所有恶意内容；候选代码白名单与结构化输出校验继续生效。
- `pre_rank_research` 的完整结果在单次选股调用内复用，入选候选不重复查询已尝试的新闻/事件，包括空结果、未知发布时间和失败结果；下一次运行重新走搜索服务的既有缓存与降级链路。重排后新入选且未提前查询的候选仍按原有入选增强上限（前 3 个入选候选）补充；一次运行最多覆盖重排前 3 个加入选后 3 个不同候选。搜索延迟可能前移，单个来源失败不会中断选股。
- 该改进让有限候选的事件证据参与 LLM 排序，不等于全市场事件硬过滤或已确认的自动事件风控；LLM 不可用时保留原有因子排序和降级提示，因子模式不依据事后搜索结果改写名单。
- 默认本地 `scorecard` 会覆盖完整短名单，保证所有可能进入近分轮换的候选使用同一最终评分口径；多个后置分析器串联时，每一步完成后都会按最新分数重排，因此后续 `dsa` 与 `external_http` 的 `POST_ANALYSIS_MAX_PICKS` 上限作用于当前真实前列。远程状态按实际提交候选记录，外部响应中的超限代码不会改写未提交候选；启用远程分析时轮换只会在已完成相同分析的候选之间发生。
- 模型、渠道、base URL、额外 headers、fallback、timeout 和 token 上限在单次调用范围内注入，不改写用户配置；主模型即使 HTTP 调用成功，但返回空内容、非 JSON 或覆盖率不足，也会继续尝试已配置的备用模型。最终 JSON 必须在 `content` 块或 `output` 块中；`reasoning_content`（链式思考）被视为内部辅助，不作为最终结果。
- 热点榜单刷新与选股长流程可并行执行；列表默认不批量预取详情，用户选中具体题材时才加载该题材详情；显式刷新后若继续保留当前题材，Web 会同步绕过详情缓存重拉该题材，保证榜单与详情来自同次刷新。
- 热点成分股并行获取东方财富与同花顺数据，并按固定数据源优先级合并，避免响应先后改变重复股票的字段：正数 `SCREENING_HOTSPOT_CALL_TIMEOUT_SEC` 作为默认 provider 整次调用的共享预算，板块列表、成分股以及引擎失败后的直接详情 fallback 都进入该预算；AkShare/东方财富的可终止子进程与同花顺 HTTP connect/read timeout 每一步只使用剩余时间，fallback 不会重新获得完整预算。直接详情 fallback 不再额外启动无法由该预算强制回收的实时行情预取，行情字段以已受控的成分股源结果为准。超时子进程会 terminate/kill 并回收；进程级并发槽限制活跃任务数量，方法返回前会等待已接纳的 worker 结束，不遗留后台线程。关闭整次调用预算时，各单源仍保留默认硬超时；单源失败不会阻止另一源和本地核心股回退。
- “搜索最新消息”复用 DSA 原生搜索服务的 provider 优先级、SearXNG 公共实例能力、结果缓存与请求合并，同一缓存键只有 owner 可以启动供应商链；owner 未产出可缓存结果时，等待者重新竞争且未获得所有权的请求继续等待，但缓存等待、抢占和 provider 执行共用请求的绝对 deadline，不会在排队后重新获得完整超时。搜索只补充有链接的事件/催化，不从网页内容推断板块成分股；增强记录分别追加到展示路线和原始时间线，不覆盖已有 `timeline`。搜索由用户主动触发，摘要在本地确定性压缩，不调用 LLM；响应以 `available`、`no_results`、`unavailable` 区分有结果、有效空结果和超时/容量/供应商失败，Web 不会再把运行失败提示成“没有近期消息”。搜索增强仅存在于本次响应，不写入或续期共享热点详情缓存，默认详情请求不会看到搜索状态或增加搜索等待。供应商子进程在启动、执行或清理任一阶段失败时都会释放进程级容量。
- 热点实时请求失败时优先使用 last-good cache；无缓存时返回稳定空态与明确错误。

## 形态预扫描（setup_scan）

快照硬过滤只看当日行情，日 K 也只对有限候选补充，回答不了"过去几个交易日谁放量大涨、现在回踩到哪"这类事件型问题。策略 YAML 可在 `screening.setup_scan` 声明全市场形态预扫描：管线在交易板块范围之后、硬过滤之前运行它，只保留仍在有效期内的形态候选，之后照常进入硬过滤、因子评分、LLM 重排和风险/组合流程。

目前支持 `type: surge_pullback`（放量大涨 → 回踩均线），字段与默认值：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `lookback_days` | 10 | 大涨后多少个交易日内仍可挂单（1–30） |
| `surge_change_pct_min` | 7.0 | 大涨日收盘涨幅下限（%），涨停亦满足 |
| `surge_volume_ratio_min` / `surge_volume_ratio_max` | 2.0 / 5.0 | 大涨日成交量相对前 5 日均量的倍数区间；上限设 `null` 即不限 |
| `surge_turnover_rate_max` | 10.0 | 大涨日换手率上限（%，Tushare `daily_basic`），缺失视为不通过 |
| `prior_20d_change_pct_max` | 20.0 | 大涨前 20 个交易日涨幅上限（%） |
| `industry_peers_min` | 1 | 同一 Tushare 行业当日另有几只放量大涨（同样排除 ST、次新和低流动性） |
| `amount_20d_avg_min` | 30000000 | 大涨前 20 日日均成交额下限（元） |
| `listed_days_min` | 90 | 上市天数下限 |
| `buy_line_ma` | 10 | 买入价格线：前一交易日收盘计算的 N 日均线 |
| `regime_index` / `regime_change_20d_pct_min` | `000852.SH` / 5.0 | 市场开关：指数近 20 个交易日涨幅须大于阈值（%）；阈值设 `null` 即关闭开关 |
| `regime_off_action` | `flag` | 开关关闭时：`flag` 仍返回观察名单并逐只加风险标签，`block` 返回空名单 |

规则语义：

- 同一股票出现更新的放量大涨时，以最新一次为准，旧形态作废。
- 大涨后任一收盘跌破大涨日最低价，形态作废。
- 开关开启的交易日若最低价触及前一日价格线，视为已按规则买入，不再重复列出；开关关闭的交易日不挂单，不算触发。
- 窗口内按 `pre_close` 链后复权计算，价格线和取消线换算回最新不复权价，除权前后不会错位。
- 北交所以及名称含 ST/退的股票不参与扫描；快照 `exclude_st` 仍会再过滤一次。

候选的 `setup` 字段记录：大涨日、涨幅、量能倍数、换手、同行业共振家数、下一交易日是大涨后第几天、剩余窗口、`buy_line`、`cancel_line`、`next_session` 和开关状态。这些字段会转成 `setup_scan` 来源的解释条目（见[选股解释契约](screening-explanations.md)）。开关状态和扫描统计排在运行 warnings 最前面。

数据与请求量：

- 需要 `TUSHARE_TOKEN`。扫描复用 DSA 的 Tushare HTTP 客户端，接入地址可用 `TUSHARE_HTTP_URL` 覆盖。未配置或接口失败时，该策略运行失败并给出原因，不会退回未扫描的快照名单。
- 全市场日线（`daily`）和换手（`daily_basic`）按交易日各请求一次，缓存到 `<SCREENING_DATA_DIR>/market_daily/`，已缓存的交易日不再请求。当日数据未发布（空结果）时不写缓存，回退到上一交易日。
- 首次运行约 50 次请求（≤5 次/秒）。之后每天约 3–5 次：交易日历、指数，以及新增交易日；股票列表缓存 24 小时。

示例（放在 `SCREENING_EXTRA_STRATEGIES_DIR` 中）：

```yaml
name: surge_pullback
display_name: 放量大涨·回踩MA10
description: 强市中温和放量大涨且板块共振的个股，按前一日 MA10 挂限价单买入
screening:
  enabled: true
  market_scope: [cn]
  setup_scan:
    type: surge_pullback
    regime_off_action: flag
  hard_filters:
    exclude_st: true
  factor_weights:
    small_cap: 1.0
  max_output: 20
```

评分新增 `small_cap` 因子：流通市值越小分数越高，缺流通市值时用总市值，可与其他因子组合；原 `size` 因子仍是大市值优先。

## 结果轮换

Web 会在浏览器本地生成一个不含用户信息的匿名种子，并随同步或后台选股请求传入 `variant_seed`。如果 Web Storage 不可读写，则在当前页面会话的模块内存中复用同一个临时种子，保证同步和后台任务入口一致。服务端将匿名种子与本次运行 ID、市场和策略共同作为扰动输入：不同浏览器以及同一浏览器的不同运行，都可能在质量接近的候选中看到不同股票。

扰动不是随机改分，也不会绕过策略：硬过滤、风险否决、因子/LLM 得分、最终评分和组合集中度惩罚全部先执行。默认本地评分覆盖完整短名单；启用有数量上限的远程后置分析时，只有完成相同分析的候选才可参与轮换。分析器产出的候选顺序是轮换输入的权威顺序，并列分不会再按股票代码重新排序；原 Top-N 的前半部分和明显高于截止分的候选始终受保护，只有后半部分名额可从不低于原截止分 1.5 分的近分池中抽取，入选候选继续保持该输入相对顺序。种子不写入选股结果或运行历史。未传 `variant_seed` 或将轮换比例设为 0 时返回严格输入 Top-N，保持脚本与旧客户端兼容。

## 缓存与持久化

| 数据 | 位置 | 有效期/行为 |
| --- | --- | --- |
| 全市场快照 | `data/screening/snapshot.last_good.json` | 默认 5 分钟内直接复用且不标记 fallback；过期后请求实时源，实时源全部失败时仍可按最大陈旧时间约束回退并标记 stale/fallback |
| 个股日 K | `data/screening/daily_history/` | 按代码、来源和回看窗口分键，默认 TTL 24 小时，并核验最新已收盘交易日及缓存获取时段；刷新失败时可使用旧数据并标记 stale |
| 行业/概念映射 | `data/screening/industry_provider_cache/` | 默认 TTL 24 小时，并保存板块热度历史用于趋势计算 |
| 热点列表与历史 | `data/screening/hotspots.json`、`hotspot.history.jsonl` | 显式刷新写入；实时失败时回退最近可用快照 |
| 热点详情 | `data/screening/hotspot_details/` | 默认 TTL 30 分钟；只缓存结构化基础详情，显式消息搜索不写入或续期该缓存；实时失败时可回退过期详情并返回陈旧时长 |
| DSA 实时行情 | `DataFetcherManager` 的行情缓存 | 默认 TTL 10 分钟，沿用 `REALTIME_CACHE_TTL` |
| DSA 基本面/资金流 | `DataFetcherManager` 的基本面缓存 | 默认 TTL 120 秒，沿用 `FUNDAMENTAL_CACHE_TTL_SECONDS` |
| DSA 新闻/公告事件 | `SearchService` 内存缓存 | 成功结果默认 TTL 10 分钟；同题材并发请求在父进程合并，实际供应商链在限流、可终止的子进程中执行；服务重启后重新查询 |
| 完整选股结果 | DSA 数据库 `screening_runs` 表 | 完成后按 `run_id` 幂等写入；数据库写入失败不阻断选股主流程 |

候选上下文模块也支持 24 小时文件缓存，但 DSA 集成默认关闭其独立新闻/公告抓取，改用 DSA 自己的资讯、基本面和实时行情链路，避免同一候选重复请求两套数据源。

### 日线新鲜度

- 复用现有交易日历与市场时区：盘前只接受上一已收盘交易日，盘中要求至少覆盖上一已收盘交易日且最新日期为实际交易日，收盘后要求覆盖当日；周末与节假日要求覆盖最近已收盘交易日。重新开盘后的盘中阶段也拒绝日期区间内的休市日伪日线。TTL 仍是独立上限，休市不会自动延长 TTL。
- 缓存文件在收盘前生成，即使包含当日的部分 K 线，也不能在收盘后直接视为完整日线。缓存覆盖日期不足、日期缺失/无效、日期超出当前市场日期或最新日期没有有效收盘价时，TTL 内也会尝试刷新。
- DSA 日线同样核验覆盖日期并保留已有陈旧标记。旧 DSA 数据先尝试选股原生数据源；原生来源取得新鲜日线时替换，全源陈旧时选择最新有效日期，同日保留 DSA 优先级，并记录备用来源的失败和降级信息。DSA 与原生来源的陈旧日线均不会覆盖 last-good 文件缓存；调用方注入旧日线时也会标记陈旧。
- `DAILY_SOURCE=auto` 的非空陈旧结果不会终止来源链路，仍按既有来源顺序尝试后续来源。取得新鲜结果才更新缓存；所有来源都无法提供新鲜日线时，比较所有实时陈旧结果和文件缓存的最新有效日线日期，优先使用日期较近的数据，同日仍保留原来源优先级。缓存中的盘中数据继续标为陈旧，不能冒充完整收盘数据。陈旧来源记入来源顺序说明，真实请求失败仍记录在 `source_errors`；新鲜备用结果不会继承陈旧标记。
- 文件缓存优先使用载荷内带时区的获取时间检查 TTL 与收盘切换，桌面备份恢复改写 mtime 不会刷新这类缓存的获取时间；旧版无时区的 `created_at` 无法确定原写入机器时区，与时间缺失或非法的缓存一样回退 mtime，跨主机迁移时应保留文件 mtime。所有带有效收盘价的日期行均核验实际交易日，原生来源、DSA 与缓存中夹杂的周末、节假日或缺失/无法解析日期的 bar 也会被拒绝，即使最新一行是合法交易日；没有有效收盘价的行不参与该核验。
- 显式 `DAILY_SOURCE=yfinance` 按美东市场日期确定日线下载范围，排他的 `end` 设为下一自然日，使 UTC、美东等部署时区在收盘后都能取得供应商已提供的当日 K 线。
- 陈旧数据沿用 `daily_stale` / `daily_quality_flags=stale_cache` 的降级契约，进入既有日线质量评分与风险惩罚，不将数据源故障变成整个选股任务失败。新鲜度检查不改变策略硬过滤、风险否决开关或因子权重。
- 日历不可用、市场无法识别或日历范围不足时，保留 TTL 判断；显式陈旧标记仍有效。日期覆盖只能检查历史是否滞后，无法证明供应商同日 K 线已完成结算；无供应商获取时间的同日数据保留这一边界。

选股运行历史目前用于复现名单与诊断；已有 `BacktestService` 的候选来自单股分析历史，并未直接评估 `screening_runs`。直接按选股运行进行 T+N 评估仍需接入现有回测链路，保存策略元数据本身不代表已完成收益验证。

## 两类策略的边界

DSA 中存在两类用途不同的策略文件：

| 位置 | 解决的问题 | 加载方 | 执行阶段 |
| --- | --- | --- | --- |
| `src/services/screening/strategies/*.yaml` | 从全市场筛出哪些候选 | `src/services/screening/strategy.py` | 快照过滤、因子评分、风险和排序 |
| `strategies/*.yaml` | 对单只股票如何分析和形成结论 | `src/agent/skills/base.py` | DSA Agent/报告分析 |

即使 `shrink_pullback`、`volume_breakout` 同名，两者也使用不同目录、Schema 和 loader，不会相互覆盖。筛选策略可通过 `analysis_skills` 声明下一阶段建议使用的分析 skill；Web 的“进一步深度分析”会显式携带这些 skill。未声明映射的筛选策略继续使用用户当前选择或默认分析策略，不做含义不可靠的强行映射。

## DSA 原生能力复用

- 行情：日 K 优先调用 DSA `DataFetcherManager`，无结果才进入筛选模块自己的多源 fallback；最终候选继续补 DSA 实时行情。
- 基本面与资讯：最终候选复用 DSA 基本面上下文和 `SearchService`；资本流向来自 DSA 基本面上下文，重要公告/业绩/减持事件调用 DSA `search_stock_events`，热点消息搜索沿用其数据源优先级、时效过滤、缓存和同请求合并，仅将真实供应商调用隔离到可终止子进程，不重复维护独立资讯入口。
- 模型：沿用 DSA LiteLLM 模型、渠道、fallback、base URL、额外 headers、超时和 token 配置。
- 任务与页面：复用 DSA 后台任务队列、Web 轮询和桌面端同源 Web 资源。
- 存储与后续分析：运行结果写入 DSA 数据库；候选可进入 DSA 原生单股分析并携带策略 skill。

对照固定参考提交，快照、日 K、美股、行业/概念、热点、候选新闻/公告/资金流、字段标准化、过滤、评分、风险、排序和数据源熔断等原始数据与选股能力均已纳入；其中公告/事件和资金流在 DSA 编排层分别接入原生事件搜索与基本面上下文。参考项目另外提供独立 CLI/server、JSON 文件 store、报告渲染、doctor、运行/数据源历史和 T+N 评估：本实现只吸收 DSA 确实缺少的运行历史与数据源历史，并接到 DSA 数据库；CLI/server 不重复建设。后续选股 T+N 评估应接入 DSA 已有 BacktestService，避免形成第二套回测真源；当前服务评估单股分析记录。实时 source health 已在 `/status` 返回，历史稳定性由 `/source-history` 补齐。

## 收益

1. 选股服务、策略、API、Web 和打包脚本在同一版本中演进，避免契约漂移。
2. 服务层只有一套原生调用路径，状态探针和业务请求反映相同实现。
3. Docker 与桌面产物直接收集同一份模块和策略资源，部署结果更一致。
4. 数据源降级、评分和策略变化可以在主仓库完成端到端审查与回归。
5. 来源 commit、许可证和逐文件归因明确，便于后续选择性同步上游修复。

## 风险与控制

| 风险 | 影响 | 控制措施 |
| --- | --- | --- |
| 主仓库维护面扩大 | 数据源或策略问题由 DSA 直接承担 | 模块边界、契约测试和 CI 打包探针共同约束 |
| 与参考项目逐渐分叉 | 上游修复不能直接覆盖 | 固定参考 revision，逐模块比较并选择性移植 |
| 数据源限流或字段变化 | 快照、热点或日 K 降级 | timeout、retry、source health 与 last-good cache |
| LLM 超时或格式异常 | 重排不可用或解释字段缺失 | 非结构化响应继续尝试备用模型；无题材限制时全部失败保留因子排序；指定题材时失败不返回未核验名单 |
| 结果轮换扩大候选差异 | 临界候选可能因浏览器不同而变化 | 仅轮换近分尾部位，保持硬过滤、风险否决、分值和头部候选不变；无种子时关闭 |
| 缓存目录变化 | 升级后旧缓存不会自动复用 | 新目录独立为 `data/screening`；升级前按需备份 |
| 运行历史增长 | 完整候选结果会增加数据库体积 | 历史接口默认只读摘要，运维可按现有数据库备份/保留策略管理 |
| 配置与 API 更名 | 旧自动化需同步调整 | 在发布说明明确 `SCREENING_ENABLED` 与 `/api/v1/screening` |
| 许可证归因遗漏 | 发布合规风险 | 保留 LICENSE、THIRD_PARTY_NOTICES 和衍生文件头 |

选股结果仅用于研究和辅助判断，不构成投资建议，也不保证收益或数据完整性。

## 更新参考实现

AlphaSift 是参考来源，不是自动同步源。更新时应：

1. 记录目标 commit 和许可证变化；
2. 比较 `src/services/screening/` 的 DSA 特有修改，按模块选择性移植；
3. 更新衍生文件头、`REFERENCE_REVISION` 和 `THIRD_PARTY_NOTICES.md`；
4. 检查 pipeline、API/Web 字段、数据源降级、策略资源与冻结打包；
5. 更新本文档和 `docs/CHANGELOG.md`，完成后端、Web、Docker/桌面验证。

## 回滚

- 业务回滚：设置 `SCREENING_ENABLED=false` 并重启；普通个股分析、报告、通知和问股不受影响。
- 代码回滚：revert 引入选股引擎的提交并重建后端、Docker 与桌面产物。
- 数据回滚：如需保留选股缓存和运行历史，先备份 `data/screening/` 与 DSA 数据库；代码回滚不会主动删除 `screening_runs` 用户数据。

实际运行元数据 `effective_factor_weights` 保留有限、非负权重，包括明确为 0 的禁用因子；候选解释只消费正权重，避免将禁用因子作为入选理由。

盘前只接受上一已完成交易日，盘前获取的当天占位 K 线在盘中仍需刷新。DSA 和备用来源都陈旧时同样优先较新有效日期，同日保留 DSA 优先级。

全源降级比较只认可非未来、实际交易日的最新日线；非法日期优先级低于有效陈旧日线。日历不可用时保持原有降级语义。

仅有非法日期或盘前占位数据且无合法备用行情时，走已有聚合取数失败路径，不生成虚假交易日的技术因子；DSA 与文件缓存同样验证。

日线通过交易日期验证后才计来源成功；非法日期响应累计现有失败计数与熔断，冷却后合法响应恢复，合法陈旧行情仍按成功取数及陈旧质量处理。
