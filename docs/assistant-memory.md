# 助手记忆：笔记与长期回忆

设计依据：[核心流程后端设计 第 4 节](design/core-backend-design.md)。

## 三层

| 层 | 内容 | 依赖 |
| --- | --- | --- |
| 会话 | 本次对话原文和滚动摘要（`conversation_messages` 等，未改） | 内置 |
| 笔记 | 偏好页、个股页、板块页（Markdown，用户确认后写入） | 内置，存在数据库里 |
| 回忆 | 过往所有对话里聊过的事 | 可选插件，默认关闭；主用户用 `HINDSIGHT_BANK_ID` 指定的库，其他用户每人一个独立记忆库（见下文「多用户记忆库」） |

优先级固定：账本 / 规则 / 工具读到的当前数据 > 笔记 > 回忆。笔记和回忆里的数字都是当时的历史数字，助手回答前要重新取当前值。

## 笔记

- 表：`assistant_note`（`UNIQUE(user_id, scope, subject)`，每个用户的 `global` 只有一页）、`assistant_note_revision`（每次写入一条历史版本，经 `note_id` 归属）、`assistant_note_proposal`（待确认的修改，不会改动页面本身，带 `user_id`）。
- **数据范围（多用户）**：笔记、历史版本和提议都按登录用户隔离，接口只返回和修改当前用户自己的数据（客户端不传 `user_id`）。别人的笔记 id、提议 id 一律按不存在处理，返回 404（与不存在的 id 完全相同的响应体）。每日主动提议限额、`usage`、清空操作都只针对当前用户。注入到对话里的笔记取自会话所属用户。机器人和 CLI 以主用户（`PRIMARY_USER_ID`）身份读写。
- 写入只有三条路径：确认提议、直接编辑、回退到历史版本；每次写入 `version` 加 1。确认提议带乐观并发保护：提议生成后页面被改过，确认返回 409。
- 助手主动提议每天最多 `ASSISTANT_NOTE_DAILY_PROPOSAL_LIMIT` 次（默认 2，只统计 `source=suggested`，用户说「记住……」不受限）。
- 注入：每次提问带「偏好」页；会话当前对象是某只股票时带该股票页；聊天请求的 `context` 里带 `board_id` 时带该板块页。总长度上限 `ASSISTANT_NOTE_INJECT_MAX_CHARS`（默认 3000）：放不下时只注入完整的前若干条目、不截断条目中间，并提示助手提醒用户整理（`GET /api/v1/agent/notes` 的 `usage.needs_tidy` 也会给出）。

### 接口（均在 `/api/v1/agent/notes` 下，新增，不影响现有接口）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `` | 全部页面 + `usage`（总字数、上限、`needs_tidy`），可选 `?scope=` |
| GET | `/page?scope=&subject=` | 单页，未写过时 `note` 为 `null` |
| PUT | `/page` | 直接编辑（新建或覆盖），可带 `expected_version`，不匹配返回 409 |
| DELETE | `/page?scope=&subject=` | 删除一页及其历史 |
| DELETE | `?confirm=true` | 清空全部笔记、历史和提议 |
| GET | `/{note_id}/revisions` | 历史版本，新到旧 |
| POST | `/{note_id}/revert` | `{version, expected_version?}`，以旧版本正文写入新版本 |
| POST | `/proposals` | `{scope, subject?, title?, body_md, source, origin_session_id?, origin_message_id?}`，返回带 `before_md` / `after_md` / `diff` 的待确认提议；超出每日限额返回 429 |
| GET | `/proposals` | 待确认提议，可选 `?status=` |
| POST | `/proposals/{id}/confirm` | 写入；页面已变更返回 409 |
| POST | `/proposals/{id}/reject` | 放弃提议 |

错误体形如 `{"detail": {"error": "note_version_conflict", "message": "...", "current_version": 2}}`。

## 长期回忆插件

