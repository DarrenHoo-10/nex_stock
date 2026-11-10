# 多用户模式设计（草案）

状态：待确认。确认后分三期实现，每期单独一个 PR。

使用范围：只开放给内部少量客户，账号由 NexorAI 用户中心管理，不做配额。

## 1. 原则与已确认的决策

原则：

- **数据能共享的一律共享**，只有和个人账户相关的数据按用户隔离
- **设置除进程级基础设施外都可以按用户配置**，包括模型与 API Key、数据源 Token、搜索 Key 和推送渠道
- **模型和搜索必须自带 Key**：普通用户不能使用管理员的模型 Key 和搜索 Key
- **一个人只影响自己**：任何用户的配置、操作和故障，都不能改变其他用户看到的数据、占用其他用户的资源，或让其他用户的密钥失效。各节的落实方式汇总在第 9 节

| 类别 | 内容 | 归属 |
| --- | --- | --- |
| 市场数据 | 行情、日线、分钟线、板块目录与成分、板块日线、新闻情报、基本面快照 | 共享 |
| 系统产出 | 板块信号与推荐、大盘复盘、市场上下文、筛选结果、回测、决策信号 | 共享 |
| 个股分析报告 | `analysis_history` | 共享，记录发起人 |
| 个人数据 | 自选股、次日计划（现金、持仓、计划结果）、持仓账户（Portfolio）、关注板块、提醒规则、Agent 对话、助手笔记 | 按用户隔离 |
| 个人设置 | 见第 3 节 | 按用户 |
| 进程级设置 | 见 3.2 | 全站一份，仅管理员可改 |
| 用量 | `llm_usage` | 按用户记录，不限额 |

本期不做：

- 用户注册与管理：只使用用户中心提供的 `userId` 和 `role`
- 飞书、钉钉、Discord 机器人的多用户映射：一个进程只能连一个 Bot 应用，机器人以“主用户”身份运行（见 2.3）
- MCP 服务的多用户映射：同样以主用户身份运行

## 2. 身份

### 2.1 当前用户

新增 FastAPI 依赖 `get_current_user()`，返回 `CurrentUser(user_id, handle, display_name, role, is_admin)`：

- `AUTH_MODE=user_center`：取自中间件挂在 `request.state.user_center_session` 上的会话，`user_id` 就是用户中心的 `userId`
- `AUTH_MODE=local`：固定返回主用户（`PRIMARY_USER_ID`），角色为 admin。本地单人部署和桌面端的行为不变

所有带用户数据或用户设置的 service 方法改成显式接收 `user_id`（或第 3 节的用户配置视图），不读全局状态，也不从线程上下文隐式获取。这样定时任务、CLI 和 API 走同一套调用方式。

### 2.2 用户登记表

用户中心不提供用户列表，定时任务又需要知道有哪些用户，所以新增 `app_user` 表：

| 字段 | 说明 |
| --- | --- |
| `user_id` | 主键，用户中心 `userId` |
| `handle` / `display_name` / `role` | 最近一次登录时的快照 |
| `first_seen_at` / `last_seen_at` | 首次 / 最近访问时间（最近访问时间按分钟节流更新） |

中间件校验通过后 upsert 这张表。

### 2.3 主用户

新增配置项 `PRIMARY_USER_ID`（默认 `local`）：

- 已有的全部历史数据迁移后归主用户所有
- 主用户的个人设置就是 `.env` 里的现有值，不写个人层；在设置页上改主用户的设置，就是改 `.env`，保持现状
- CLI（`main.py`）、GitHub Actions、MCP 服务、机器人，以及没有用户上下文的旧入口，都以主用户身份运行
- 切到 `user_center` 模式时，把它设成管理员自己在用户中心的 `userId`

主用户的自选股继续以 `.env` 的 `STOCK_LIST` 为准，保证 CLI 和 Actions 不受影响。其他用户的自选股存在数据库的 `user_watchlist` 表。两者由一个 `WatchlistService` 统一读写，调用方不区分。

### 2.4 权限

新增依赖 `require_admin()`。以下接口改为仅管理员可用：

- 进程级设置的读写（3.2）
- 服务启停、数据中心维护任务
- 全站用量视图（普通用户只能看自己的）

开放给内部客户时，把 `USER_CENTER_REQUIRED_ROLE` 改为 `user`。

## 3. 设置

### 3.1 两层结构

- **系统层**：`.env`。既是主用户的设置，也是普通用户非密钥类设置的默认值
- **个人层**：新表 `user_setting(user_id, key, value, is_secret, updated_at)`，主键 `(user_id, key)`

某个用户的生效配置 = 系统层，叠加该用户在个人层设置过的键，再按 3.3 去掉不允许回退的密钥。新增 `get_user_config(user_id)`，返回一个只读的配置视图，接口与现有 `Config` 一致。次日计划等服务本来就从传入的 `config` 读取参数（如 `PositionRules.from_config(config)`），所以主要工作是把传入的对象换成这个视图。

每个配置项是“用户级”还是“进程级”，在 `src/core/config_registry.py` 的字段定义里加一个 `scope` 属性来标记，作为唯一真源。个人设置接口、配置视图和设置页都从这里读，不另外维护名单。

### 3.2 进程级设置（不能按用户区分）

| 分组 | 示例 | 原因 |
| --- | --- | --- |
| 运行与部署 | `WEBUI_HOST/PORT`、`LOG_LEVEL/LOG_DIR`、`DEBUG`、`HTTP_PROXY`、`MAX_WORKERS`、各类并发上限、调度器超时、`LLM_USAGE_HMAC_SECRET` | 整个进程只有一份 |
| 认证 | `AUTH_MODE`、`PUBLIC_ORIGIN`、`USER_CENTER_*`、`TRUST_X_FORWARDED_FOR`、`ADMIN_AUTH_ENABLED` | 用来判断“你是谁”，不能由用户自己决定 |
| 机器人应用 | 飞书、钉钉、Discord、Slack 的 Bot App 凭据和长连接开关 | 一个进程只连一个 Bot |
| 本机守护进程 | `FUTU_OPEND_HOST/PORT`、`PYTDX_HOST/PORT/SERVERS` | 连接的是服务器本机或局域网上的进程 |
| 共享任务参数 | 板块流水线、新闻采集、行情同步、大盘复盘的开关和参数 | 产出全站共享，参数只能有一份 |
| 数据口径 | 数据源优先级（`*_PRIORITY`）、复权方式（如 `TICKFLOW_KLINE_ADJUST`）、实时行情与筹码开关等改变数据内容的项 | 取到的数据进共享缓存，口径必须全站一致（见 3.4） |

代码里还有上游项目带的本机 CLI 生成后端（`codex_cli` 等，实验性 opt-in），当前部署不使用；普通用户的设置页不显示这些选项。

### 3.3 用户级设置与回退规则

| 分组 | 内容 | 用户未配置时 |
| --- | --- | --- |
| 模型 | `LLM_CHANNELS`、各家 API Key 与 Base URL、主模型 / 轻量模型 / 回退模型、Agent 模型、温度、推理强度 | **不回退**：不能发起模型分析和 Agent 对话 |
| 搜索 | Tavily、SerpAPI、Brave、Bocha、Anspire、MiniMax 等搜索 Key，SearXNG 地址 | **不回退**：分析时跳过实时搜索，只用共享的新闻情报池 |
| 数据源凭据 | `TUSHARE_TOKEN`、`TICKFLOW_API_KEY`、`BIYING_LICENCE`、`MX_APIKEY` 等。只有凭据，不含优先级和口径 | 回退到系统层；免 Token 数据源照常可用 |
| Agent | 技能选择、路由、上下文压缩、助手记忆（含 Hindsight 地址和 Key） | 非密钥项回退到系统默认值；密钥不回退。使用系统层的 Hindsight 时，每个用户用独立的记忆库（bank） |
| 推送 | 推送渠道（第 5 节）、`DAILY_PLAN_ENABLED`、计划生成时间、`PREOPEN_PUSH_ENABLED` / `PREOPEN_PUSH_TIME` | 渠道不回退：没配就不推送；开关和时间回退到系统默认值 |
| 仓位规则 | `POSITION_*` | 回退到系统默认值 |
| 报告偏好 | `REPORT_TYPE`、`REPORT_LANGUAGE`、`REPORT_SUMMARY_ONLY`、涨跌配色 | 回退到系统默认值 |
| 自选股 | `STOCK_LIST` 对应的 `user_watchlist` | 为空 |

回退规则统一为：**密钥类配置不回退，非密钥类配置回退到系统默认值**。数据源 Token 是唯一的例外，允许回退，因为它取到的数据本来就进共享缓存（3.4）。

### 3.4 用户的数据源 Token

- 共享任务（板块流水线、行情同步、新闻采集）始终使用系统层的 Token
- 用户自己触发的分析，用该用户的凭据取数；取到的数据写入全站共享的行情缓存，其他人可以复用
- 用户只能提供凭据，不能改数据源优先级和复权等口径（3.2）。所以不管用谁的 Token，写进缓存的数据和用系统 Token 取到的完全一致，一个用户的配置改变不了别人看到的数据
- 用户 Token 的频控和额度由用户自己承担；某个用户的 Token 失败，只在他自己这次取数里按现有 fallback 链降级，不影响其他用户

### 3.5 密钥的存储与使用

个人层会保存用户付费的模型 Key，所以密钥必须加密存储，并按用户隔离：

