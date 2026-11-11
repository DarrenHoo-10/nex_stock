# 金融工具与身份网关（finance.v1）

Stock 保存金融事实、策略、自选、账户和报告。NexBot Core 使用配对服务身份和可撤销用户委托调用同一套业务路由；服务 Bearer 本身不代表任何用户。浏览器继续使用 Stock 原有登录、Origin 与 CSRF 规则。

## 配置与绑定

多账号部署使用 `AUTH_MODE=user_center`，将 `PRIMARY_USER_ID` 配成历史资产已核实的主人。允许普通用户登录时，按用户系统的部署配置设置 `USER_CENTER_REQUIRED_ROLE=user`；其默认值 `admin` 只接受管理员。用户登录后，具体业务接口仍执行各自的角色检查。

Stock 的 `FINANCE_CORE_URL` 填 Core 根地址。Core 的 `NEX_CORE_STOCK_URL` 填 Stock 根地址，`NEX_CORE_STOCK_TOKEN`、`NEX_CORE_STOCK_INSTANCE`、`NEX_CORE_STOCK_CORE_ID` 分别对应下述三项配对设置。

用户系统 P1 至 P3 已合入 Stock 主线。金融网关沿用已验证登录的用户与角色读取自选、账本、笔记和报告，工具目录也按同一角色显示。P3 由 Stock 按用户设置运行定时计划、使用本人配置的推送渠道。NexBot 的实现和跨仓验收索引见其 `docs/plans/2026-11-09-finance-assistant.md`。

双方安全配置同一个至少 32 字符的 `FINANCE_SERVICE_TOKEN`，以及 `FINANCE_INSTANCE_ID`、`FINANCE_CORE_ID`。凭据只能放本机私有配置或部署秘密存储，不写进仓库、命令或日志。令牌轮换使原有加密委托失效，用户须重新绑定。

用户中心模式的 issuer 来自 `USER_CENTER_URL`，subject 为经过验证的 `user_session.userId`，绝不读取普通 `X-User-Id` 或工具参数。按人调用必须同时集成用户系统的 `CurrentUser`、个人配置、业务服务归属与报告过滤。在尚无该系统的主线快照中，共享模式网关只开放核实的公开市场读取，个人和付费操作拒绝执行。

本地模式必须显式配置 `FINANCE_LOCAL_ISSUER`、`FINANCE_LOCAL_SUBJECT`，并在 Core 将该实例配对到已核实的本地工作区。不能用缺失身份隐式选择主用户。`PUBLIC_ORIGIN` 也是本地绑定写操作的必需项。

1. 已登录浏览器显式 `POST /api/v1/finance/delegations` 绑定，返回 `{issuer,subject,instanceId,coreId,delegationId,expiresAt}`，不向浏览器返回用户委托 Bearer。
2. `POST /api/v1/finance/session-exchanges`（空请求体）返回 `{code,expiresAt}`。只有现存、未撤销的绑定可以交换；绑定缺失返回 409。Core 用配对服务 Bearer 向 `POST /api/v1/finance/session-exchanges/redeem` 提交 `{code,coreId}`，取得上述委托结构。code 60 秒内单次使用。
3. 委托 Bearer 租期 15 分钟，时间均为 Unix 毫秒。有效浏览器登录可重新交换以续租既有绑定；过期租期续租会轮换 Bearer。撤销后的绑定不会由交换自动恢复，必须本人再次显式绑定。
4. `GET /api/v1/finance/delegations` 仅列本人绑定。`DELETE /api/v1/finance/delegations/{delegationId}` 只撤销本人绑定。其他人的 ID 按不存在返回。

每次执行、读取或验证委托都向用户中心复核登录与当前角色，退出、角色变化、账号状态和撤销在下一次调用生效。委托不是离线授权：上游会话过期后，暂停私人步骤，回到本人入口恢复。SQLite 账本放应用数据库旁的 `.finance.db`，只存 Bearer 哈希；上游 Cookie 与待交换 Bearer 由配对服务密钥派生的 Fernet 密钥加密。账本属于私有运行数据，备份也需同等保护。

