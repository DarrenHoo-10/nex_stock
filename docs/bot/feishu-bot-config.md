# 飞书机器人配置指南

本文以**飞书应用机器人 + 长连接**为主线：一个应用机器人同时负责

1. 接收你发给它的消息（私聊，或群里 @它），由助手回答；
2. 作为**唯一的通知出口**，把个股报告、盘前版、板块 / 计划结果、告警和系统错误推送到你绑定的会话，并把推送写进该会话的助手上下文，回复推送即可继续追问。

群自定义机器人 Webhook 仍然可用，但只作为兼容回退，见文末「Webhook（兼容回退）」。

## 整体机制

```text
飞书 ──WebSocket 长连接（im.message.receive_v1，无需公网地址 / 回调 URL）──> 本进程
                                                                 │
   "/" 开头的消息 ───────────> 命令分发器（/analyze /market /bind /unbind …）
   其他文本（私聊；群里需 @机器人）──> 助手（与 Web 助手同一条链路）
                                      先回「正在分析…」卡片，完成后原地更新为答案

定时任务 / 告警 / 报告 ──> NotificationService ──机器人身份──> 绑定会话（卡片）
                                                └─> 同时写入该会话的助手上下文
```

- 长连接客户端在 `FEISHU_STREAM_ENABLED=true` 时随服务启动：`--serve` / `--serve-only` 等服务模式，以及不带 Web 的 `--schedule` 定时模式都会启动，同一进程只会运行一个客户端。
- 助手回答和 Web 助手一致：会话与当前标的、笔记注入、长期回忆、市场阶段、标的解析都生效；它可以读取持仓和现金（见下文「访问控制」）。
- 回答先以「正在分析…」卡片回复，助手完成后**原地更新**这条卡片（Markdown 渲染）；答案超过 `FEISHU_MAX_BYTES` 时，第一段更新进原卡片，其余部分作为后续回复卡片发送；助手失败时卡片会改成一句简短的错误说明。没有进度步骤，也没有按钮。

## 一、创建应用

**推荐：扫码创建（Web「设置 → 运行和推送」）。** 点「扫码创建飞书机器人」，用飞书扫描二维码并确认，系统会自动创建你自己的机器人（PersonalAgent），把 App ID / App Secret 写入配置、打开长连接，并把你和机器人的私聊设为推送目标（`FEISHU_CHAT_ID` = 你的 open_id，`FEISHU_RECEIVE_ID_TYPE=open_id`）。凭证只在服务端保存，不会传到浏览器。飞书国内版 / Lark 国际版由扫码账号自动识别并写入 `FEISHU_DOMAIN`。重新扫码会换成新的机器人并清除旧的 `/bind` 绑定（会话 ID 属于具体应用）。长连接已在运行时需要重启服务才会换用新机器人。接口：`POST /api/v1/system/feishu/register`、`GET /api/v1/system/feishu/register/{session_id}`。

下面是手动创建（已有机器人，或不方便扫码时）：