- **每个用户一把独立的数据密钥**：首次保存密钥时为该用户随机生成，只用来加密这个用户的密钥项。用户之间的密文互不相关：某个用户的记录损坏、密钥轮换或被删除，只影响他自己。删除用户时销毁他的数据密钥即可，不用逐条清理
- **根密钥只用来包裹各用户的数据密钥**：定时任务要在用户离线时用他的 Key，所以服务器必须能解密，必然存在一把服务器持有的根密钥。根密钥默认在首次启动时自动生成，存在数据目录里、和数据库放在一起（权限 600），不需要新增必填配置，也不会因为没配置而拒绝启动。需要把根密钥和数据库分开存放时，可以用可选配置项 `USER_SECRET_KEY` 覆盖
- **根密钥丢失属于运维事故**：只有数据目录丢失或被误删才会发生，这种情况下数据库本身也一起丢了。普通用户的任何操作都碰不到根密钥。备份数据库时一并备份根密钥文件
- `local` 模式没有个人层，不生成根密钥
- 接口返回时一律打码；更新时留空表示保持原值
- 日志、错误信息、诊断接口里不得出现明文。复用现有的脱敏逻辑
- 用户填写的 Base URL、SearXNG 地址、自定义 webhook 等，服务器会主动访问，必须只允许 `https` 和公网地址，防止 SSRF

### 3.6 设置页

- **个人设置**：3.3 里的全部分组。非密钥项显示“使用系统默认值”或本人设置值，可以一键恢复默认；密钥项显示“未配置”或打码后的值
- **系统设置**：3.2 里的全部分组，仅管理员可见。管理员自己的个人设置也在这里编辑，因为主用户的个人设置就是 `.env`
- 普通用户未配置模型 Key 时，首页和分析入口显示提示，引导去个人设置

## 4. 数据模型改动

| 表 | 改动 |
| --- | --- |
| `app_user`（新表） | 见 2.2 |
| `user_setting`（新表） | 见 3.1 |
| `user_data_key`（新表） | `user_id`（主键）、`wrapped_key`（用根密钥包裹后的数据密钥）、`created_at`；见 3.5 |
| `user_watchlist`（新表） | `user_id, code, position, created_at`；主用户不使用此表 |
| `user_notification_channel`（新表） | 见第 5 节 |
| `plan_account` | 由“固定 id=1 的单行”改为每用户一行，新增 `user_id`（唯一） |
| `plan_holding` | 新增 `user_id`；唯一约束 `code` 改为 `(user_id, code)` |
| `plan_run` / `plan_stock` | 新增 `user_id` 并加索引；同一股票同一交易日的计划按用户区分 |
| `portfolio_accounts` | 新增 `user_id`。现有的 `owner_id` 是前端自由填写的文本，保留原语义，不复用 |
| `portfolio_*` 子表 | 不加列，经 `account_id` 归属；查询入口统一校验账户归属 |
| `board_follow` | 新增 `user_id`；唯一约束 `board_ref` 改为 `(user_id, board_ref)` |
| `alert_rules` | 新增 `user_id`；触发、通知、冷却记录经 `rule_id` 归属 |
| `conversation_session_states` | 新增 `user_id`；消息与摘要经 `session_id` 归属，读取前校验会话归属 |
| `assistant_note*` | `assistant_note` 新增 `user_id`，唯一约束加上 `user_id`；修订与提案经 `note_id` 归属 |
| `analysis_history` | 新增 `requester_user_id`（只记录发起人，不参与过滤）和 `model_channel_fingerprint`（服务商 + Base URL + 模型名的摘要，用于第 7 节的复用比对） |
| `llm_usage` | 新增 `user_id` |

其余表（行情、板块、新闻、信号、筛选、回测等）不改。

迁移：

- 沿用 `storage.py` 现有的 `schema_migrations` 加列迁移方式，新增列的历史行一律回填为 `PRIMARY_USER_ID`
- 改唯一约束的三张表（`plan_holding`、`board_follow`、`assistant_note`）在 SQLite 下需要重建表：建新表、复制数据、改名
- 迁移前自动备份数据库文件；迁移在单个事务内完成，失败即回滚并拒绝启动

## 5. 个人推送渠道

新表 `user_notification_channel`：`id, user_id, channel_type, config_encrypted, enabled, created_at, updated_at`。渠道配置含 webhook 和 token，按 3.5 加密存储。

首批支持只需填一个 webhook 或 token、且用户能自己申请的渠道：

- 飞书机器人 webhook
- 钉钉机器人 webhook
- 企业微信机器人 webhook
- Telegram（bot token + chat id）
- PushPlus / Server 酱
- 邮件：用户填自己的 SMTP 发件账号和收件地址
- 自定义 webhook

实现方式：`NotificationService` 现在到处直接读 `get_config()`。改造为接收用户配置视图和渠道配置；主用户继续用 `.env`。报告渲染逻辑不动，只换发送目标和报告偏好。

提供“发送测试消息”接口。

## 6. 定时任务

| 任务 | 多用户下的行为 |
| --- | --- |
| 板块流水线、新闻采集、行情同步、大盘复盘 | 不变，全站只跑一次，用系统层的模型和 Token，结果共享 |
| 次日计划 | 每个用户按自己的开关和时间、用自己的模型 Key 生成，推给本人渠道 |
| 盘前推送 | 每个用户按自己的开关和时间生成和推送 |
| 提醒规则 | 按规则所属用户评估，推给该用户 |
| 旧的每日个股分析（`STOCK_LIST`） | 只给主用户，保持现状 |

调度方式：调度器为所有用户设置过的时间点去重后各注册一个任务；到点时运行时间匹配的用户。共享的市场上下文和板块数据在同一交易日内只计算一次，各用户的计划复用。

时间约束：次日计划依赖收盘后的板块流水线和行情数据。个人设置的计划时间必须晚于系统的数据就绪时间；早于这个时间的设置在保存时拒绝，并提示最早可选时间。盘前推送时间限制在开盘前。

没有模型 Key 的用户：跳过需要模型的步骤，运行记录标记为“未配置模型”。若配置了推送渠道，推一条提示，不重复推送。

用户之间并行执行，并发数有上限；每个用户的一次运行有独立的超时。某个用户的模型接口很慢或卡住，只会让他自己的这次运行超时，不会推迟其他用户的推送。单个用户失败（包括其 Key 失效或额度用完）只记录在该用户的运行记录上。

## 7. 共享报告

个股分析报告全员共享，但每人用自己的模型和报告语言。已有报告分两层复用：整份报告直接复用，或者只复用它当时采集的输入。

**直接复用**（返回已有报告，不调用模型，也不需要 Key）要求下面全部成立：

- 股票、交易日、模型渠道（服务商 + Base URL + 模型名）、报告语言、报告类型都相同。比对时包含 Base URL：用户可以自填接口地址，只比模型名的话，某个用户接到非官方接口生成的报告会被别人当成同名模型的结果复用
- **有效策略集相同**。策略集不是“请求里有没有写 `skills`”，而是这次分析实际会用的那一组：请求的 `skills`，没有就用发起人自己的 `AGENT_SKILLS`（个人设置），再没有就用系统默认策略。已有报告生成时用的是另一组、或者没有记录策略（旧报告），就不能直接复用
- 本次请求不带持仓 / 仓位上下文；已有报告是共享的（`is_private` 为假）
- 没有 `force_regenerate` / `force_refresh`，没有指定非 `auto` 的分析阶段
- 该股票所在市场不在交易时段（报告里含实时行情）
- 主用户要推送通知的请求（默认请求）不直接复用：复用的报告没有新内容可推，也保持主用户每次点分析都重新生成的现有行为

**重新分析**：任何一项不满足（持仓、另一组策略、另一个模型渠道 / 语言 / 报告类型、强制重新生成……），就用发起人自己的 Key 生成新报告，新报告同样对所有人可见（带持仓的除外）。没有自己的 Key 的用户在这种情况下得到 `model_key_required`；只有当请求的每只股票都能直接复用时才放行。

**重新分析时复用已采集的输入**：同一只股票、同一交易日已经分析过时，新的分析不再重新向数据源和搜索取数，而是沿用上一次分析采集到的输入：行情（实时行情及其采集时间）、日线和技术指标（日线在共享表里，指标由它重新计算）、新闻 / 搜索结果、基本面（含资金流、板块归属）、筹码分布、大盘 / 市场结构。**不复用**上一次分析的结论、建议、目标价 / 止损价、评分、提示词，以及任何个人数据。

- 来源只取共享报告，或发起人自己的私有报告；绝不取别人的私有报告。来源里的持仓 / 组合字段（`portfolio_context`、`holdings` 等）在取用时再剔除一遍
- 股票所在市场处于交易时段时，实时行情重新获取，其余输入照常复用；来源里在交易时段采集的行情也不再复用
- 没有可用来源（没有同日报告、报告没有保存快照）就照常取数
- 强制重新生成 / 强制刷新要求最新数据，不复用输入
- 主用户要推送的请求仍然重新生成，但可以复用输入

**持仓不共享**（硬规则）：用持仓 / 仓位上下文生成的报告，或 Agent 运行里调用了个人数据工具（持仓快照）的报告，只属于发起人，永远不会被直接复用，也不会给别人看。存量报告里无法确定是否用过持仓的（P2b-4 之前的 Agent 模式报告，当时没有记录工具调用）一律按私有处理（迁移，见第 19 节）。

历史页默认显示所有人的（共享）报告，可以筛选“只看我发起的”。

说明：一个用户用自己的 Key 生成的报告，其他人可以免费看到。这是“报告共享”这个决策的直接结果。

## 8. Agent

- 对话会话归属于创建者；列表、读取、续聊都校验归属
- 对话使用本人的 Agent 模型和 Key；没有 Key 时不能发起对话，但可以查看自己的历史会话
- Agent 工具读取持仓、自选股、笔记时，必须用会话所属用户的 `user_id`，不能读主用户或全局数据
- 工具发起的搜索和取数使用本人的搜索 Key 和数据源配置