`src/agent/recall/`：`RecallProvider` 协议（`retain` / `recall` / `forget` / `health`）、注册表、默认 `none`（空实现）和 `hindsight`（仅在选中时才导入，使用 `requests`，没有新增必装依赖）。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ASSISTANT_RECALL_PROVIDER` | `none` | `none` / `hindsight` |
| `HINDSIGHT_BASE_URL` | 空 | Hindsight HTTP API 地址 |
| `HINDSIGHT_API_KEY` | 空 | 可选 Bearer 令牌（敏感项，可在 Web 设置页填写） |
| `HINDSIGHT_BANK_ID` | 空 | 助手专用 bank，不要和开发用记忆混用 |
| `ASSISTANT_RECALL_TIMEOUT_S` | `2` | 提问时 recall 的整体超时，超时即跳过 |
| `ASSISTANT_RECALL_MASK_AMOUNTS` | `true` | 发送前对金额、股数、成本打码 |

行为：

- 提问时按问题文本 + 上下文对象标签（`stock:300750`、`board:<id>`）召回，结果附日期，作为「过往对话」注入；未配置、超时或报错都会跳过，系统提示要求助手在依据里注明「未使用长期回忆」，聊天照常进行。
- 回答结束后在后台队列写入本轮问答，文档 ID 为 `{session_id}:{user_message_id}`，带对象标签、`date:` 和 `phase:` 标签；失败最多重试 3 次，之后丢弃并记日志。
- 删除会话时（Web、机器人 `/history clear`）对对应文档发起 `forget`；失败进入待删队列按退避重试，直到成功。队列在内存里，进程重启后未完成的待删项会丢失。
- `GET /api/v1/agent/status` 新增 `recall` 字段（`provider`、`status`、`message`、`latency_ms`、`pending_retain`、`pending_forget`）。

### Hindsight 接口

按 Hindsight 公开 OpenAPI（`vectorize-io/hindsight` 的 `hindsight-docs/static/openapi.json`）：

- `POST /v1/default/banks/{bank_id}/memories`（retain，`async: true`）
- `POST /v1/default/banks/{bank_id}/memories/recall`
- `DELETE /v1/default/banks/{bank_id}/documents/{document_id}`（404 视为已删除）
- `GET /health`

## 对话快照

`conversation_messages.context_snapshot`（可空 JSON 文本）记录新消息产生时的市场阶段、数据时间和上下文对象；旧数据和旧写入方不受影响，启动时自动补列。

## 多用户记忆库

长期回忆按用户隔离，两个用户永远不会读到或写入同一个库：

- **主用户**：和以前一样，使用 `.env` 的 `HINDSIGHT_BASE_URL` / `HINDSIGHT_API_KEY` / `HINDSIGHT_BANK_ID`。
- **非主用户使用系统 Hindsight**（没有保存自己的 `HINDSIGHT_BASE_URL`）：服务器地址和 Key 取系统配置，库固定为 `<HINDSIGHT_BANK_ID>-u-<sha256(user_id) 前 16 位十六进制>`（系统没配 bank id 时前缀为 `dsa`）。同一用户每次得到同一个库，不同用户互不相同，库名里没有用户 id 明文。用户自己填写的 `HINDSIGHT_BANK_ID` 对系统 Hindsight 不生效，否则两人可能指到同一个库。用户把自己的 `HINDSIGHT_BASE_URL` 填成系统服务器的地址（主机和端口相同，不论大小写、结尾斜杠、`/.` 等路径写法）同样按「使用系统 Hindsight」处理：仍是派生的库，自己填的 bank id 和 Key 都不会用到系统服务器上。
- **非主用户自带 Hindsight**（在个人设置里保存了自己的 `HINDSIGHT_BASE_URL`）：用自己的地址、Key 和 bank id（没填 bank id 时用上面的派生规则，前缀 `dsa`）。地址必须是 `https` 公网地址，每次使用前重新检查，不通过则对该用户关闭回忆，不会改用系统服务器；对这台服务器的请求不跟随重定向。
- `ASSISTANT_RECALL_PROVIDER=none`、系统 Hindsight 没配置完整时，该用户的回忆保持关闭。
- 删除会话时，遗忘请求只发往会话所属用户的记忆库。