核销同时只向已认证 Core 返回稳定 `refreshToken` 和 capabilities。`POST /delegations/renew` 使用服务认证，请求 `{delegationId,refreshToken}`，续租相同绑定及操作回执；仍逐次复核真实上游会话和角色，不能复活撤销绑定或失效私人权限。并发续租复用尚有效的租期，避免互相废止令牌。`offlinePrivateRenewal` 仅显式配对的本地模式为 true。`bound(owner)` 检查持久绑定可解密，`ensure_bound(owner,cookie)` 为显式连接的事务幂等实现；同时点击连接不会互相撤销。凭据轮换导致旧加密失效时，状态显示未绑定，交换返回 409，由本人显式重新连接。

## 飞书设置入口与接管状态

原设置页「运行和推送 → 飞书」通过受原登录中间件保护的 `GET /api/v1/finance/assistant/feishu-settings` 读取 SQLite 中实际管理状态，不依赖浏览器缓存的配置开关。`owner=stock` 显示原本地扫码、绑定说明和手动配置；目标应用为 `paused` 或 `core` 时，移除旧扫码及写入表单，显示暂停或 NexBot 承接状态，引导用户在 NexBot「设置 → 连接器 → 飞书」管理账号绑定和订阅。首次载入、返回页面、每 30 秒及扫码/保存前重新核验；已打开的旧二维码在归属变化后取消轮询。旧后端没有此 GET 时（404）保留本地流程，认证、网络错误或畸形状态不会降级开放旧写入。

可选配置 `FINANCE_CORE_SETTINGS_URL=https://your-public-nexbot.example/?settings=feishu`，必须是管理员明确提供的浏览器公开入口；NexBot 深链在登录后定位到飞书设置。只允许 HTTPS（本机 localhost/127.0.0.1/::1 可用 HTTP），禁止 userinfo、fragment 及除 `settings=feishu` 以外的 query。未配置或校验失败时显示原生导航提示，绝不从私有 `FINANCE_CORE_URL`、反向代理头或服务 token 生成链接。该配置是可公开的 URL，不应放任何凭据。

接管仅作用于 `FINANCE_FEISHU_APP_ID` 所选择的应用。当前 Stock `FEISHU_APP_ID` 是另一应用时，显示实际当前应用仍由 Stock 管理并保留本地操作；服务端扫码入口和扫码成功写入也按实际应用核验，不能将已接管的目标应用写回旧流程。没有选择接管应用的部署保持原行为。

验证包含真实 loopback HTTP + 生产 Axios 的 React 组件用例，以及现有认证中间件和持久 CAS 状态的后端回归。未调用真实飞书或用户凭据。该专题只有中文文档，没有平行英文版本需要同步。

## 工具调用

Core 请求必须带 `Authorization: Bearer <配对服务凭据>` 和 `X-Finance-Delegation: <用户委托>`。`POST /delegations/verify` 返回当前验证的身份与有效期，省略 Bearer；任务执行、恢复与私人投递前应复核。

`GET /tools` 返回 `{version:"finance.v1",tools:[{name,description,inputSchema}],openapi,...}`。基础工具包括 `read_api_doc`、`call_api` 和 `strategy_inputs`。接入完整用户系统的私人会话和显式配对的单用户私人会话还可发现 `screening_run`、`screening_task`。实时 OpenAPI 提供业务输入输出结构，复用行情、宏观、指数、板块、策略、筛选、分析、报告、自选、持仓、计划和笔记路由。共享设置、登录和递归助手执行不属于此委托表面。业务路由自身的管理员限制、个人配置和数据归属继续生效。

`strategy_inputs({stock_codes,days?,strategy?})` 为普通账号提供多个标的的源日线、可引用证据、现有 `StockTrendAnalyzer` 的技术指标和可选的完整策略 YAML，由当前助手模型基于证据执行策略比较。它不调用进程级模型或搜索凭据，不给标的列表新增固定数量限制；数据不足明确标记。原有付费全市场筛选仍保留原业务管理员限制，不能将本工具返回的输入误称为已完成该全市场筛选。

普通账号的付费筛选使用独立私域工具 `screening_run` / `screening_task`。前者需要 operationId，使用本人配置/凭据与用量归属创建耐久私人任务；后者只返回本人任务及结果。它们在公开受众中不可发现/调用。执行复用既有策略引擎，但在隔离工作进程和本人缓存范围内运行，不把个人结果写入共享筛选历史；旧进程级管理员筛选入口保持原限制，缺少个人配置的共享部署明确不可用。

