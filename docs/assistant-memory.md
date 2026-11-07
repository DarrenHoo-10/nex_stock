# 助手记忆：笔记与长期回忆

设计依据：[核心流程后端设计 第 4 节](design/core-backend-design.md)。

## 三层

| 层 | 内容 | 依赖 |
| --- | --- | --- |
| 会话 | 本次对话原文和滚动摘要（`conversation_messages` 等，未改） | 内置 |
| 笔记 | 偏好页、个股页、板块页（Markdown，用户确认后写入） | 内置，存在数据库里 |
| 回忆 | 过往所有对话里聊过的事 | 可选插件，默认关闭 |

优先级固定：账本 / 规则 / 工具读到的当前数据 > 笔记 > 回忆。笔记和回忆里的数字都是当时的历史数字，助手回答前要重新取当前值。

## 笔记

- 表：`assistant_note`（`UNIQUE(scope, subject)`，`global` 只有一页）、`assistant_note_revision`（每次写入一条历史版本）、`assistant_note_proposal`（待确认的修改，不会改动页面本身）。
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
