import type {
  ChannelTypeInfo,
  MeSchedule,
  PersonalChannel,
  PersonalChannelsResponse,
} from '../types/me';

const SECRET = '******';

/** The channel type catalog as `GET /me/notification-channels` returns it (src/services/user_notification_service.py). */
export const channelTypeCatalog: ChannelTypeInfo[] = [
  {
    channelType: 'feishu',
    label: '飞书机器人 Webhook',
    fields: [
      { name: 'webhook_url', label: 'Webhook 地址', kind: 'url', required: true, secret: true },
      { name: 'secret', label: '签名密钥（可选）', kind: 'token', required: false, secret: true },
      { name: 'keyword', label: '关键词（可选）', kind: 'text', required: false, secret: false },
    ],
  },
  {
    channelType: 'dingtalk',
    label: '钉钉机器人 Webhook',
    fields: [
      { name: 'webhook_url', label: 'Webhook 地址', kind: 'url', required: true, secret: true },
      { name: 'secret', label: '加签密钥（可选）', kind: 'token', required: false, secret: true },
    ],
  },
  {
    channelType: 'wecom',
    label: '企业微信机器人 Webhook',
    fields: [{ name: 'webhook_url', label: 'Webhook 地址', kind: 'url', required: true, secret: true }],
  },
  {
    channelType: 'telegram',
    label: 'Telegram',
    fields: [
      { name: 'bot_token', label: 'Bot Token', kind: 'telegram_token', required: true, secret: true },
      { name: 'chat_id', label: 'Chat ID', kind: 'telegram_chat', required: true, secret: false },
      { name: 'message_thread_id', label: '话题 ID（可选）', kind: 'thread', required: false, secret: false },
    ],
  },
  {
    channelType: 'pushplus',
    label: 'PushPlus',
    fields: [
      { name: 'token', label: 'Token', kind: 'alnum_token', required: true, secret: true },
      { name: 'topic', label: '群组编码（可选）', kind: 'topic', required: false, secret: false },
    ],
  },
  {
    channelType: 'serverchan3',
    label: 'Server酱3',
    fields: [{ name: 'sendkey', label: 'SendKey', kind: 'alnum_token', required: true, secret: true }],
  },
  {
    channelType: 'email',
    label: '邮件（自己的 SMTP 账号）',
    fields: [
      { name: 'smtp_host', label: 'SMTP 服务器', kind: 'host', required: true, secret: false },
      { name: 'smtp_port', label: 'SMTP 端口（465 / 587 / 25）', kind: 'port', required: true, secret: false },
      { name: 'username', label: '登录用户名（可选，默认同发件人）', kind: 'text', required: false, secret: false },
      { name: 'password', label: '密码 / 授权码', kind: 'password', required: true, secret: true },
      { name: 'sender', label: '发件人邮箱', kind: 'email', required: true, secret: false },
      { name: 'receivers', label: '收件人邮箱（最多 5 个，留空发给自己）', kind: 'email_list', required: false, secret: false },
    ],
  },
  {
    channelType: 'custom',
    label: '自定义 Webhook',
    fields: [
      { name: 'webhook_url', label: 'Webhook 地址', kind: 'url', required: true, secret: true },
      { name: 'bearer_token', label: 'Bearer Token（可选）', kind: 'token', required: false, secret: true },
      { name: 'body_template', label: '请求体模板（可选，JSON）', kind: 'template', required: false, secret: false },
    ],
  },
];

export function feishuChannel(overrides: Partial<PersonalChannel> = {}): PersonalChannel {
  return {
    id: 1,
    channelType: 'feishu',
    name: '我的飞书群',
    enabled: true,
    source: 'user',
    editable: true,
    readable: true,
    config: { webhook_url: SECRET, secret: SECRET, keyword: '计划' },
    createdAt: '2026-11-10T08:00:00',
    updatedAt: '2026-11-10T08:00:00',
    ...overrides,
  };
}

export function emailChannel(overrides: Partial<PersonalChannel> = {}): PersonalChannel {
  return {
    id: 2,
    channelType: 'email',
    name: '邮件（自己的 SMTP 账号）',
    enabled: true,
    source: 'user',
    editable: true,
    readable: true,
    config: {
      smtp_host: 'smtp.example.com',
      smtp_port: 465,
      username: '',
      password: SECRET,
      sender: 'alice@example.com',
      receivers: ['alice@example.com', 'bob@example.com'],
    },
    ...overrides,
  };
}

export function channelsResponse(overrides: Partial<PersonalChannelsResponse> = {}): PersonalChannelsResponse {
  return {
    userId: 'u-alice',
    isPrimary: false,
    managedInSystemSettings: false,
    note: '',
    maxChannels: 10,
    channels: [feishuChannel(), emailChannel()],
    channelTypes: channelTypeCatalog,
    ...overrides,
  };
}

/** The primary user's read-only `.env` channels: types only, no address and no secret. */
export function primaryChannelsResponse(types: Array<[string, string]> = [['feishu', '飞书'], ['wechat', '企业微信']]): PersonalChannelsResponse {
  return {
    userId: 'u-admin',
    isPrimary: true,
    managedInSystemSettings: true,
    note: '主用户的推送渠道就是系统设置里的 .env 渠道，请在系统设置中管理。',
    maxChannels: 0,
    channels: types.map(([channelType, name]) => ({
      id: null,
      channelType,
      name,
      enabled: true,
      source: 'env' as const,
      editable: false,
      readable: true,
      config: {},
    })),
    channelTypes: [],
  };
}

export function scheduleResponse(overrides: Partial<MeSchedule> = {}): MeSchedule {
  return {
    userId: 'u-alice',
    isPrimary: false,
    managedInSystemSettings: false,
    dailyPlan: { enabled: true, times: ['17:30'], defaultTime: '18:00', earliestTime: '17:30' },
    preopenPush: { enabled: false, time: '08:15', defaultTime: '08:15', latestTime: '09:00' },
    ...overrides,
  };
}