这是最容易漏的地方，需要逐个审查读取用户数据和使用 Key 的工具，并补越权测试。

## 9. 一个人只影响自己

| 可能互相影响的地方 | 处理方式 |
| --- | --- |
| 用户的数据源配置写进共享缓存 | 用户只能提供凭据，优先级和口径是进程级的（3.2、3.4） |
| 一把密钥丢失或损坏，所有人的 Key 都失效 | 每个用户一把独立的数据密钥；根密钥与数据库同存同备份，普通用户碰不到（3.5） |
| 别人自填接口生成的报告被当成同名模型复用 | 复用比对包含 Base URL（第 7 节） |
| 一个用户一次提交大量股票，占满分析线程 | 分析任务按用户排队、轮流取任务；每个用户同时运行的任务数有上限（新增配置项，默认 1）。全站并发上限 `MAX_WORKERS` 不变 |
| 一个用户高频触发，导致服务器 IP 被东方财富等免费数据源封禁，所有人都取不到数 | 上面的“每用户并发上限”同时限制了取数速度；行情先查共享缓存，命中就不请求外部接口 |
| 一个用户的模型接口卡住，推迟别人的定时推送 | 定时任务按用户并行，每人独立超时（第 6 节） |
| 一个用户的推送渠道失败 | 只记录在他自己的运行记录上 |
| 助手记忆共用一个库 | 每个用户独立的记忆库（3.3） |
| 改动影响全站的设置 | 进程级设置只有管理员能改（2.4） |

## 10. 接口与前端

- 接口路径和请求字段保持不变，数据范围改由登录身份决定。客户端不需要也不能传 `user_id`
- 越权访问别人的资源（如持仓账户 id、提醒规则 id）一律返回 404，不返回 403，避免泄露资源是否存在。例外是 Agent 会话：缺失的会话 id 本来就返回 200（空结果），所以读取、删除、查看上下文、压缩别人的会话时返回与缺失 id 完全相同的响应（见第 15 节）
- 新增 `GET /api/v1/me`：返回当前用户、`is_admin`，以及模型 Key 是否已配置，前端据此显示入口和提示
- 新增个人设置接口：`GET/PUT /api/v1/me/settings`、`/api/v1/me/notification-channels`，以及模型和推送的连通性测试接口
- 页头显示当前用户；用量页对普通用户只展示本人数据
- 桌面端走 `local` 模式，行为不变

## 11. 分期

| 期 | 内容 | 用户可见变化 |
| --- | --- | --- |
| P1 身份与地基 | `CurrentUser`、`app_user`、`PRIMARY_USER_ID`、根密钥与每用户数据密钥、配置项 `scope` 标记、`user_setting` 与配置视图、管理员接口守卫、`/api/v1/me`、全部表的 `user_id` 迁移与回填 | 单用户部署无变化；普通用户还不能进 |
| P2 私有数据与个人设置 | 个人数据按用户隔离；模型、搜索、数据源、Agent、仓位、报告偏好进入个人设置；分析、Agent 和取数改用用户配置视图；分析任务按用户排队与并发上限；共享报告复用规则；用量按用户统计；设置页拆分 | 可以把 `USER_CENTER_REQUIRED_ROLE` 改为 `user`，内部客户配好自己的 Key 后可用，但还收不到推送 |
| P3 推送与定时 | 个人推送渠道、推送开关和时间、按用户的次日计划和盘前推送、提醒按用户推送 | 内部客户收到自己的推送 |

每期都需要同步 `.env.example`、`docs/CHANGELOG.md` 和对应专题文档；P2 和 P3 改动 Web 页面，PR 需附截图。

## 12. 风险

- **越权**：任何一个读取个人数据的入口漏了 `user_id` 过滤，就会泄露别人的数据。每张私有表都要有“用户 A 读不到用户 B 的数据”的测试，覆盖 API、Agent 工具和定时任务三条路径
- **Key 误用**：任何一处仍直接读全局 `get_config()` 去调模型或搜索，就会用管理员的 Key 替普通用户付费，同时忽略个人设置。需要逐个检查分析流水线、次日计划、推送、报告渲染和 Agent 的配置读取点，并加测试：普通用户未配 Key 时，任何入口都不会用到系统层的模型 Key
- **密钥泄露**：个人层保存了用户的付费 Key，按用户独立加密。只拿到数据库文件解不开；同时拿到数据库和根密钥文件才能解开，所以两者的访问权限要和数据目录一致收紧
- **跨用户影响漏网**：第 9 节逐项列出了已知的影响点。实现时每项都要有测试，例如：用户 A 提交 50 只股票时，用户 B 的单只分析仍能立即开始
- **SSRF**：用户可填的 Base URL 和 webhook 地址会被服务器主动访问，必须校验
- **迁移**：SQLite 重建表有数据风险。迁移前自动备份；在生产库的副本上先完整跑一遍迁移
- **主用户双数据源**：主用户的自选股和设置在 `.env`，其他用户在数据库，`WatchlistService` 和 `get_user_config` 必须是唯一读取入口

## 13. 回滚

- 代码回滚：P1 只加列、不删列，旧代码会忽略新列，可以直接回到旧版本
- 唯一约束重建的三张表：旧代码在多用户数据下可能触发唯一约束冲突，回滚时需要用迁移前的备份恢复
- `AUTH_MODE` 改回 `local` 或 `USER_CENTER_REQUIRED_ROLE` 改回 `admin`，即可立即关闭普通用户入口

## 14. P1 实现说明（补充与差异）

实现 P1 时对上文的补充，其余按上文执行：

- **新增列允许为空**：`user_id`、`requester_user_id` 等在 SQLite 里用 `ALTER TABLE ADD COLUMN` 添加，不能带 `NOT NULL`；新写入的行由 ORM 的 Python 侧默认值取 `PRIMARY_USER_ID`。回滚到旧版本后写入的无归属行，在再次升级启动时会自动补成 `PRIMARY_USER_ID`。`plan_account` 不重建表，只在 `user_id` 上建唯一索引。
- **`PRIMARY_USER_ID` 必须在首次迁移前设好**：迁移按当时配置的值回填历史数据，之后再改不会移动已回填的数据（生产在 `AUTH_MODE=user_center` 下，应先填管理员的 `userId` 再升级）。迁移前的备份文件位于数据库旁，名为 `<数据库文件名>.pre-multi-user-<时间>.bak`。
- **`PRIMARY_USER_ID`、`USER_SECRET_KEY` 不进配置字段注册表**：它们只能在 `.env` 修改，列入 `WEB_SETTINGS_HIDDEN_FROM_UI`，作用域按“未登记即 system”处理。
- **个人设置服务拒绝主用户**：主用户的个人设置就是 `.env`，向 `user_setting` 写主用户会被拒绝（错误码 `primary_user_uses_env`），因此单人部署不会生成根密钥。
- **不回退的补充**：除 3.3 列出的模型和搜索外，`STOCK_LIST`（主用户的自选股在 `.env`）和 `HINDSIGHT_BANK_ID`（共用记忆库会混用户数据）也不回退；除 `NOTIFICATION_*` / `REPORT_*` / `SINGLE_STOCK_NOTIFY` / `MERGE_EMAIL_NOTIFICATION` 这些偏好外，推送渠道的 webhook、地址、收件人等一律不回退。
- **`LITELLM_CONFIG` 是 system 作用域**（服务器本机文件路径，用户不能指定），但在非主用户的配置视图里置空，不继承系统层的渠道定义。非主用户的视图固定 `GENERATION_BACKEND=litellm`，`AGENT_BACKEND` / `AGENT_GENERATION_BACKEND` 不允许本机 CLI 或 Codex，实时行情优先级等由数据口径决定的派生值取系统层。
- **管理员守卫的范围**：`/api/v1/system/*`、`/api/v1/usage/*` 整体仅管理员可用；另有资讯源管理与抓取（`/api/v1/intelligence/sources*`）、`PUT /api/v1/boards/thresholds`、`POST /api/v1/boards/recommendations/refresh`、`POST /api/v1/decision-signals/outcomes/run`、`POST /api/v1/backtest/run`、`POST /api/v1/analysis/market-review`。服务启停在 MCP 管家进程里，不经过 FastAPI。

## 15. P2a 实现说明（个人数据隔离，补充与差异）

P2a 只做后端的个人数据隔离，不含个人设置接口、用户配置视图接入、分析队列、报告复用、用量、推送渠道和按用户调度。与上文的差异：