`POST /tools/call` 请求体为 `{name,arguments,operationId?}`。`call_api` 的 arguments 为 `{method,path,query?,body?}`，path 必须是已发现的 `/api/v1/...` 路由，不接受身份参数、URL 或路径跳转。写操作必须提供稳定 operationId。同一绑定、同一操作号与同一内容重试返回已记录结果；内容变化返回 409。执行前耐久写入意图，进程在可能写入后崩溃会返回 `FINANCE_OPERATION_OUTCOME_UNKNOWN`，必须回读业务状态，不能盲目重放。租期续租保留绑定 ID，因此操作回执继续有效。

`X-Finance-Audience` 为 `private`（默认）或 `public`。公开模式在取数据之前限定为公开行情、日线、分时、公司事件、机构调研、资本数据、问答、宏观指数、板块异动和策略目录；包含持仓/监控关系的个股聚合档案、私人报告、持仓、自选、计划、笔记、写操作均拒绝。群投递还必须由 Core 核实目的地与内容可见性。发现结果也按当前受众过滤。

返回保留业务数据全部来源、市场时区、交易日、源时间、单位和粒度字段。顶层 `evidence.retrievedAt` 仅为网关读取时间，`marketTimestamp:null` 表明网关没有推断市场观测时点；真实时点以业务数据内的来源证据为准。日线、过期快照或没有观测时间的数据不可当作当天某分钟的事实。该路径不额外限制标的数量、模型轮次或模型能力。

行情 API 增补实际 `source`、`provider_timestamp`、`fetched_at` 和 `is_stale`，日线增补实际 source。旧 API 的 `update_time` 兼容保留；金融工具的 quote 视图将其替换为真实来源时间（未知则为空），避免把服务端时钟用作市场证据。分时证据携带 5 分钟粒度、Asia/Shanghai、源交易日和稳定行引用；日线明确为 daily_bar，绝不推断分时变化。

## 报告事件与通道交接

`GET /api/v1/finance/reports/events?after=<cursor>` 使用服务 Bearer、用户委托和受众头，返回 `{events,nextCursor}`。事件的 `tradingDay` 可以为 null，独立的 `reportDate` 为可选产物日期，例如周末生成、引用此前交易日材料的公开报告：

```json
{
  "events": [{
    "cursor": 42,
    "instanceId": "stock-instance",
    "reportId": "market-review:2026-11-08",
    "version": "<content-sha256>",
    "kind": "market_review",
    "tradingDay": null,
    "reportDate": "2026-11-08",
    "views": [{"audience": "public", "content": "...", "verified": true}]
  }],
  "nextCursor": 42
}
```

内容哈希为版本，唯一索引保证重放不重复登记。游标扫描经过他人记录但不返回他人内容；Core 只有耐久登记自己的投递项后才能推进消费游标。

事件可额外带 `reportDate`（产物日期），它与实际 `tradingDay` 分开：文件名/生成标记只证明产物日期，不能证明行情交易日。已知实际交易日才填 tradingDay，未知保持 null；旧账本升级将历史文件名日期迁入 reportDate 并清除误标的 tradingDay。reportDate 只用于话题标题、来源引用及产物归档，不作为行情观测时点。

读取前从已提交的分析报告、已完成的本人计划和文件归档补登记。公开市场复盘使用 `read_public_market_review` 复核生成器登记的原文，历史个股汇总归主用户。读取事件时重新检查原记录、归属和隐私状态。源时间未知的历史报告保留空 tradingDay。P3 中普通用户未配置模型时，定时生成的规则计划标记 `reason=no_model`，只在本人网页展示，首次登记和历史事件重放都跳过该计划。NexBot 消费其他私人计划时继续验证本人委托。

公开订阅另用 `GET /reports/public-events`、服务 Bearer 和 `X-Finance-Subscription: <refreshToken>`。此 proof 只证明本人曾明确签发且尚未撤销的公开订阅，绝不构造用户身份，不能调用任何私人接口。它不依赖上游登录继续有效，配对密钥轮换、解绑或撤销后立即失效；公开事件使用独立公共游标，私人报告及其存在性均不进入该流。`publicEventsWithoutUpstreamSession` 为 true 表示这一独立能力，并不表示离线私人权限。两个事件入口均支持 `head=true`：先补登记已授权档案，再返回空 events 和当前水位，供首次切换保存起点而不重发旧档案。