1. 打开 [飞书开放平台](https://open.feishu.cn/app)（国际版 Lark 用 [open.larksuite.com](https://open.larksuite.com/app)，并设置 `FEISHU_DOMAIN=lark`）→ 创建企业自建应用。
2. 「凭证与基础信息」里复制 **App ID** 和 **App Secret**，写入运行环境：

   ```env
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=xxx
   FEISHU_STREAM_ENABLED=true
   # Lark 国际版必须设置为 lark；飞书国内版保持 feishu
   FEISHU_DOMAIN=feishu
   ```

3. 「应用能力」→「添加应用能力」→ **机器人**（没有开启机器人能力时收不到消息、也无法推送）。

## 二、事件订阅：长连接

「事件与回调」→「事件配置」：

1. **订阅方式**选择「**使用长连接接收事件**」（不要选「将事件发送至开发者服务器」，本项目没有回调地址）。
2. 添加事件 **`im.message.receive_v1`**（接收消息 v2.0）。
3. 「回调配置」不需要配置，也不要使用卡片按钮回调：本项目的卡片不带按钮。

> 注意：长连接模式下，同一个应用的多个在线连接会被飞书**随机分发**事件。同一个应用不要同时在两台机器 / 两个进程里开启 `FEISHU_STREAM_ENABLED`，否则一部分消息会被另一个实例收走。

## 三、权限（应用身份）

「权限管理」里开通下面的权限（名称以开放平台权限管理页的搜索为准）：

| 权限 | 用途 |
| --- | --- |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息（接收私聊提问） |
| `im:message.group_at_msg:readonly` | 接收群聊中 @机器人 的消息事件（接收群里的提问） |
| `im:message:send_as_bot` | 以应用身份发送消息：推送通知，以及回复提问（占位卡片、后续分段卡片） |
| `im:message:update` | 更新应用发送的消息：占位卡片原地更新为答案（`im.v1.message.patch`） |
| `im:message` | 「获取与发送单聊、群组消息」；若权限管理页里发送 / 回复 / 更新能力显示为这一项，开通它即可 |
| `im:file`（仅 `FEISHU_SEND_AS_FILE=true` 时） | 以文件形式发送报告 |

说明：

- 本项目只从事件里读取 `chat_id`，**不调用**群信息 / 群成员接口，因此不需要 `im:chat:readonly` 一类读取群信息的权限；获取机器人自身信息（`GET /open-apis/bot/v3/info`）不需要额外权限。
- 如果希望群里**不 @机器人**也能用 `/` 命令，需要额外的「获取群组中所有消息」权限（敏感权限）；助手提问仍然只在被 @ 时响应。一般不建议开通。
- 上表依据 `lark-oapi` SDK 的接口定义（发送 / 回复 / `message.patch` / 机器人信息）与飞书开放平台文档整理；SDK 本身只在代码里出现 `im:message:send_as_bot`，其余权限的名称请在权限管理页里搜索确认。**缺少权限的典型表现**：收不到消息（事件权限缺失）、推送失败并返回 `99991672` 类错误（发送权限缺失）、占位卡片一直停在「正在分析…」（更新权限缺失；此时系统会改为直接回复答案）。
- 卡片更新有 14 天限制、单条消息更新频控约 5 次 / 秒；本项目每个占位卡片只更新一次（失败重试一次）。

## 四、发布版本与可用范围

权限和事件**发布版本后才生效**：「版本管理与发布」→ 创建版本 → 申请发布（企业自建应用通常由管理员审核）。之后每次改权限 / 事件都要重新发布。

### 访问控制：项目内没有白名单

本项目**不做用户白名单**，访问控制完全由飞书侧决定。因为助手可以读取持仓和现金，请务必：

- 把应用的**可用范围**设为你自己（或少数几个人），不要用「全员可用」；
- 机器人只加入「所有成员都可以看到持仓」的群，不要加入大群或外部群。

## 五、绑定通知会话：`/bind`

1. 打开与机器人的私聊（在飞书里搜索应用名称），或把机器人拉进目标群。
2. 私聊里直接发送 `/bind`；群里发送 `@机器人 /bind`。机器人回复确认后，这个会话就是**唯一的通知目标**：再次在别的会话 `/bind` 会替换它，`/unbind` 取消。

推送目标解析顺序：

1. `/bind` 绑定的会话（数据库表 `feishu_binding`）；
2. `FEISHU_CHAT_ID`（备用目标，`FEISHU_RECEIVE_ID_TYPE` 指定 `chat_id` 或 `open_id`）；
3. `FEISHU_WEBHOOK_URL`（兼容回退，不推荐）。

解析到 1 或 2 时，所有推送都由机器人以卡片发出（`report` / `alert` / `system_error` 的 `NOTIFICATION_*_CHANNELS` 路由过滤照常生效，邮件等其他渠道不受影响），并同时作为一条 assistant 消息写入该会话的助手上下文（`context_snapshot` 含 `kind=notification`、`route_type`、标题、时间）。

### 会话与追问

| 场景 | 助手会话 |
| --- | --- |
| 私聊，直接发的新消息 | 机器人在这条消息下开一个话题回答，每条新消息一个新会话 `feishu:<chat_id>:<消息 id>` |
| 私聊，话题内的消息 | 延续该话题的会话（同一话题内多轮对话共享上下文，不同话题可并行回答） |
| 私聊推送 | 写在 `feishu:<chat_id>`；回复推送（或在推送的话题里追问）延续这个会话 |
| 群聊，普通 @ 消息 | 每个群一个会话 `feishu:<chat_id>` |
| 群聊，话题 / 回复串里的 @ 消息 | 每个话题一个会话 `feishu:<chat_id>:<话题或根消息 id>` |
| 回复一条推送（或机器人的回答） | 延续这条消息所属的会话，所以追问时助手已经看过那条推送 |

### 在对话里让助手记笔记

发送「记住：不碰 ST 股」（或「记住 …」）：机器人会给出要写入的笔记条目（全局页，或当前会话关注标的的专属页），回复「确认」保存、「取消」放弃（提议 1 小时内有效，只对同一个会话的提议生效）。这是你主动要求的修改，不受「助手每天最多主动提议 N 次」的限制。

## 六、自检脚本

配置完成后运行（不会打印密钥或消息正文）：

```bash
python scripts/feishu_check.py                  # 凭据、机器人信息、推送目标
python scripts/feishu_check.py --listen 60      # 另外打开长连接 60 秒，期间给机器人发一条消息，确认能收到事件
python scripts/feishu_check.py --send-test      # 另外向推送目标发一张测试卡片
```

检查项：用 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 换取 tenant token → 获取机器人信息 → 显示实际生效的推送目标（`/bind` 绑定 > `FEISHU_CHAT_ID` > Webhook）→（可选）长连接收事件 →（可选）测试推送。`--listen` 前请先停掉正在运行的服务（见上文长连接的随机分发说明）。

## 七、常见问题

- **机器人不回复私聊**：确认已发布版本、事件订阅是「长连接」并添加了 `im.message.receive_v1`、已开通 `im:message.p2p_msg:readonly`、服务进程里 `FEISHU_STREAM_ENABLED=true`；用 `--listen` 验证。
- **群里没反应**：必须 @机器人（并开通 `im:message.group_at_msg:readonly`）；`/` 命令在没有「获取群组中所有消息」权限时同样需要 @。
- **卡片一直是「正在分析…」**：缺少 `im:message:update`（或 `im:message`）权限，或分析超时 / 进程被重启；系统在更新失败时会改为直接回复答案。
- **推送没有发到飞书**：先看推送目标——没有 `/bind`、也没有 `FEISHU_CHAT_ID` 且没有 Webhook 时不会发；`NOTIFICATION_REPORT_CHANNELS` 等路由配置里没有 `feishu` 也不会发。
- **`/bind` 后推送仍走 Webhook**：需要同时配置 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`；`/bind` 之后无需再配置 `FEISHU_CHAT_ID`。
- **私聊推送**：机器人不能主动给从未联系过它的用户发私聊，先在飞书里给机器人发一条消息再 `/bind`。
- `FEISHU_DOMAIN=lark` 会让推送、长连接和消息回复统一使用 `open.larksuite.com`；飞书国内版使用默认值 `feishu`。
- 飞书侧如果配置了 IP 白名单，需要把运行环境出口 IP 加入白名单（本地、Docker、云服务器各不相同）。

发送路径复用 `requirements.txt` 中已有的 `lark-oapi>=1.0.0`，标准安装使用 `pip install -r requirements.txt`；参考 [Feishu message create OpenAPI](https://open.feishu.cn/document/server-docs/im-v1/message/create)、[更新应用发送的消息卡片](https://open.feishu.cn/document/server-docs/im-v1/message-card/patch)、[lark-oapi PyPI](https://pypi.org/project/lark-oapi/) 和 [SDK repo](https://github.com/larksuite/oapi-sdk-python)。

### 文件发送模式（FEISHU_SEND_AS_FILE）

开启后，飞书 App Bot 将报告以 `.md` 文件形式发送，而非文字/卡片消息：

```bash
FEISHU_SEND_AS_FILE=true
```

- **需要应用权限**：`im:message`（发送消息）+ `im:file`（上传文件）
- **依赖版本**：`lark-oapi>=1.0.0` 需包含 `im.v1.file.create` API（文件上传类）
- **Webhook 模式**：回退为发送文件内容文本（Webhook 不支持文件上传）
- **生效范围**：仅对 `route_type="report"` 的报告推送生效；告警、系统通知等不受影响
- **GitHub Actions 定时任务**：已通过 `.github/workflows/00-daily-analysis.yml` 映射，在 repo Settings → Secrets and variables → Actions 中添加同名变量或 secret 即可启用
- **配置方式**：支持 `.env` 文件、GitHub Actions Secret/Variable 或 Web/桌面设置页配置

## Webhook（兼容回退）

适用场景：只想把报告推到一个群、不需要提问 / 追问，也不想建应用。它**不能**接收消息，推送也不会写入助手上下文；只在没有 `/bind` 绑定、也没有 `FEISHU_CHAT_ID` 目标时才会使用（配置了应用凭据且解析到目标后，Webhook 不再使用）。

```env
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/your_hook_token
# 按需填写
FEISHU_WEBHOOK_SECRET=your_sign_secret
FEISHU_WEBHOOK_KEYWORD=股票日报
```

### 1. 在飞书群里创建自定义机器人

路径通常是：
- 群聊
- 群设置
- 群机器人
- 添加机器人
- 自定义机器人

完成后复制机器人提供的 Webhook URL。

### 2. 查看机器人安全设置

飞书群机器人常见有三种安全限制：

1. 不加任何安全设置
2. 开启“关键词”
3. 开启“签名校验”

如果你的机器人开启了额外安全项，项目侧也必须同步配置，否则请求会被飞书拒绝。

- 开启了关键词：把同一个关键词写到 `FEISHU_WEBHOOK_KEYWORD`，项目会自动在每条飞书消息前补上。
- 开启了签名校验：把飞书里显示的 secret 写到 `FEISHU_WEBHOOK_SECRET`，项目会自动补 `timestamp` 和 `sign`。`FEISHU_WEBHOOK_SECRET` 与 `FEISHU_APP_SECRET` 是两个完全不同的配置，不能互换。

### 3. 在飞书自动化里配置 Webhook 触发器

如果你在飞书自动化流程里消费本项目推送的卡片消息，请按下面配置：

1. 在创建 Webhook 触发器时，**参数** 填写下面 JSON（`content` 可按需保留占位符）：

```json
{
  "msg_type": "interactive",
  "card": {
    "config": { "wide_screen_mode": true },
    "elements": [
      {
        "tag": "div",
        "text": {
          "tag": "lark_md",
          "content": "..."
        }
      }
    ],
    "header": {
      "title": {
        "tag": "plain_text",
        "content": "A股智能分析报告"
      }
    }
  }
}
```

2. 在 **操作/消息内容** 部分，不要手填纯文本；点击加号选择 **Webhook 触发**，并映射到：

`card.elements[0].text.content`

![img_11.png](img_11.png)

### Webhook 常见失败原因

1. **只填了 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`**：它们不会启用群 Webhook 推送；需要 `/bind`（或 `FEISHU_CHAT_ID`）或 `FEISHU_WEBHOOK_URL`。
2. **机器人开启了关键词，但本地没配 `FEISHU_WEBHOOK_KEYWORD`**：把飞书机器人安全设置中的关键词原样填进去。
3. **机器人开启了签名校验，但本地没配 `FEISHU_WEBHOOK_SECRET`**：把机器人 secret 填进去。
4. **机器人没在目标群里，或者没有发言权限**：检查机器人是否被添加到群，群管理员是否限制了机器人发言。
5. **飞书侧配置了 IP 白名单**：云服务器、Docker、GitHub Actions 的出口 IP 和本地不同，需要确认在白名单里。

## 排查顺序建议

1. 先运行 `python scripts/feishu_check.py`，看凭据、机器人信息和推送目标是否通过。
2. 收不到消息：`python scripts/feishu_check.py --listen 60`，核对事件订阅、权限和发布版本。
3. 收不到推送：`--send-test`，再核对路由配置（`NOTIFICATION_*_CHANNELS`）和机器人是否仍在目标会话里。
4. 仍用 Webhook 时，回到飞书机器人安全设置核对关键词 / 签名 / IP 白名单。

## 附：应用机器人原流程截图参考

下面是飞书开放平台创建应用的原截图（界面可能随平台更新而变化，权限与事件以本文为准）。

### 1. 创建应用

https://open.feishu.cn/document/develop-an-echo-bot/introduction

![img_6.png](img_6.png)

![img_8.png](img_8.png)

### 2. 获取密钥

![img_7.png](img_7.png)

### 3. 发布应用

![img_5.png](img_5.png)

### 4. 在飞书中打开应用

![img_9.png](img_9.png)

### 5. 消息交互

![img_10.png](img_10.png)