- **用户绑定方式**：`PortfolioService`、`DailyPlanService`、`AlertService`、`AssistantNoteService` 在构造时绑定 `user_id`，`None` 表示主用户（构造时解析一次，供 CLI、机器人、MCP 和旧入口使用）；仓库层所有个人数据方法都要求显式传入 `user_id`。接口一律由 `Depends(get_current_user)` 取用户。
- **`assistant_note_proposal` 新增 `user_id`**：提议没有 `note_id`（只记录 scope + subject），无法经笔记归属，所以单独加列。迁移记录为 `2026-11-09-multi-user-p2a-note-proposal-user-id`，与 P1 迁移分开，历史行回填 `PRIMARY_USER_ID`；每日主动提议上限按用户计数。
- **`user_watchlist` 多一个自增 `id`** 作为主键，`(user_id, code)` 唯一。
- **会话归属**：以 `conversation_session_states.user_id` 为准；只有消息、没有状态行的历史会话归主用户。读取、删除、查看上下文、压缩（`GET/DELETE /agent/chat/sessions/{id}`、`/context`、`/compact`）别人的会话，返回与不存在的会话 id **完全相同**的响应（200，空消息 / `deleted: 0` / 空上下文 / `nothing_to_compact`），不再返回 404：缺失的 id 本来就是 200，单独对别人的 id 返回 404 会泄露这个 id 存在。实现上别人的 id 改读一个不会存在的临时 id（`api/v1/endpoints/agent.py` 的 `_read_session_id`），响应里回显的仍是请求的 id。续聊别人的会话（`POST /agent/chat`、`/chat/stream`）仍是 `session_not_found`，因为缺失的 id 在这里会被创建，两种情况无法同响应；彻底消除需要把会话 id 按用户命名空间化，留给后续期。
- **机器人会话 id 保留**：机器人以主用户身份写 `{platform}_{user_id}[:scope]`（`bot/commands`）和 `feishu:{chat_id}[:thread]`（飞书助手）形式的会话 id。非主用户经 `POST /agent/chat`、`/chat/stream` 指定这类 id 会提前占住机器人之后要用的会话，所以返回 400 `validation_error`。保留的平台前缀集中在 `src/agent/conversation.py` 的 `BOT_SESSION_PLATFORMS`（`feishu`、`dingtalk`、`discord`、`wecom`、`telegram`），`is_bot_session_id` 判断；新增机器人平台时在这里加，测试会核对 `bot.models.Platform` 和各平台类。主用户不受限。
- **提醒 Worker**：评估所有用户的启用规则；非主用户规则的触发只落库，并写一条 `__no_channel__` 通知记录，不调用通知渠道。非主用户的去重键带 `user:<id>|` 前缀，冷却按 `rule_id`，用户之间不会互相抑制。`.env` 里的虚拟规则、大盘灯、持仓风险规则的兜底都归主用户。
- **系统任务读主用户的自选**：盘前推送、资讯池每日公告抓取经 `WatchlistService(...).list_codes(主用户)`；分析主流程（`main.py`、`pipeline`、调度的 claim）继续读主用户的 `config.stock_list`。资讯池的共享抓取在 P3 前不覆盖其他用户的自选。
- **Agent**：用户上下文显式经 `AgentRunRequest.user_id` / `AgentContext.user_id` / `ToolRegistry.execute(..., user_id=)` 传递；`ToolDefinition.user_scoped` 标记读取个人数据的工具，目前只有 `get_portfolio_snapshot`，缺少用户上下文时拒绝执行（`user_context_required`），不回退主用户。其余工具读取的都是共享数据。助手笔记注入读会话所属用户的笔记。
- **非主用户没有推送渠道**：次日计划运行对非主用户视为未配置推送——推送步骤标记为 `skipped`（`push.configured=false`，原因「个人推送渠道尚未开放」），不调用推送函数；计划预览照常存下，只在网页里看。与提醒 Worker 对非主用户规则的处理一致。持仓「手动分析」（`POST /portfolio/positions/{symbol}/analysis`）对非主用户以 `notify=False` 提交，不走全局通知渠道。个人推送渠道在 P3。
- **提醒规则按用户限量**：每个用户启用的规则数不超过 `ALERT_MAX_ENABLED_RULES_PER_USER`（默认 200，`.env`），创建、启用、PATCH 为启用时检查，超限返回 400 `rule_limit_exceeded`。Worker 每个用户各取最近更新的这么多条（`AlertRepository.list_all_enabled_rules(per_user_limit=)`，窗口函数按用户分区），规则展开后的运行时条数上限 `ALERT_WORKER_RULE_LIMIT` 也按用户计算，因此一个用户的大量规则不会把别人的规则挤出评估。已超限的存量规则不会被删除，但 Worker 只评估前 N 条。
- **孤儿提醒历史**：触发、通知、冷却记录经所属规则归属，P2a 之前删除规则不会级联删除这些记录，SQLite 又会复用已删除规则的 id，新规则会继承旧历史。P2a 迁移（备份之后、同一事务内）删除 `rule_id` 不存在于 `alert_rules` 的触发和冷却记录，以及 `trigger_id` 不存在于 `alert_triggers` 的通知记录；`rule_id` / `trigger_id` 为空的记录（`.env` 兼容规则、无触发的通知）属于主用户，保留。`delete_rule` 本身已级联删除这三类记录。
- **共享决策信号只能由管理员写**：`POST /decision-signals`、`POST /decision-signals/reassess`（会调用系统模型）、`PUT /decision-signals/{id}/feedback`、`PATCH /decision-signals/{id}/status` 使用 `require_admin`（403 `ADMIN_REQUIRED`），读接口对所有用户开放。持仓分析的 `portfolio_context` 带 `owner_user_id`，提取器对非主用户不写 `metadata.holding_state`（记为 `unknown`）。
- **板块推荐不含个人关注**：推荐是共享的系统产出，生成时不再读取任何用户的关注（以前把主用户关注的板块作为提示词参考，其他用户能由推荐结果反推）。用户自己的关注仍由 `GET /boards/follows` 和前端关注状态标记，读取推荐的响应没有变化；可见差异仅是模型少了关注这条参考。
- **已知缺口（留给后续期）**：分析主流程（`src/core/pipeline.py` → `build_agent_executor`）没有发起人，以主用户身份运行，`AGENT_MODE` 打开时非主用户发起的分析可能把主用户的持仓带进报告，需要在“分析接入用户配置”那一期解决；共享的 Hindsight 记忆库只对主用户开放，其他用户得到禁用的 recall provider；`POST /agent/chat/send` 对非主用户返回 `no_channels`；非主用户发起的持仓分析仍走全局任务队列：任务列表（`/analysis/tasks`）、同股票任务去重（409 `duplicate_task` 会暴露别人正在分析这只股票）、`/history` 里带持仓上下文的报告对其他用户的可见性都没有按用户隔离，决策信号的 `reason` / `risk_summary` 也可能引用该报告里的持仓语境。这些随“分析接入用户配置”一期一起解决（其中分析的模型 / 搜索配置、Agent 持仓读取和 Hindsight 记忆库在 P2b-1 已解决，见第 16 节；任务去重、列表、可见性仍留待后续）。
- **决策信号**：`GET /decision-signals` 的 `holding_only` 过滤使用当前用户的持仓；服务层调用时必须传 `holding_user_id`。

## 16. P2b-1 实现说明（模型 / 搜索 Key 隔离与个人设置，补充与差异）

P2b-1 做后端：个人设置接口、非主用户的模型与搜索只用自己的 Key、用量按用户、助手记忆按用户分库。不含分析队列公平性与每用户并发、任务列表 / SSE / 历史的可见性、报告隐私与复用、取数凭据接入抓取器、Web 页面、推送渠道、按用户调度。与上文的差异：

- **用户的配置视图带身份**：`get_user_config` 返回的只读视图通过 `Config.user_view_id` 标明所属用户（系统配置为 `None`）。凡是对非主用户发起的模型调用，都显式收到该视图，不使用线程上下文或全局切换；`src/llm/credentials_guard.py` 集中放守卫（`ModelKeyRequiredError`、`require_explicit_credentials`、`resolve_acting_user_id`）。
- **入口守卫**：`require_usable_model(config)`（`src/services/user_config_service.py`）在请求入口调用：非主用户没有可用模型 → 400 `model_key_required`；每个部署都必须带自己的 `api_key`（本地 Ollama 除外）；模型地址再做一次 SSRF 检查。守卫失败发生在任何 LiteLLM 调用之前。`ModelKeyRequiredError` / `UnsafeUrlError` 另由全局异常处理器统一映射为 400，防止深层抛出变成 500。
- **LiteLLM 边界的第二道防线**：所有直连 `litellm.completion` 的位置（分析器的回退分支、Agent 适配器的直连分支、图片提取、模型连通性测试）对非主用户要求调用参数里有显式 `api_key`，否则抛 `ModelKeyRequiredError`，不会让 LiteLLM 去读环境变量；Router 路径只含该用户自己的部署。
- **模型调用点分类**见 `docs/CHANGELOG.md` 同期条目与下表：

  | 调用点 | 类别 | 处理 |
  | --- | --- | --- |
  | 个股分析 `POST /analysis/analyze`（同步 / 异步）→ `AnalysisService` → `StockAnalysisPipeline` → `GeminiAnalyzer` / `build_agent_executor` / 搜索服务 | (a) | 入口守卫；任务记录 `user_id`；Worker 用发起人视图；流水线构造时校验 `user_id` 与视图一致；Agent 模式带 `user_id`；不走系统推送渠道 |
  | 持仓手动分析 `POST /portfolio/positions/{symbol}/analysis` | (a) | 同上 |
  | Agent 对话 `/agent/chat`、`/chat/stream`、`/research`、`/chat/sessions/{id}/compact`；`/agent/models`、`/agent/status` | (a) | 用本人视图；对话 / 研究 / 压缩要求可用模型；模型列表与状态只反映本人配置 |
  | Agent 搜索工具 `search_stock_news` / `search_comprehensive_intel` / `search_market_news` | (a) | `user_scoped`，用调用者的搜索服务 |
  | 次日计划手动运行 `POST /plan/runs`、按需计划的后台模型计划 | (a) | 入口守卫；`DailyPlanService.config` 取本人视图（模型与 `POSITION_*`）；没有模型时只出规则计划 |
  | 图片提取 `POST /stocks/extract-from-image` | (a) | 入口守卫；`extract_stock_codes_from_image(config=)` |
  | 助手笔记提议 `/agent/notes/proposals` | (a)，但不调用模型 | 提议由 Agent 在对话中产生，接口只存储；不设模型守卫，仅 `ASSISTANT_NOTE_*` 取本人视图 |
  | 导入解析 `/stocks/parse-import`、名称转代码 | (a)，不调用模型 | 无需改动 |
  | 助手记忆（Hindsight） | (a) | 见下 |
  | 选股运行 `POST /screening/screen*`、热点检索 `include_search` | (a)，无法按用户隔离 | 选股运行改为仅管理员；非管理员的热点检索忽略 `include_search`（它的运行时用进程级环境变量桥接系统模型和搜索 Key） |
  | 板块流水线 / 新闻分类 / 行情同步 / 大盘复盘 / 定时分析 / 板块推荐刷新 / 决策信号重评估 / 系统设置的模型测试 | (b)(c) | 系统配置，系统任务或仅管理员 |
  | 机器人、MCP、CLI | 主用户 | 不变 |

