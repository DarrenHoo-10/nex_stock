# 数据渠道使用说明

本文按渠道说明项目接入的每个行情 / 数据来源：它能提供什么、要不要凭据、怎么配置、排在什么顺序、有什么限制、怎么确认它在工作。

和相邻文档的分工：

- 本文：**每个渠道怎么用**（配置、权限、限流、验证）。
- [数据源稳定性与故障处理图示](data-source-stability.md)：**失败时怎么降级**、按场景的 fallback 矩阵。
- [市场支持与边界](market-support.md)：日股 / 韩股 / 台股等市场的能力边界。
- [完整配置与部署指南](full-guide.md)：全部环境变量表。

> 以代码为准：优先级与回退规则来自 `data_provider/` 与 `src/config.py`。如果本文和代码不一致，以代码为准，并请顺手修正本文。

## 1. 总览

| 渠道 | 需要凭据 | 覆盖 | 主要用途 | 默认优先级 |
| --- | --- | --- | --- | --- |
| [Tushare Pro](#21-tushare-pro) | `TUSHARE_TOKEN` | A 股（部分接口含港股、美股） | 日线、实时单只报价、全市场快照、板块、资金流、基本面、筹码、股票列表 | 配置 Token 且初始化成功后为 **-1（最高）**，否则 2 |
| [必盈 API](#22-必盈-api) | `BIYING_LICENCE` | A 股 | 实时行情、全市场快照、5 分钟线（当天与历史）、买卖五档、指数 | 仅实时链路：配置后排在 `tushare` 之后、免费源之前；不参与日线 |
| [TickFlow](#23-tickflow) | `TICKFLOW_API_KEY` | A 股 | 日 K、实时行情（需显式加入）、股票列表、大盘复盘增强 | 日 K：2（`TICKFLOW_PRIORITY`） |
| [Efinance](#31-efinance东方财富) | 否 | A 股 | 日线、实时（东财） | 0（`EFINANCE_PRIORITY`） |
| [AkShare](#32-akshare) | 否 | A 股、港股、美股、指数 | 日线、实时（东财 / 新浪 / 腾讯）、基本面、板块 | 1（`AKSHARE_PRIORITY`） |
| [Pytdx](#33-pytdx通达信) | 否 | A 股 | 日线（通达信行情服务器） | 2（`PYTDX_PRIORITY`） |
| [Baostock](#34-baostock证券宝) | 否 | A 股 | 日线 | 3（`BAOSTOCK_PRIORITY`） |
| [YFinance](#35-yfinance) | 否 | 美股、港股、日股、韩股、台股 | 日线、实时、轻量基本面 | 4（`YFINANCE_PRIORITY`） |
| [Tencent](#36-tencent腾讯财经) | 否 | A 股、已登记指数 | 日线兜底、实时 | 5（`TENCENT_PRIORITY`） |
| [Futu OpenD](#41-futu-opend) | 本机 OpenD | 港股 | 港股实时 / 日线 / 基本面（只读） | 港股实时独立优先级 |
| [Longbridge](#42-longbridge长桥) | OAuth 或 Legacy 凭据 | 美股、港股 | 实时、换手率 / PE 等补充字段 | 5（`LONGBRIDGE_PRIORITY`） |
| [Finnhub](#43-finnhub--alphavantage美股补充) | `FINNHUB_API_KEY` | 美股 | 补充字段、日线链 | 2 |
| [AlphaVantage](#43-finnhub--alphavantage美股补充) | `ALPHAVANTAGE_API_KEY` | 美股 | 补充字段、日线链 | 3 |
| [妙想 MX](#51-妙想东方财富-mx_api) | `MX_APIKEY` | A 股 | 东财接口受限时兜底筹码分布与个股资金流 | `MX_PRIORITY`，默认 6（不参与日线） |
| [台湾三大法人](#52-台湾三大法人) | 否 | 台股 | 三大法人买卖超（仅数据层） | — |
| [FXMacroData](#53-fxmacrodata) | 可选 `FXMACRODATA_API_KEY` | 宏观 / 外汇 | 助手与报告 Agent 的宏观工具 | — |

优先级数字越小越先尝试。

## 2. 付费 / 凭据型渠道

### 2.1 Tushare Pro

**能提供**：A 股日线、股票列表与名称、实时单只报价（`rt_k`）、全市场快照与板块榜、个股资金流、基本面聚合（业绩预告 / 快报、财务指标等）、筹码分布、主要指数与市场统计。港股、美股只有在账号具备对应接口权限时才可用，否则会出现港股数据查不到或报错的情况。

**怎么配置**

1. 在 [Tushare Pro](https://tushare.pro) 注册并取得 Token。
2. 写入 `.env`（或在 Web 设置页填写）：

```bash
TUSHARE_TOKEN=你的token
# 可选：只有需要走公司代理、跨境网络或自建镜像时才填，必须以 http:// 或 https:// 开头
# TUSHARE_HTTP_URL=http://api.tushare.pro
```

**行为**

- 配置 Token 且 API 初始化成功后，Tushare 的日线优先级自动提升为 **-1**，排在 Efinance 之前；没有 Token 或初始化失败时保持 2。
- 实时行情优先级：配置了 Token 而没有显式设置 `REALTIME_SOURCE_PRIORITY` 时，会自动把 `tushare` 放到默认顺序最前面。
- 内置请求计数器按每分钟限额控流，超限会休眠到下一分钟；限流和超时按指数退避重试。
- 项目用内置 HTTP 客户端访问，不强依赖 `tushare` SDK。

**需要单独开通的接口**：`rt_k`（A 股日线 RT）和 `rt_min_daily`（A 股分钟 RT）**不含在积分内**，需要在 Tushare 另行开通。未开通时项目把它当作"干净的未命中"：记录一次后冷却 10 分钟不再请求，实时链路直接落到下一个渠道（必盈或免费源）。部分基本面、板块接口受积分门槛限制，权限不足时不重试，直接回退。

**安全提示**：`TUSHARE_HTTP_URL` 指向非官方地址时，Token 和全部请求内容都会经过该服务器，请自行评估风险。GitHub Actions 里它是"接入地址"配置，不是凭据；`vars` 非空时会压过同名 Secret，详见 [完整指南](full-guide.md) 的相关说明。

**确认它在工作**：启动日志出现 “检测到 TUSHARE_TOKEN 且 API 初始化成功，Tushare 数据源优先级提升为最高”；未配置时日志是 “Tushare Token 未配置，此数据源不可用”。GitHub Actions 的每日任务日志里也会打印 Tushare Token 是否已配置。运行时优先级可在 [数据中心](data-center-diagnostics.md) 查看。

**相关**：股票列表导出脚本见 [Tushare 股票列表指南](TUSHARE_STOCK_LIST_GUIDE.md)。

### 2.2 必盈 API

**能提供**（均为 A 股）：

| 能力 | 接口（证书为 URL 最后一段） | 说明 |
| --- | --- | --- |
| 个股实时报价 | `hsstock/real/time/{code}`、`hsrl/ssjy/{code}` | 券商源优先，公开源兜底 |
| 多只报价 | `hsrl/ssjy_more`（每次 ≤ 20 只） | 自动分批 |
| 全市场快照 | `vip.biyingapi.com/hsrl/real/all`（bulk） | 独立限流约 **3 次/分钟**，本地先挡一道 |
| 买卖五档 | `hsstock/real/five/{code}` | 成交量由"手"换算为"股" |
| 当天 5 分钟线 | `hsstock/latest/{code}/5/n?lt=N` | 助手盘中数据使用 |
| **历史 5 分钟线** | `hsstock/history/{code}/5/n?st=YYYYMMDD&et=YYYYMMDD` | 行情图双击日 K 看分时使用 |
| 指数 | `hsindex/real/time/{code}` | 如 `000001.SH` |

`{code}` 形如 `600519.SH`、`002029.SZ`、`920001.BJ`，由项目按代码自动补交易所后缀。

**怎么配置**

1. 在 [biyingapi.com](https://biyingapi.com) 取得证书（项目按包年版设计：每分钟 3000 次）。
2. 写入 `.env`，或在 Web 设置页填写：

```bash
BIYING_LICENCE=你的证书
BIYING_TIMEOUT_SECONDS=8   # 可选，单次请求超时（秒）
```

**行为**

- 只提供实时与分钟线能力，**不参与日线**。
- 配置后若没有显式设置 `REALTIME_SOURCE_PRIORITY`，实时顺序自动变为 `tushare → biying → tencent → akshare_sina → efinance → akshare_em`（只注入已配置的付费源）。想手动控制时，在 `REALTIME_SOURCE_PRIORITY` 里写 `biying`。
- 单位统一：成交量"手 → 股"（×100），成交额为元。
- 证书出现在请求 URL 里，项目在日志和异常里会把它**打码**；仍然不要把证书贴进 issue、截图或提交里。
- 全市场快照由助手和板块工具按需取，带 `INTRADAY_SNAPSHOT_TTL_SECONDS`（默认 60 秒）的内存缓存和单次飞行合并，不做定时轮询，不写入日线表。

**在 Web 里的用处**

- 个股行情窗口 / 个股计划页：双击日 K 查看当天分时（`GET /api/v1/stocks/{code}/intraday?date=YYYY-MM-DD`）。当天先走 Tushare `rt_min_daily`，再走必盈当天 5 分钟线；历史交易日只走必盈历史 5 分钟线，并缓存 6 小时。**没有配置 `BIYING_LICENCE` 时，历史分时会返回 503**。503 的 `error` 是 `intraday_unavailable`，`reason` 说明原因：`biying_not_configured`（历史交易日需要必盈证书）、`not_configured`（当天没有任何分钟线数据源）、`no_bars`（数据源没有这天的数据，如停牌、非交易日）、`source_failed`（数据源请求失败），`message` 是对应的中文说明，页面直接显示这条说明，不再显示通用的「检查代理、DNS」。
- 助手：盘中全市场快照、个股报价、5 分钟线（Tushare 失败后用必盈兜底）。

**确认它在工作**：行情窗口里双击一根历史日 K 能看到分时；或在日志里看到实时来源为 `biying`。证书错误时日志是 `Biying error: ***` 一类的打码信息，HTTP 非 200 为 `Biying HTTP <状态码>`。

### 2.3 TickFlow

**能提供**：A 股日 K（可批量预取）、实时行情、股票列表 / 名称、大盘复盘的指数与市场宽度增强；套餐支持 `CN_Equity_A` 标的池时还能做市场统计和申万一级行业榜。

```bash
TICKFLOW_API_KEY=你的key
TICKFLOW_PRIORITY=2            # 普通 A 股日 K 的优先级
TICKFLOW_KLINE_ADJUST=none     # none/forward/backward/forward_additive/backward_additive
TICKFLOW_BATCH_DAILY_ENABLED=true
TICKFLOW_BATCH_SIZE=100
```

要点：

- 没有 Key 不启用；权限不足或失败时自动回退，不会拖垮分析流程。
- **实时行情不会自动使用 TickFlow**，要在 `REALTIME_SOURCE_PRIORITY` 里显式加入 `tickflow`；排在它前面的渠道会先被尝试。
- `TICKFLOW_PRIORITY` 只影响普通 A 股日 K，不影响实时，也不影响已登记指数的固定链。
- 日 K 的 `volume` 从手统一换成股，`amount` 为元。

## 3. 免费渠道（A 股与通用）

免费渠道不需要凭据，但会被限流或反爬，所以都带随机休眠、重试和熔断。配置多个比依赖一个稳。

### 3.1 Efinance（东方财富）

东财爬虫封装，默认优先级 0（配置了 Tushare Token 时 Tushare 排在它前面）。每次请求前随机休眠 1.5–3 秒。`EFINANCE_CALL_TIMEOUT`（默认 30 秒）避免东财不可达时无限挂起。全量拉取容易被封，不想用可设 `PREFETCH_REALTIME_QUOTES=false` 关闭实时预取。

### 3.2 AkShare

默认优先级 1。实时和基本面可走东财、新浪、腾讯多个后端，请求前随机休眠 2–5 秒并轮换 User-Agent。覆盖 A 股、港股、美股和指数，也是业绩预告 / 快报、机构持股、板块榜等数据的主要来源。

### 3.3 Pytdx（通达信）

默认优先级 2，直连通达信行情服务器，无配额；多服务器自动切换、断线重连。内网或自建服务器可指定：

```bash
PYTDX_HOST=192.168.1.100
PYTDX_PORT=7709
# 或多个：PYTDX_SERVERS=ip1:port1,ip2:port2
```

### 3.4 Baostock（证券宝）

默认优先级 3，无配额，需要登录 / 登出，项目用上下文管理器管理连接生命周期。适合作为稳定的日线备用。

### 3.5 YFinance

默认优先级 4。美股、港股、日股（`7203.T`）、韩股（`005930.KS`）、台股的主要来源，也是 A 股所有国内渠道都失败后的最后保障。日股 / 韩股只走 YFinance。看美股时可以设 `YFINANCE_PRIORITY=0` 让它优先。

### 3.6 Tencent（腾讯财经）

默认优先级 5，普通 A 股日线的最终兜底；同时是**已登记指数固定链的第一位**。实时行情里的 `tencent` 有量比 / 换手率 / PE / PB，单股查询稳定，是免费实时源的推荐首选。

## 4. 港股与美股渠道

### 4.1 Futu OpenD

只读港股行情 / 日线 / 基本面，**不调用任何交易接口**。需要本机或可信网络里运行 OpenD：

```bash
FUTU_OPEND_HOST=127.0.0.1
FUTU_OPEND_PORT=11111
# 港股实时顺序，逗号分隔；失败自动回退
FUTU_HK_REALTIME_SOURCE_PRIORITY=futu,longbridge,akshare,yfinance
```

锁定的 `futu-api` 只支持 IPv4 地址；跨主机连接只用于受信网络或本机端口转发。持仓导入（`FUTU_ACC_ID` 等）见 [完整指南](full-guide.md) 的 Futu 小节。

### 4.2 Longbridge（长桥）

覆盖美股和港股，可补齐 YFinance 缺的换手率、PE、总市值和量比。凭据二选一：

```bash
# OAuth（推荐）
LONGBRIDGE_OAUTH_CLIENT_ID=...
# headless 环境（Actions / Docker）：把 ~/.longbridge/openapi/tokens/<client_id> 做 base64 后放这里
LONGBRIDGE_OAUTH_TOKEN_CACHE_B64=...

# 或 Legacy
LONGBRIDGE_APP_KEY=...
LONGBRIDGE_APP_SECRET=...
LONGBRIDGE_ACCESS_TOKEN=...
```

未配置凭据时不会实例化。遇到 `client is closed` 等连接关闭类异常会进入冷却（`LONGBRIDGE_CONNECTION_COOLDOWN_SECONDS`，默认 15 秒），期间美股 / 港股自动退回 YFinance / AkShare。其余 `LONGBRIDGE_*` 变量见 [完整指南](full-guide.md) 和官方文档。

### 4.3 Finnhub / AlphaVantage（美股补充）

```bash
FINNHUB_API_KEY=...        # 免费档约 60 次/分钟
ALPHAVANTAGE_API_KEY=...   # 免费档 25 次/天、5 次/分钟
```

只做美股补充：在已有主源报价之后补字段或参与日线链，**不能**在 Longbridge / YFinance 全部失败时独立返回实时报价。AlphaVantage 免费额度很小，不建议作为主力。

## 5. 补充与可选渠道

### 5.1 妙想（东方财富 MX_API）

严格 opt-in 的第三方能力：需自备 Key，限时免费 / 会员配额策略可能调整，不承诺长期免费。未配置时系统行为不变。

```bash
MX_APIKEY=你的key
MX_PRIORITY=6
```

只在东财公开接口被限流或失败时兜底获取**筹码分布**和**个股资金流**，不参与日线。同一进程内共享请求准入锁：已有请求未结束时，新查询直接跳过，不排队。GitHub Actions 需把 `MX_APIKEY` 配成 Secret。

### 5.2 台湾三大法人

政府开放资料（TWSE T86、TPEx），无需 Key，只有数据层，**尚未接入报告、Web 或评分链路**，也不影响 A 股 / 港股 / 美股流程。

### 5.3 FXMacroData

宏观 / 外汇数据，给助手和报告 Agent 用。公开的 USD 目录和近 90 天历史无需密钥；授权数据配 `FXMACRODATA_API_KEY`。详见 [FXMacroData 说明](FXMACRODATA.md)。

### 5.4 东财接口补丁

东财 / Efinance / AkShare 的东财接口频繁出现 `RemoteDisconnected`、连接被关闭时可开启：

```bash
ENABLE_EASTMONEY_PATCH=true
```

会注入 NID 令牌和随机 User-Agent，降低被东财限流的概率。默认关闭。

### 5.5 东方财富统一限速（默认开启）

东方财富按 IP 风控：每秒超过约 5 次、同一 IP 10 个以上并发、每分钟约 200 次就会临时封 IP，不同子域名（`push2`、`push2his`、`datacenter-web` 等）分开封。本进程内所有发往 `*.eastmoney.com` 的请求（AkShare、efinance、选股、热点、资讯库）都经过 `data_provider/eastmoney_gate.py`：

- 同一子域名一次只发一个请求，两次请求至少间隔 `EASTMONEY_MIN_INTERVAL_SEC` 秒（默认 1.0），另加 0.1–0.5 秒随机抖动；不同子域名互不影响。
- 某子域名返回 HTTP 403（封禁信号）后不重试，暂停 `EASTMONEY_BAN_COOLDOWN_MINUTES` 分钟（默认 30）；期间对它的请求直接抛 `EastmoneyCoolingDown`（属于 `requests` 的连接错误），各处现有的回退逻辑会换用其他来源。
- 在子进程里运行的 AkShare 东财函数（如 `stock_zh_a_hist`、`stock_zh_a_spot_em`）整个调用期间占住对应子域名。
- 设置 →「数据状态」里有「东方财富（统一限速）」一行：显示是否有子域名在冷却（warnings 里写明子域名和恢复时间），两个配置项可以直接修改，保存后立即生效。
- 选股原有的 `SCREENING_EASTMONEY_MIN_INTERVAL_SEC` / `SCREENING_EASTMONEY_JITTER_SEC` 仍作用于选股自己的东财请求，排队和 403 冷却与全局共用。
- 与 5.4 的东财接口补丁可以同时开启，两者叠加。

## 6. 优先级与回退规则

**A 股日线**：`DataFetcherManager` 按优先级数字从小到大尝试，单个渠道失败就换下一个，连续失败会短期熔断。默认顺序：

```text
tushare(-1，仅配置 Token 后) > efinance(0) > akshare(1) > tushare/pytdx/tickflow(2) > baostock(3) > yfinance(4) > tencent(5)
```

用 `*_PRIORITY` 调整。这些设置只管普通 A 股日线。

**已登记的沪深指数**（用 `sh000016`、`000016.SH` 这类显式写法时）：固定按 `Tencent → AkShare → TickFlow → YFinance`，不读 `*_PRIORITY`；裸 `000016` 仍按股票处理。

**A 股实时行情**由 `REALTIME_SOURCE_PRIORITY` 决定，可选 `tencent`、`akshare_sina`、`efinance`、`akshare_em`、`tickflow`、`tushare`、`biying`。

| 你的配置 | 实际默认顺序 |
| --- | --- |
| 什么都没配 | `tencent, akshare_sina, efinance, akshare_em` |
| 只配 `TUSHARE_TOKEN` | `tushare, tencent, akshare_sina, efinance, akshare_em` |
| 只配 `BIYING_LICENCE` | `biying, tencent, akshare_sina, efinance, akshare_em` |
| 两者都配 | `tushare, biying, tencent, akshare_sina, efinance, akshare_em` |
| 显式设置 `REALTIME_SOURCE_PRIORITY` | 完全按你写的，不再自动注入 |

**港股 / 美股 / 日韩台**：港股实时按 `FUTU_HK_REALTIME_SOURCE_PRIORITY`；美股以 Longbridge / YFinance 为主链，Finnhub / AlphaVantage 只补充；日股 / 韩股只走 YFinance。各场景的失败降级见 [数据源稳定性与故障处理图示](data-source-stability.md)。

## 7. 配置方式与安全

- **本地**：写进项目根目录 `.env`（参考 `.env.example`），或在 Web 设置页填写（保存前有草稿保护，见 [设置页草稿保护](settings-draft-protection.md)）。
- **Docker**：通过环境变量或挂载 `.env`。
- **GitHub Actions**：Token / Key 放 **Secrets**；优先级、复权、批量开关这类非敏感项可放 **Variables**。`00-daily-analysis.yml` 目前映射了 `TUSHARE_TOKEN`、`TUSHARE_HTTP_URL`、`TICKFLOW_*`、`LONGBRIDGE_*`、`MX_APIKEY`、`FXMACRODATA_API_KEY`。**`BIYING_LICENCE` 和 `BIYING_TIMEOUT_SECONDS` 尚未映射**，所以每日任务在 Actions 里不会使用必盈；要用，需要先改 workflow（属于工作流改动，单独评估）。新增渠道变量时同步更新 workflow。
- **不要提交**：Token、Key、证书、OAuth 缓存、Futu 账户 ID。不要写进代码、fixture、日志或文档。
- **最小可用**：什么都不配也能跑（免费渠道）。想更稳，优先加 `TUSHARE_TOKEN`；要盘中数据和历史分时，再加 `BIYING_LICENCE`。

## 8. 限流与测试约束

东方财富、同花顺、Tushare 等外部接口容易被频控或封禁，所以：

- 测试默认在 HTTP / SDK 边界 mock，**不要**用脚本、循环、并发或反复重跑去高频访问真实数据源。
- 确实需要真实验证时，同一任务、同一接口最多做一次最小请求（先检查 SDK 是否内部分页、并发或重试），之后用脱敏样本或 mock。
- `network` 标记的测试和 smoke 脚本不豁免这条限制；不要通过换 IP、代理或账号来规避频控。

## 9. 常见问题速查

| 现象 | 常见原因 | 处理 |
| --- | --- | --- |
| 启动日志提示 Tushare Token 未配置 | 没填 `TUSHARE_TOKEN` | 填写后重启；不填则自动用免费渠道 |
| 实时行情总是落到免费源 | Tushare 未开通 `rt_k`，或没配必盈 | 开通 RT 权限，或配置 `BIYING_LICENCE`；冷却期 10 分钟后会重试 |
| 港股数据查不到 | Tushare 账号没有港股接口权限 | 配置 Futu / Longbridge，或依赖 AkShare / YFinance |
| 双击日 K 提示“分时读取失败” | 页面上的说明对应 503 的 `reason`：提示配置必盈 licence 是 `biying_not_configured`；没有这天的分时是 `no_bars`（停牌、非交易日）；请求失败是 `source_failed`（证书无效、网络）；非 A 股或指数不提供 | 按提示配置有效证书；确认是 A 股个股 / ETF 的交易日；看后端日志里的 `分时行情不可用 ... (reason)` |
| 日志里出现 `Biying HTTP 4xx/5xx` | 证书失效、超出频率或服务端异常 | 检查证书和额度，必要时调大 `BIYING_TIMEOUT_SECONDS` |
| 东财类接口 `RemoteDisconnected` | 被东财限流 | 开启 `ENABLE_EASTMONEY_PATCH`，降低 Efinance 优先级，或配置 Tushare / TickFlow |
| 美股日线 / 实时失败 | Longbridge 冷却中，或 YFinance 被限流 | 等冷却结束，配置 Longbridge 凭据 |
| 配置了 TickFlow 但实时没用它 | 实时默认不含 `tickflow` | 在 `REALTIME_SOURCE_PRIORITY` 里加入 `tickflow` |

## 10. 相关文档

- [数据源稳定性与故障处理图示](data-source-stability.md)
- [数据中心：Provider 能力与运行时优先级](data-center-diagnostics.md)
- [市场支持与边界](market-support.md)
- [完整配置与部署指南](full-guide.md)
- [Tushare 股票列表指南](TUSHARE_STOCK_LIST_GUIDE.md)
- [FXMacroData 说明](FXMACRODATA.md)