日期均未知的新报告也按内容类型、受众和当前订阅正常入账与排队，不等待补日期，也不阻塞后面的报告。去重身份为 instanceId + reportId + version；话题标题依次使用 tradingDay、否则 reportDate，加 kind，两个日期都没有时只显示 kind，不编造日期。来源上下文保留真实的 tradingDay:null 和可选 reportDate。Core 在接收/话题记录及投递项事务耐久成功后推进游标，后续发送仍复核当前订阅与通道归属。

`FINANCE_FEISHU_MANAGED=true` 启用通道交接账本，`FINANCE_FEISHU_APP_ID` 明确选择接管应用（缺省首次启用时取当前配置应用），可记录 `FINANCE_FEISHU_TENANT_ID`。已有账本不因删除开关而恢复旧发送；更换应用必须显式处理原交接，不能通过配置热重载重置归属。其他应用的接收/发送和其他通知渠道保持各自原来的行为。

Core 用服务认证读取 `GET /channels/feishu`，返回 `{owner,version,managed,appId,tenantId}`；提交 `POST /channels/feishu`，请求 `{expectedVersion,owner,appId}`，owner 为 stock / paused / core。必须先暂停才能切到另一方，应用和版本不一致返回 409。旧接收器启动期间持有 OS 接收租约，Core 获取归属时若旧 SDK 仍连接会返回 `FINANCE_STOCK_RECEIVER_STILL_RUNNING`；必须停止持有旧连接的进程，不能只调用旧 SDK 的 stop()。首次启用管理前需重启所有旧 Stock 接收进程，使它们先具备租约约束。

Stock 的消息接收、扫码和共享飞书通道的发送读取同一份归属账本。发送持有归属事务直到完成。共享飞书切换后，主用户计划的 Stock 直发结果为 `status=skipped`、`reason=managed_feishu`，页面显示由 NexBot 按订阅投递。个人独立 Webhook、邮件等仍按本人设置发送，指向同一共享飞书 Webhook 的个人配置也遵循接管状态。Core 在收发前复核归属，回切 Stock 前关闭连接，保留消息回执和报告游标用于对账。

## 外部 MCP

既有 `python -m mcp_server` 的 `/mcp` Streamable HTTP 同时提供 `financial_discover()` 和 `financial_call(name,arguments,operation_id?)`，转发到 Stock 的相同 HTTP 网关并保留错误状态、用户和受众。凭据只从本次 HTTP 的 `Authorization`、`X-Finance-Delegation`、`X-Finance-Audience` 读取，MCP 工具参数不包含凭据或执行人。

委托调用使用 `FINANCE_SERVICE_TOKEN`，每次 MCP 请求先经 Stock 的 `/delegations/verify` 验证。此类会话只发现两个金融工具，不能调用 `service_start/stop` 或旧管家工具。旧 `MCP_SERVER_BEARER_TOKEN` 仍代表原管家运维身份，但它本身不能调用需要用户委托的金融工具。两套 token 必须不同，相同则拒绝启动 ASGI 应用。金融 API 不会按需启动/停止服务，也不回退管家身份。

`tests/test_finance_mcp_transport.py` 用真实 Streamable HTTP 客户端执行工具发现、调用与双向越权反例，仅在 Stock 出站 HTTP 边界提供合成响应。SDK HTTP 请求头默认隐藏 Authorization；此适配显式只纳入所需认证头，避免将浏览器 Cookie 或无关头转发到业务服务。

## 历史与旧 HTTP 客户端

`GET /api/v1/finance/history?after=<cursor>&limit=<page size>` 使用服务+私人用户委托，导出实际拥有的 user/assistant 消息，不导出 tool/system/provider trace、密钥或完整 context_snapshot。sourceContext 只保留 kind/id/code/name/tradeDate/asOf 字符串。会话归属采用 P2 真实状态行；只有消息的旧会话按原迁移规则属于主用户，名称和 Feishu 前缀不能认领他人记录。没有原始签名、应用与拥有者证明的飞书旧记录不带投递映射，保持私人。