- **`user_id` 的传递约定**：`None` 等同主用户。API 在请求人是主用户时不向队列、服务、流水线额外传 `user_id`（调用与多用户之前完全一致）；只有非主用户才传，并同时传入其配置视图。
- **没有搜索 Key**：用户的搜索服务 `is_available == False`，流水线跳过实时搜索并继续用共享资讯池；Agent 搜索工具返回「没有可用的搜索引擎」。SearXNG 地址在每次构建搜索服务时重新做 SSRF 检查，不安全的实例被丢弃（用户只是少一个搜索渠道）。
- **用量归属**：`persist_llm_usage(..., user_id=)`；分析器和 Agent 按其配置视图的所属用户写入，主用户和系统任务不带 `user_id`，落为 `PRIMARY_USER_ID`。`GET /me/usage` 只返回本人行；`/usage/*` 仍仅管理员。
- **个人设置接口**：`src/services/me_settings_service.py` 只做读模型与连通性测试，写入、校验、密钥加密和缓存失效仍由 `UserSettingsService` 完成。`STOCK_LIST` 虽是用户级键，但自选股走 `/stocks/watchlist*`，设置接口拒绝它（`managed_elsewhere`）。主用户的字段 `source` 取 `user`（有值）或 `unset`，并带 `editable=false`。`POSITION_*`、`PREOPEN_PUSH_*` 等没有注册字段定义的用户级键与系统设置一样没有类型校验。
- **SSRF**：`src/utils/url_safety.py`；`UserSettingsService` 对名称以 `_URL` / `_URLS` / `_BASE_URL` / `_BASE_URLS` / `_API_BASE` 结尾的用户级键强制公网 https。调用前的再检查覆盖模型部署地址、`OPENAI_BASE_URL`、SearXNG、Hindsight；它缩小但不能完全消除 DNS 重绑定窗口（LiteLLM 自己再解析一次域名，无法固定 IP）。
- **助手记忆**：库名 `<HINDSIGHT_BANK_ID>-u-<sha256(user_id)[:16]>`；系统 Hindsight 上用户自己填的 bank id 无效；用户保存了自己的 `HINDSIGHT_BASE_URL` 才使用自己的服务（库名用其 `HINDSIGHT_BANK_ID`，未填则派生，前缀 `dsa`）。每个非主用户一个 `RecallService`（含各自的后台重试队列），删除会话的遗忘请求发往会话所属用户的库（`forget_documents(ids, user_id=)`）。
- **用户中心里的非主用户管理员**：「主用户」只由 `PRIMARY_USER_ID` 决定。角色为 admin 但不是主用户的账号，普通业务入口同样需要自带 Key；它们访问的仅管理员接口（大盘复盘、选股运行、系统设置等）按设计使用系统配置。
- **仍未覆盖（后续期；其中数据源凭据、任务去重 / 列表 / SSE / 历史可见性、报告复用与指纹、每用户并发已在第 17 节解决）**：US 社交舆情（`SOCIAL_SENTIMENT_API_KEY`）和宏观数据（`FXMACRODATA_API_KEY`）这类系统级第三方数据 Key 仍是系统层的，P2b-3 起非主用户的分析不再使用它们（第 18 节）；Agent 技能自动加权（`AGENT_SKILL_AUTOWEIGHT`）仍读系统值。

## 17. P2b-2 实现说明（队列公平、隐私、报告复用、取数凭据，补充与差异）

P2b-2 关闭 P2a / P2b-1 留下的跨用户缺口。主用户的行为尽量不变：下面标注了仅有的几处差异。

**公平队列（`src/services/task_queue.py`）**

- 每个任务有所有者：请求人，CLI / 机器人 / 系统任务 / 主用户的任务都是主用户（`task_owner`）。待办任务按所有者各一条队列，空闲线程取「最久没被服务」的那个所有者的第一个可启动任务（轮流），调度是“空闲时可用满”的：没有其他用户有可启动的任务时，一个用户可以用满全站线程；有人排队时，空出来的线程优先给运行数低于 `ANALYSIS_MAX_CONCURRENT_PER_USER`（默认 1）的用户。全站总线程仍是 `MAX_WORKERS`。大盘复盘 / 选股等后台任务占一个全站线程，但不计入该上限（也不会排在被封顶的分析任务后面）。
- **用户确认的取舍**：上限只在有人排队时生效，所以单独使用时主用户的批量分析并行度与以前相同（最多 `MAX_WORKERS`）；代价是一个人占满线程时，后来者要等其中一只股票分析完才能开始。线程池只在有空位时才收到任务，所以线程池自己的队列不再积压。
- 去重按所有者：主用户沿用裸键，其他用户键前缀带所有者；`DuplicateTaskError` 只会指向同一用户自己的任务。事件广播带所有者，SSE 只推送请求人自己的事件。任务列表先按所有者过滤再取 `limit`。

**报告隐私**

- 判定（`StockAnalysisPipeline._history_ownership_kwargs`）：传入了 `portfolio_context`（持仓分析），或 Agent 运行的工具调用日志里出现个人数据工具（`PERSONAL_DATA_TOOL_NAMES`，目前只有 `get_portfolio_snapshot`；搜索类 `user_scoped` 工具不读个人数据），报告私有，`requester_user_id` 为发起人（主用户也一样）。其余报告共享。
- 读取：`AnalysisHistory` 的查询统一用 `analysis_history_visibility_clause(viewer)`（共享或发起人是自己）；`viewer_user_id=None` 表示不过滤，只给系统任务（回测、信号评估、定时任务）用，用户入口一律显式传入请求人。`HistoryService(viewer_user_id=)` 对别人的私有报告返回与缺失 id 相同的结果。
- 删除：可见范围内，非管理员只能删自己发起的报告（删别人的共享报告会改变别人看到的内容），管理员可删共享报告，但看不到也删不了别人的私有报告。
- 回测、技能观点样本等读取历史报告的系统派生数据只读共享报告，私有报告不进入它们（第 18 节）。

**决策信号**：`decision_signals.owner_user_id` 空 = 共享。私有报告的信号归其发起人；读取（列表、`latest`、详情、后验结果 / 统计 / 单票复盘、反馈）只含共享和自己的；写（反馈、状态、重评估）对别人的私有信号按不存在处理；`outcomes/run` 仍是对全站信号的系统维护任务，但只回显请求人可见的条目。新信号只使同所有者（共享对共享、私有对同一用户私有）的相反 active 信号失效。P2a 的 `holding_state` 规则不变：非主用户的持仓分析不写 `holding_state`（记为 `unknown`），私有归属再保证信号只给本人看。

**共享报告复用（`src/services/report_reuse_service.py`）**

- 命中条件（全部相同）：股票（含各种存储写法）、交易日（`get_effective_trading_date(market)`，报告保存时写入 `report_trade_date`）、`model_channel_fingerprint`、`report_language`、`report_type`，且报告 `is_private` 为假。指纹是服务商 + 规范化 Base URL（小写主机、去默认端口和结尾斜杠）+ 模型名的 SHA-256 前 32 位，不含 Key；一个别名对应多个部署时全部参与（排序，与 Key 顺序无关）；保存时用实际应答的模型（`result.model_used`），查找时用请求人配置的主模型（Agent 模式用 Agent 主模型），主模型失败改用备用模型生成的报告指纹不同，不会被复用。本机 CLI 生成后端没有可检查的渠道，不生成指纹、不复用。
- 用户只声明了渠道、还没保存 Key（配置视图里该渠道因无 Key 被丢弃）时，指纹回落到声明的值：模型名 + 该服务商的声明 Base URL（`OPENAI_BASE_URL` / `OLLAMA_API_BASE`；不是服务商的前缀如网关分组 `auto/x` 按 OpenAI 兼容端点处理），所以同一渠道有没有 Key 指纹相同；没有任何声明模型的用户无法命中（没有渠道可比）。
- **差异（相对设计第 7 节原文的额外条件）**：非 `auto` 的 `analysis_phase`、`force_refresh` / `force_regenerate`、持仓分析不直接复用；股票所在市场处于交易时段（盘中 / 午休 / 收盘集合竞价）不复用，因为报告内含实时行情；请求要推送（`notify=true`，只有主用户可能）不复用，因为复用的报告没有新内容可推，也保持主用户每次点分析都重新生成的现有行为。P2b-2 当时“请求写了 `skills` 就一律不复用”的规则已被 P2b-4 的策略指纹取代（第 19 节）。
- 门禁：非主用户有可用模型 → 与以前一样校验并放行；没有 → 仅当每只请求的股票都命中复用才放行，否则 400 `model_key_required`（批量里只要有一只未命中就整体拒绝）。命中时 worker 里的 `AnalysisService.analyze_stock(allow_reuse=True)` 在需要模型之前返回已有报告，任务仍走队列（秒完成）。结果的 `query_id` 是原报告的 query_id（所有历史查找的键），并带 `reused`、`reused_report_id`。

**取数凭据**：`DataFetcherManager(config=用户配置视图)`；`StockAnalysisPipeline` 仅在配置是用户视图时传入。凭据（Tushare / TickFlow / 必盈 / 妙想 / Finnhub / AlphaVantage）取自视图，其余口径取系统配置；`TushareFetcher(token=, boost_priority=)` 让用户 Token 的链路位置与系统配置一致（系统配了 Token 才提升为最高优先级，否则排在默认位置）。每次分析本来就新建一个 `DataFetcherManager`，没有全局单例被改写，所以不需要按凭据指纹缓存管理器。`SOCIAL_SENTIMENT_API_KEY` / `FXMACRODATA_API_KEY` 不进 `DATA_SOURCE_CREDENTIAL_KEYS`：前者是系统级可选增强（未配置时对所有人关闭），后者由工具运行时直接读进程环境变量，无法在不改环境的前提下按用户绑定。
- **未做（Longbridge）**：Longbridge SDK 通过 `os.environ` 和 `~/.longbridge` 的共享 OAuth 缓存绑定凭据（`from_apikey_env` 会优先读环境），按用户绑定需要改写进程环境，违背「不改全局状态」，所以 Longbridge / Futu 一律用系统配置；用户在个人设置里填写的 `LONGBRIDGE_*` 目前被接受但不会用于取数。

**未覆盖（后续期）**：Agent 工具（`src/agent/tools/data_tools.py` 的模块级 `DataFetcherManager` 单例）、持仓估值、提醒取数等非分析流水线路径仍用系统凭据；Web 页面（历史页「只看我发起的」筛选、模型 Key 提示）和个人推送渠道仍待后续期。

## 18. P2b-3 实现说明（安全补漏：模型路由、重定向、回测隐私、持仓清扫，补充与差异）

契约：非主用户不能花系统的 Key / 凭据，也不能让服务器访问它自己或内网的服务；一个用户的个人数据（首先是持仓）不能到别人手里，包括主用户；一个人不能改变别人看到的内容。主用户的行为不变，可见差异在 18.7 汇总。

### 18.1 模型路由白名单（`src/llm/credentials_guard.py`）

LiteLLM 保留任意服务商前缀，`ollama/llama3`、`sagemaker/`、`bedrock/`、`vertex_ai/`、`azure/`、`watsonx/`、`hosted_vllm/`、`lm_studio/`、`vllm/`、`llamafile/`、`litellm_proxy/`，以及没有 Base URL 的 `openai/`（落到管理员的 `OPENAI_API_BASE`）都能绕过「必须自带 Key / 公网地址」的检查，使用服务器的环境变量、本机默认地址或云端环境凭据。现在非主用户的每个部署、每次直连只允许两类路由（`check_user_model_route`）：

| 类别 | 服务商 | 规则 |
| --- | --- | --- |
| 公共 SaaS 白名单（`USER_PROVIDER_DEFAULT_BASES`） | `openai`、`anthropic`、`gemini`、`deepseek`、`openrouter`、`groq`、`mistral`、`xai`、`together_ai`、`dashscope`、`moonshot`、`zai`、`perplexity`、`fireworks_ai`、`cerebras`、`sambanova`、`nvidia_nim`、`deepinfra`、`volcengine` | 必须有 `api_key`；没填 `api_base` 时调用前补上该服务商的默认公网地址（`harden_user_params`），所以 `<PROVIDER>_API_BASE` / `OPENAI_BASE_URL` 之类的环境变量不再生效；填了就必须是公网 https |
| 自建（`USER_OWN_ENDPOINT_PROVIDERS`） | `ollama`、`ollama_chat`、`hosted_vllm`、`lm_studio`、`litellm_proxy` | 必须有自己的公网 https `api_base`；除 Ollama 外必须有 Key；免 Key 的 Ollama 用固定占位 Key，不会把服务器的 `OLLAMA_API_KEY` 发给用户的主机 |
| 其他 | 一切别的前缀与 LiteLLM 无法判定服务商的裸名 | 拒绝（`ModelRouteNotAllowedError`，是 `ModelKeyRequiredError` 的子类，错误码 `model_route_not_allowed`，HTTP 400） |

**白名单逐项核对方法**：对已安装的 LiteLLM（1.98.1）扫描 `main.py` / `utils.py` / `router.py` / `llms/` / `litellm_core_utils/` 里读取的全部环境变量名，其中 140 个形如 `*_API_BASE` / `*_BASE_URL` / `*_ENDPOINT` / `*_URL` / `*_HOST`、148 个形如 `*_API_KEY` / `*_TOKEN` / `*_SECRET`，全部设成毒值后，逐个服务商用 `harden_user_params` 的结果经 httpx 传输层（mock，不联网）发一次调用：请求只落在「无毒环境下同一服务商带 Key 默认调用」的 URL 上，请求头里只有用户的 Key，没有任何环境里的 Key（`test_every_allowlisted_provider_ignores_the_server_environment[<provider>]`，19 项全过）。核对的同时一并确认：`gemini` 走 Google AI Studio（`x-goog-api-key`，不是 Vertex）、`deepseek` 默认地址为 `https://api.deepseek.com/beta`。新增服务商前必须先让这个参数化测试覆盖它。有疑问的一律不进白名单（要求自己的 Base URL）。

**保存时**（`UserSettingsService._validate_model_routes` → `validate_user_model_routes`）：对「已保存值 + 本次提交」按用户视图的真实构建结果检查，覆盖渠道（`LLM_<NAME>_PROTOCOL` / `_BASE_URL` / `_MODELS`）、`LITELLM_MODEL`、`LITELLM_FALLBACK_MODELS`、`LITELLM_LIGHT_MODEL`、`AGENT_LITELLM_MODEL`、`OPENAI_VISION_MODEL`；没有任何部署承载的模型名只检查服务商是否允许（它的 Base URL 取决于渠道，调用时再查）。系统的 `VISION_MODEL`（系统级键）被用户视图继承，图片提取在调用时按同一规则处理。**调用时**：分析器与 Agent 适配器的 Router 只含加固过的部署（`harden_user_model_list`），直连走 `require_explicit_credentials`（路由检查 + 补默认地址 + DNS 复查），入口门禁 `require_usable_model` 与 `validate_user_deployments`。旧的「免 Key Ollama 必须有自己的地址」规则（`_keyless_with_own_endpoint`）并入同一函数（`KEYLESS_PROVIDERS`）。

**环境变量引用**：LiteLLM 会把部署参数里 `os.environ/<名字>` 形式的值展开成服务器的环境变量（实测 `api_key` / `organization` / `api_base` 都会，Router 把管理员的变量作为 Bearer 发给用户填的地址）。非主用户的任何路由参数（渠道、各个模型名键、直连的 `api_key` / `extra_headers` 等，对话内容 `messages` 除外）含 `os.environ/` 一律拒绝（`reject_environment_references`，同一个 `model_route_not_allowed`），保存时和调用时都检查。

**白名单里的 `openai`**：没有 Base URL 的 `openai/<模型>` 加自己的 Key 是允许的（官方 OpenAI Key 的常见用法），但调用时一定带 `https://api.openai.com/v1`。如果要求用户也必须自填 OpenAI 的 Base URL，把 `openai` 从 `USER_PROVIDER_DEFAULT_BASES` 挪到 `USER_OWN_ENDPOINT_PROVIDERS` 即可（一行改动）。

### 18.2 重定向与 worker 复查

地址检查只看第一跳，公网主机可以回 3xx 到 `http://169.254.169.254/`。规则：服务器对用户提供的 URL 发请求一律不跟随重定向，3xx 就是错误。`requests` 调用传 `**NO_REDIRECT_REQUESTS_KWARGS`；调用方不拥有 httpx 客户端的（LiteLLM、OpenAI SDK、Router 缓存的客户端）在 `forbid_httpx_redirects()` 里发请求，它把 `httpx.Client.send` / `AsyncClient.send` 包一层（只包一次，上下文外原样透传），上下文内强制 `follow_redirects=False`。已覆盖：模型调用（`user_call_scope(config)`，只对用户视图生效；分析器 Router / 直连、Agent 适配器、图片提取、连通性测试）、非主用户的 SearXNG（`SearchService(searxng_follow_redirects=False)`，含重试路径 `_get_with_retry`）、用户自己的 Hindsight（`HindsightRecallProvider(allow_redirects=False)`；系统 Hindsight 是管理员自己的，不变）。公共 SearXNG 实例列表（searx.space）仍用库默认。异步分析任务的 worker 在执行时经 `AnalysisService.analyze_stock` 再次调用 `require_usable_model`（含 `validate_user_endpoints`：重新解析 DNS），请求入口之后、任务开始之前被改成私网地址的域名会被拒绝（`UnsafeUrlError`，400）。

### 18.3 持仓清扫

用户指示「持仓信息不能共享」：持仓（账户 / 持仓 / 批次 / 交易、次日计划的现金与持仓、`portfolio_context`、`get_portfolio_snapshot` 输出、提醒的持仓展开）的每一个写入点，都要么只属于所有者，要么已修。回归测试 `tests/test_multi_user_holdings_isolation.py` 以 A（非主用户）的名义跑每个产生路径（模型与行情 mock），再用 B 和主用户调用应用里**每一个** `GET` 路由（自动枚举，新增路由自动纳入），断言响应里没有 A 的任何持仓数值（股数、成本、浮盈、现金、止损 / 止盈位）；另有「对照」用例证明 A 自己能读回这些数据（测试不是空转）。