返回 `{messages,nextCursor,hasMore,cursorVersion:2,capabilities:{edits:true,deletions:true,transactionalChanges:true}}`。messages 的 id 保留原消息 ID，cursor 为独立递增变更号；普通记录带 sessionId/role/content/createdAt（Unix 毫秒）及可选 sourceContext，删除记录为 `{id,sessionId,cursor,deleted:true}`。首次安装在同一 SQLite 事务中补登记源消息并创建 INSERT/UPDATE/DELETE 与拥有者变化触发器；后续追加、编辑、删除和归属迁移均与源写入同事务。删除会清除账本旧正文，重放不会恢复已删内容；修改后的正文替换旧快照，避免初次导入泄露编辑前文本。此变更游标不能当原消息 ID 使用。

配置 `FINANCE_CORE_URL` 后，真实旧 `/agent/chat`、`/chat/stream`、会话列表/消息/上下文/压缩/删除与停止入口进入相同 Core alias/topic；关闭配置仍执行旧本地路径。仅保留一个主副本：受管聊天不再往 Stock conversation_messages 写镜像，因此不会增量导入自己的新回复。SSE 保留 accepted/generating/done，断线只停止本次读取，已接受任务留在原话题；显式停止使用精确 messageId/taskId，不取消别的话题或同题的另一项排队输入。旧会话原归属和非主用户机器人前缀限制先于跨服务写入生效。跨进程/断线重试依靠请求回执和 Core 幂等输入，无固定模型轮次上限。

旧请求模型字段保持本轮 override：只在本人 Core 连接目录中精确解析成 alias 输入的 `modelSelection {connectionId,model,reasoning?}`，由 Core 验证并固化到任务/排队回执，纳入幂等摘要；不调用修改助手默认模型的接口，不影响并行话题。未知/歧义的旧渠道标识返回 422，不降档，不回退 Stock 主用户凭据。行情、宏观和指数的来源字段经过真实 API 响应模型保留；指数规范身份保留交易所，`000001.SH` 与裸 `000001` 个股不同，指数价格单位为点。宏观期数只提供日/月分辨率和存储来源证据，不宣称实时分钟观测。

## 验证与回滚

离线测试：`python -m pytest tests/test_finance_gateway.py -q`，覆盖一次性核销、撤销、跨主体、防伪造、租期续租、加密、写幂等/结果未知和并发 ASGI 身份。真实外部数据、真实用户中心及线上 Core 未由这些测试调用。

回滚可停用配对服务凭据并切回已验证按人隔离的旧入口；保留委托/操作账本和切换后业务写入。撤销不能通过恢复旧配置或重试交换绕过。该文档为中文专项接口说明，无对应英文文档需要同步。

## 助手页与侧边抽屉

`/assistant` 与各个股、板块、持仓页面的「问助手」抽屉先读取 `/api/v1/finance/assistant/status`。服务明确返回 `managed=false`，或旧后端尚无此入口（404）时，保留已有 Stock 会话。启用但未绑定时显示「连接金融助手」，点击后才调用 `/connect` 建立委托；身份、网络或畸形状态错误不会退回旧身份执行。

受管入口共用 Core 的 `conversationId`，每个话题在左侧会话栏显示一个入口，窄屏通过「会话列表」切换。模型和思考强度在输入栏右下角设置，会话列表独立于模型目录加载。新对话只是私人草稿，首条输入成功后才出现在会话栏。抽屉从个股、板块或持仓打开时保留证券代码/板块 ID、名称和已知交易日；这些资料只是研究起点，不限制跨标的查询。回复已有飞书话题时显示其投递目的地，受众由 Core 的原绑定决定。

浏览器只使用原有 Cookie 和 CSRF 链路访问 Stock BFF；不会接收 Core Bearer。模型和思考强度读取并修改本人 Core 助手设置，支持的档位取自该连接。发送和网络重试复用原 `clientId`，读取、停止与排队消息编辑均带选中的 `conversationId`。执行后有已发生或结果未知的效果时，Core 拒绝直接重放。

前端验证：运行 `npm run lint`、`npm run build` 与 financeAssistant/FinanceAssistant、原助手页/抽屉定向 Vitest。构建产物通过隔离 HTTP 静态服务和 Playwright Chrome 验证；全部 API 用合成夹具拦截，没有调用真实 Core、模型、行情或用户数据。截图留在本机临时验收目录，不入库。Windows 桌面复用该 Web 界面，但此次未重新打安装包。


## 通用 API 工具与报告可见性