| 路径（写入点） | 谁能读 | 结论 |
| --- | --- | --- |
| 报告正文 / `raw_result` / 上下文快照 / 新闻（`analysis_history`，持仓分析与读了持仓快照的 Agent 运行） | 报告 `is_private`，只有发起人（历史列表 / 详情 / 诊断 / 运行流 / 新闻 / Markdown / 分享图 / 个股栏 / `analysis/status` 的回退，管理员也看不到） | 私有（P2b-2）；本期核对全部读取路径，`portfolio_context` 不进快照（`_without_runtime_prompt_context`） |
| 决策信号 `reason` / `risk_summary` / `holding_state` / 元数据（来自私有报告） | `owner_user_id` = 发起人；所有读写接口按所有者过滤；非主用户不写 `holding_state` | 私有（P2b-2）；本期补：`list_signals` 的懒提取只对可见报告生效、信号失效只发生在同一所有者之间 |
| **提醒创建的决策信号（`source_type=alert`，`portfolio_holdings` 规则的 `watch_conditions` 含「持仓 - 代码」）** | 以前落入共享池，所有人可读 | **已修**：所有者取 `RuntimeAlertRule.user_id`（内层规则对象没有 `user_id`，此前恒为空）；非主用户的规则、以及主用户自己的 `portfolio_holdings` 规则，信号都归所有者；读取最新信号 / 历史上下文也以所有者身份 |
| 回测结果 / 汇总 / 技能观点样本、结果、权重（含持仓相关的止损止盈位） | 回测接口、Agent 回测工具、技能权重 | **已修**：只读共享报告（`BacktestRepository.shared_report_condition` / `shared_result_condition`、`SkillOpinionSampleRepository.shared_sample_condition`）；样本只在报告共享时生成；存量由迁移删除（先备份）。选择「读取时过滤 + 迁移清理」两者并用：只靠迁移挡不住之后的写入，只靠读取过滤会留着私有数据 |
| Agent 记忆（`AGENT_MEMORY_ENABLED`，默认关）→ 提示词 → 可能被共享的报告 | 记忆原来读「共享 + 自己的私有」报告 / 信号 | **已修**：只读共享报告 / 信号（`SHARED_ONLY_VIEWER_ID`）；主用户开启记忆时也不再把自己私有报告的日期 / 建议 / 结果带进共享报告 |
| 定时分析超时的部分结果通知（系统渠道）与调度器 `last_error` | 主用户的渠道 | **已修**：只汇总主用户自己发起的报告（此前按时间窗读全部用户的行，会列出别人持仓分析的建议和分数） |
| Agent 会话 / 消息 / 摘要 / 提供方轨迹、`get_portfolio_snapshot` 输出 | 按会话所属用户（`require_chat_session_access`），工具 `user_scoped`，模型传入的 `user_id` 丢弃、缺用户即拒绝 | 私有（P2a） |
| 助手笔记 / 提议 / 修订 | `user_id` 过滤，注入用会话所有者 | 私有（P2a） |
| 长期记忆（Hindsight） | 非主用户一人一库 `<库>-u-<哈希>`；主用户用系统库；数字脱敏 | 私有；本期堵「用户填系统地址」「路径变体」绕过（18.4）；残留见 18.4 |
| 次日计划（现金 / 持仓 / 运行 / 计划 / 推送预览） | 仓库方法都带 `user_id`；推送对非主用户关闭 | 私有（P2a） |
| 提醒规则 / 触发 / 通知 / 冷却 | 经所属规则过滤；非主用户不推送，记 `__no_channel__` | 私有（P2a），展开用规则所有者的持仓 |
| 持仓接口（账户 / 交易 / 快照 / 风险 / 导入） | 服务绑定请求用户，外来账户 id 过滤 | 私有（P2a），没有不分用户的内存缓存 |
| 板块推荐 / 关注 | 推荐是共享产出，输入不含任何用户的关注 / 持仓；关注按用户 | 私有（P2a / P2b-2） |
| `llm_usage` | 只有 token、模型、股票代码，没有提示词；`/me/usage` 仅本人，`/usage/*` 仅管理员 | 无持仓内容 |
| 通知（分析推送、机器人、提醒、次日计划） | 非主用户的内容从不进入 `NotificationService`（`send_notification=False`、`single_stock_notify=False`） | 私有 |
| 日志 | 无按文件读日志的接口；Agent 运行日志只记工具名 | 无 |

**未修、需要你决定或属于设计取舍**（已核对，列在这里）：

1. **机器人 / MCP / CLI 以主用户身份运行**（设计第 8 节 / 第 15 节）：群里的任何人通过机器人 `/ask`、`/chat`、`/analyze` 都等于主用户，能让 Agent 读主用户的 `get_portfolio_snapshot`、看到主用户的私有报告；经 MCP 网关的调用方同理。这是「机器人是主用户自己的助手」的前提，没有改。要改的话：机器人的执行器去掉 `PERSONAL_DATA_TOOL_NAMES` 并让机器人 / MCP 的历史读取只读共享报告，会改变主用户在机器人里问持仓的现有能力，需要确认。
2. **主用户的批量 / 定时推送**（`src/notification.py` 的历史信号对比、`reports/` 本地文件、系统渠道）仍包含主用户自己的私有报告：推送渠道是主用户自己的，只要渠道里没有别人就不是泄露；若渠道是多人群，请在渠道侧处理。
3. **存量报告**（已在 P2b-4 解决，第 19 节）：P2b-2 之前由 Agent 运行读了持仓快照生成的报告没有记录工具调用日志，当时的迁移无法识别；P2b-4 的迁移把这类无法判定的 Agent 模式存量报告一律标为私有。
4. **资讯池**（`news_pool_service`）每天按主用户的自选 + 持仓抓公告，写入共享资讯池，别人通过「哪些代码有公告」能间接猜到主用户的关注集合。去掉持仓会改变主用户的公告覆盖，没有改。
5. 管理员的 `/usage/dashboard.recent_calls` 带 `stock_code`（不带用户 id，不是持仓内容，只提示某只股票被分析过）。

### 18.4 Hindsight 同址

`user_recall_settings`：用户的 `HINDSIGHT_BASE_URL` 与系统 URL 指向同一台服务器（比较主机与端口，忽略大小写、结尾点、默认端口、路径 `/.`、`/a/..`、查询串）时，按「用系统服务器」处理：始终用该用户派生的库，自己填的 bank id 和 Key 都不用于系统服务器。这样不能用别人的 / 主用户的 bank id 去读写它的记忆。残留：不同域名但解析到同一地址的别名无法识别（按地址比较会把共享 CDN 地址上的无辜用户误判），系统 Hindsight 应当用 `HINDSIGHT_API_KEY` 认证；能登录 Hindsight 服务器的运维人员当然能读到所有库。

### 18.5 设置页、大盘复盘、第三方 Key

- `GET /me/settings` 对继承系统默认的键只返回 `source=system_default` + `configured=true`，`value` 为空；主用户只读视图显示自己的值（密钥打码）。`POST /me/settings/test-model` 失败只返回 `error_category`（`auth_failed` / `unreachable` / `timeout` / `bad_response`）、最多一个 `http_status` 与固定文案，成功也不回显模型回复。
- 非主用户的个股分析调用 `DailyMarketContextService.get_context(allow_generate=False)`：只读取已存在的当日复盘，没有就不带，不用用户的模型生成也不写库（复盘是共享产出，只由系统任务和主用户生成）。
- **系统第三方付费 Key 清单**（用户分析间接可达的）：`SOCIAL_SENTIMENT_API_KEY`（用户视图里清空，流水线对用户视图不构建社交舆情服务，系统任务与主用户不变）；`FXMACRODATA_API_KEY`（FXMacroData 工具标记 `user_scoped`，对非主用户使用无 Key 的公共基础授权，主用户 / 机器人不变）；数据源凭据 `TUSHARE_TOKEN` / `TICKFLOW_API_KEY` / `BIYING_LICENCE` / `MX_APIKEY` / `FINNHUB_API_KEY` / `ALPHAVANTAGE_API_KEY`（设计上有意回退到系统值，取到的数据进共享缓存，第 17 节）；Longbridge / Futu 与 Agent 工具里的取数单例用系统凭据（第 17 节「未覆盖」）。其余秘密（机器人 token、`LLM_USAGE_HMAC_SECRET`）只被通知 / 用量代码使用，用户分析不会调用。搜索 Key 与模型 Key 用户自带。

### 18.6 测试

`tests/test_multi_user_p2b3_security.py`（路由拒绝 / 允许、白名单逐项对环境免疫、重定向不跟随、test-model 不回显、回测排除私有报告、Hindsight 同址、设置页、大盘复盘、第三方 Key）、`tests/test_multi_user_holdings_isolation.py`（持仓清扫）。涉及真实 LiteLLM / httpx / requests 的用例只替换最底层传输，不联网、不调用真实模型。

### 18.7 主用户的可见变化

- 开启 `AGENT_MEMORY_ENABLED`（默认关）时，记忆只含共享报告 / 信号，不再含主用户自己的私有（持仓）报告。
- 主用户自己的 `portfolio_holdings` 提醒触发时创建的决策信号归主用户私有，不再出现在共享信号池里。
- 定时分析超时的部分结果通知只汇总主用户自己发起的报告。
- 其余（模型路由、重定向、设置页、大盘复盘、第三方 Key）只影响非主用户。

## 19. P2b-4 实现说明（策略指纹、已采集输入的复用、存量 Agent 报告私有化，补充与差异）

对应第 7 节的重写。主用户的行为尽量不变：差异在 19.6 汇总。

### 19.1 有效策略集与策略指纹

- 解析（`src/agent/factory.py` 的 `compute_strategy_fingerprint`）与分析本身共用同一条规则（`_resolve_selected_skill_ids`，分析器和 Agent 执行器都经它取技能）：请求的 `skills`，否则发起人配置视图的 `AGENT_SKILLS`（用户个人设置会进入视图），否则系统默认技能。未知的技能 ID 与分析一样被忽略；`all` 展开为整个目录。
- 指纹 = SHA-256 前 32 位，输入：按技能 ID 排序的各技能**定义摘要**（名称、说明、指令正文、类别、核心规则、所需 / 允许工具、执行上下文、首选模型：技能定义改了就是另一套策略），加上“怎么用这组技能”：经典流水线，还是 Agent 流水线（单 Agent / 多 Agent 的编排模式）。Agent 是否启用与流水线共用 `analysis_uses_agent`（`AGENT_MODE`、请求技能、非 `all` 的 `AGENT_SKILLS`）。**这是相对“只比技能集合”的有意收紧**：经典分析器与 Agent 对同一技能生成的报告不是同一份；不想区分时去掉指纹里的 `pipeline` 项即可（一行）。
- **自动路由**：多 Agent 的 `specialist` 模式下，请求没有写 `skills` 时由技能路由器按股票的技术形态在运行时挑技能（`AGENT_SKILL_ROUTING=auto`），实际挑中的集合要生成后才知道。指纹因此记录的是**路由输入**（`routing=auto` + 整个技能目录的摘要；`manual` 则是 `AGENT_SKILLS` 过滤后的确定结果），不是挑中的集合：同一只股票同一个交易日的技术形态相同，路由决定相同，所以输入相同的两次运行得到同一份策略。能直接复用的请求恰好是：生成时的路由输入（模式、`AGENT_SKILLS` / 默认技能、技能目录定义）与本次请求的相同。请求写了 `skills` 时不经过路由器，指纹只取决于这组技能。
- 无法解析（技能管理器异常等）→ 指纹为空 → 永远不直接复用，走“重新分析 + 复用输入”。
- 保存：每份新报告写入 `analysis_history.skill_fingerprint`（管道 `_history_ownership_kwargs`）。旧报告为 NULL，查询要求指纹相等，所以永远不会被直接复用。查找条件见 `find_reusable_analysis`（新增 `skill_fingerprint` 条件）。
- 门禁 `_require_model_or_reusable_reports`：没有可用模型的用户，只有当每只请求的股票都命中直接复用（含策略指纹，使用该用户自己的 `AGENT_SKILLS`）才放行；否则 400 `model_key_required`。

### 19.2 复用已采集的输入（建立在现有的上下文快照上）

- **机制**：不新增存储。每次分析已经把输入写进 `analysis_history.context_snapshot`（`SAVE_CONTEXT_SNAPSHOT`，默认开启）：`realtime_quote_raw`、`chip_distribution_raw`、`enhanced_context.fundamental_context`、`market_structure_context`、`news_content`、`news_result_count`。本期给快照追加两个小字段：`news_evidence_present`（资讯是否真有证据，披露文案要用）和 `metadata_reuse`（本次复用了哪些输入、来源报告 ID、各输入最初的采集时间；链式复用时保留最初的采集时间）。日线不在快照里：它在共享的 `stock_daily` 表中，流水线原有的断点续传（`has_today_data`）本来就跳过已有交易日的日线获取，技术指标由日线重新计算。
- **取用**（`src/services/analysis_metadata_reuse.py`）：`load_reusable_metadata` 取同股票（所有存储写法）、同交易日（`report_trade_date`）、请求人可见（共享，或请求人自己的私有）、有快照的最新几份报告，取第一份能用的。只读白名单键，再对取到的结构递归剔除持仓 / 组合容器键（`portfolio_context`、`holdings`、`positions`、`accounts` ……）。结论类字段（`trend_analysis`、`analysis_context_pack_overview`、诊断、提示词、建议、评分、目标价）从不读取。
- **使用**：`StockAnalysisPipeline(reused_metadata=...)`；命中的输入不再向数据源 / 搜索获取：实时行情、筹码、基本面（含资金流、板块归属，不再补取板块）、市场结构、资讯文本（经典路径不再检索 / 取社交情绪 / 读本地资讯池；Agent 路径把它作为 `news_context` 注入，不再事后补查新闻）。来源缺的某项照常获取。
- **交易时段**：股票所在市场在盘中 / 午休 / 收盘集合竞价时，实时行情重新获取，其余照常复用；来源是在交易时段创建的，它的行情也不复用。
- **存储**：快照默认已经保存，复用不改变体积（只多两个小字段）。`SAVE_CONTEXT_SNAPSHOT=false` 时没有可复用的来源：每次照常取数，直接复用不受影响。
- **未覆盖**：(1) Agent 模型在运行中自己调用的工具（搜索、取数）仍然实时执行，复用的是流水线预取的输入；(2) 当日大盘环境（`DailyMarketContextService`）有自己的按日存储，仍由它读取，不从快照重建；(3) 股票名称查询不算数据复用。
- 主用户：要推送的请求仍然重新生成，但可以复用输入。CLI / 定时任务 / 机器人 / MCP 不走这条路径（它们不传 `reuse_metadata`），行为不变。

### 19.3 接口

- `POST /analysis/analyze`（同步响应与 `GET /analysis/status/{task_id}` 的 `result`）：直接复用仍返回 `reused=true`、`reused_report_id`；重新分析时若复用了输入，追加 `metadata_reused_from`（来源报告的主键，对请求人一定可见：来源只取共享的或请求人自己的）。旧字段不变，新字段仅在命中时出现。
- 任务内部：`TaskInfo.reuse_metadata`（不对外序列化），`submit_tasks_batch(reuse_metadata=)`，`AnalysisService.analyze_stock(reuse_metadata=)`；持仓页的单票分析（`/portfolio/positions/{symbol}/analysis`）传 `reuse_metadata=not force`。

### 19.4 迁移（`src/storage_multi_user_migration.py`，先备份、幂等、记录在 `schema_migrations`）

- `2026-11-10-multi-user-p2b4-skill-fingerprint`：`analysis_history.skill_fingerprint`（旧行 NULL，永不直接复用）。
- `2026-11-10-multi-user-p2b4-legacy-agent-reports-private`：Agent 模式（`raw_result.data_sources` 带 `agent:`）且没有 `report_trade_date` 的报告，是 P2b-2 记录工具调用之前保存的，无法判定有没有读过持仓快照 → 标为私有（属于它的发起人，旧行即主用户），其决策信号归同一所有者，并再次清理由私有报告派生的回测 / 技能观点数据。读不出来但含 `agent:` 的同样按私有处理；有 `report_trade_date` 的报告在保存时已检查过工具调用，保持原判定。两步同在一个事务，失败回滚并拒绝启动。回滚 = 恢复备份。

### 19.5 测试

`tests/test_multi_user_reuse_strategy_metadata.py`：策略指纹规则（顺序 / 去重、个人 `AGENT_SKILLS`、技能定义变化、路由输入）、直接复用（同策略免 Key、不同策略 / 旧 NULL 行不复用、无 Key 用户只放行直接命中）、真实流水线 + 真实分析器 + 模拟 LiteLLM 与数据源的端到端（策略 A / B：数据源 / 搜索都不再调用、模型只调用一次且只用 B 的 Key、提示词不含 A 的结论；持仓请求不直接复用且报告私有；他人私有报告既不能直接复用也不是输入来源；交易时段刷新行情）、持仓字段剔除、Agent 路径注入、迁移。`tests/test_multi_user_p2b2_reuse.py` 按新规则更新（保存时带策略指纹，去掉“写了 `skills` 就不复用”的用例）。

### 19.6 主用户的可见变化

- 请求写了 `skills`、且与已有共享报告的策略集一致时可以直接复用（此前一律不复用）；主用户要推送的默认请求仍然重新生成。
- 同日重复分析同一只股票时，行情 / 基本面 / 资讯等输入沿用同日已采集的，不再重新获取（交易时段内实时行情仍刷新）；要最新数据请用 `force_refresh` / `force_regenerate`。
- 迁移后，P2b-2 之前保存的 Agent 模式存量报告变为主用户的私有报告：其他用户在历史列表里不再看到它们，也不会被复用。

## 20. P2c Web 实现说明（补充与差异）

前端按第 3.6、10 节实现，差异如下：

- **`local` 模式不请求 `/me`**：`AuthProvider` 只在 `user_center` 且会话已授权时取一次 `/me`（取不到时回退为会话里的角色：`admin` 才是管理员，模型 Key 视为已配置）；`local` 模式按单人管理员处理（`isAdmin=true`、不提示模型 Key、不显示用户标签），因此单人部署和桌面端的界面与请求都和多用户之前完全一致。
- **门控依据**：`useCurrentUser().isAdmin`（来自 `/me.is_admin`）。整页门控用 `AdminOnly`（选股页、次日计划的选股页）或 `AdminRoute`（`/settings`、`/preferences` 跳到 `/settings/personal`）；按钮级门控用于回测运行、大盘复盘、决策信号写操作、板块推荐重新生成、问股上下文压缩和首页系统初始化检查。选股页整页对普通用户隐藏，因此他们在 Web 里看不到共享的选股历史（后端读接口仍对所有用户开放）。
- **个人设置不复制系统设置的渠道编辑器**：模型分组按 `GET /me/settings` 返回的字段逐项渲染（`LITELLM_MODEL`、`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` 等排在最前），`LLM_<NAME>_*` 动态渠道键只有保存过才会出现，Web 里不提供「新建渠道」向导。推送分组只显示开关 / 时间 / 静默时段等偏好，不显示 webhook / Token 等渠道凭据（个人推送渠道在 P3）。
- **「只看我的」的范围**：`mine_only` 只有 `GET /history` 支持，所以筛选放在个股历史趋势抽屉和首页「今日」标签；`GET /history/stocks`（历史标签的个股栏）没有该参数，不带筛选。发起人、私有徽标同样只在返回了这些字段的记录上显示。
- **复用标记的来源**：`reused` / `metadata_reused_from` 在 `GET /analysis/status/{task_id}` 的结果里；任务完成时前端补取一次状态，按报告的 `query_id` 记录（尽力而为，失败不影响报告），所以刷新页面后标记不会保留。