`src/services/finance_data_visibility.py` 只将已核实的市场数据 GET 视图归为公开；自选、账户、计划、历史报告、决策信号、混合结果、写操作及未知/畸形接口均保守归为私人。调用结果的可见性证据只能进一步收窄，不能把私人接口提升为公开。该分类不代替接口本身的用户、角色和受众授权。

P2 的工具执行日志同时识别 runner 的 `arguments` 与 Codex 的 `arguments_summary`。经 `call_api` 间接读取私人接口与直接读取持仓一样，派生报告持久化 `requester_user_id` 和 `is_private`，后续信号继承主人，不能进入公共报告复用。看不到私人报告的请求不会触发该报告的延迟信号回填；提醒规则的归属取运行时包装对象的 `user_id`。


## 普通用户付费筛选

私人网关工具 `screening_run` 接收策略名称、市场、结果数、可选范围和变体种子，必须带稳定 `operationId`；`screening_task` 按任务编号读取本人结果，省略编号列出本人历史。普通用户配置好自己的模型后即可使用，与管理员维护的 `/screening/screen`、`/screen/tasks` 分开授权。旧接口继续保持管理员权限，不开放进程全局凭据。

引擎复用现有 `ScreeningService`，每项工作在独立 spawn 进程执行，仅传入请求人的有效模型、搜索和取数配置；缺少个人模型时明确拒绝，不回退系统模型或搜索密钥。未合入个人配置模块的功能树在用户中心模式返回 503，合入 P2 后直接启用。单一本地已验证所有者沿用其现有配置。模型用量写入现有本人 `LLMUsage` 记录，费用继续由本人供应商连接承担。

任务复用现有 TaskQueue 配置与公平调度，同一人不同操作编号可正常并发或入队。进度/SSE 使用队列的本人 `user_id`，结果保存在本人金融任务账本，缓存按账户和任务目录隔离，不写入旧共享筛选历史。另一个 API 进程查询不会中断活任务；仅确认所属进程退出或进程创建身份改变时标记中断，不自动重放已付费工作。

受理入口校验有效短租委托；受理后的任务使用原委托绑定与当前上游登录持续核验，可跨越 15 分钟 bearer 租约，但撤销、重新绑定或登录失效立即阻断下一次授权。搜索与模型调用前重新核验，个人连接被移除或更换时终止当前任务，避免继续使用已撤销配置。HTTP/提供方错误正文不进入任务账本或后台公共日志。

离线验证运行 `python -m pytest tests/test_finance_user_screening_compat.py -q`；P2 集成树另外运行 `tests/test_finance_user_screening.py`，通过合成提供方边界执行真实硬过滤、新闻搜索、模型解析和本人用量写库，并覆盖两人差异凭据、跨人拒绝、同人并发、独立服务观察进程、短租延续与撤销中止。未执行真实付费模型、搜索或行情调用。

## 跨进程互通验收

显式设置 `NEXBOT_TEST_CHECKOUT` 为包含 `server/personal-os/testing/cross-stock-fixture.ts` 的 NexBot 检出目录，运行 `python -m pytest tests/test_finance_cross_service.py -q`。夹具启动独立 Node Core、合成 nex_user、模型和飞书发送端，以及真实 loopback HTTP Stock 服务与临时 SQLite；只在行情提供方边界返回合成报价，保留认证中间件、业务服务、网关与 Core broker。未配置检出目录时明确跳过。P2 用户数据分支须在用户系统集成工作树实际运行，不能用主线缺少 P2 时的跳过作为通过证据。

用例覆盖两主体原生 Core 登录与 Stock 话题一致、同键重放、跨人及伪造身份拒绝、CSRF、委托与上游会话撤销，以及飞书问答经 Core 金融工具读取 Stock 报价。P2 分支另外覆盖两用户由模型分别经 Core broker 写入并读取自己的自选、公共受众拒绝私域接口，且核对实际工具回执与持久化数据。所有凭据和内容均为合成值，禁止非 loopback DNS/HTTP，LiteLLM 价格表固定使用本地副本，不启动 Stock 调度任务。

左侧会话「⋯」菜单中的删除操作在 Core 按会话 ID 生效，Stock 和 NexBot 同步移除入口。删除会停止该话题的工作。聊天按消息序号同步；打开会话加载近期消息，可向前查看更早消息。发送后立即显示自己的消息，阅读上文时会保留滚动位置。
