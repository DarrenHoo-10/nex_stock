import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type Route } from '@playwright/test';
import { mockPlanApi } from './plan/mockPlanApi';

// Deterministic fixture spec for the multi-user Web UI (AUTH_MODE=user_center): no backend, every API call is
// mocked. Besides the assertions it writes screenshots (test-results/fixtures/**, and the P3b push channel / schedule
// states into test-results/pr-screenshots/, both git-ignored) for the PR description.

const HERE = dirname(fileURLToPath(import.meta.url));
const PR_SCREENSHOTS = join(HERE, '..', '..', 'test-results', 'pr-screenshots');
const prShot = (name: string) => join(PR_SCREENSHOTS, `${name}.png`);

type Role = 'user' | 'admin';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function field(overrides: Record<string, unknown>) {
  return {
    title: overrides.key,
    description: '',
    group: 'model',
    data_type: 'string',
    ui_control: 'text',
    options: [],
    is_secret: false,
    value: '',
    source: 'unset',
    inherits: true,
    configured: false,
    editable: true,
    updated_at: null,
    ...overrides,
  };
}

const PUSH_GROUP = {
  id: 'push',
  title: '推送',
  fields: [
    field({ key: 'DAILY_PLAN_ENABLED', group: 'push', title: '次日计划', ui_control: 'switch', data_type: 'boolean', source: 'system_default', configured: true }),
    field({ key: 'DAILY_PLAN_TIME', group: 'push', title: '次日计划时间' }),
    field({ key: 'PREOPEN_PUSH_ENABLED', group: 'push', title: '盘前推送', ui_control: 'switch', data_type: 'boolean' }),
    field({ key: 'PREOPEN_PUSH_TIME', group: 'push', title: '盘前推送时间' }),
    field({ key: 'NOTIFICATION_QUIET_HOURS', group: 'push', title: '静默时段', value: '22:00-07:00', source: 'user', configured: true }),
    // The legacy per-channel credentials the API still returns: the page must not offer them.
    field({ key: 'FEISHU_WEBHOOK_URL', group: 'push', title: '飞书 Webhook（旧）', is_secret: true, ui_control: 'password', inherits: false }),
    field({ key: 'TELEGRAM_BOT_TOKEN', group: 'push', title: 'Telegram Bot Token（旧）', is_secret: true, ui_control: 'password', inherits: false }),
  ],
};

function settingsFor(primary: boolean) {
  const editable = !primary;
  const withEditable = (item: Record<string, unknown>) => ({ ...item, editable });
  return {
    user_id: primary ? 'u-admin' : 'u-alice',
    is_primary: primary,
    managed_in_system_settings: primary,
    groups: [
      {
        id: 'model',
        title: '模型',
        fields: [
          field({ key: 'LITELLM_MODEL', title: '主模型', value: 'openai/auto/deepseek-v4.1-flash', source: 'user', configured: true }),
          field({ key: 'OPENAI_API_KEY', title: 'OpenAI API Key', is_secret: true, ui_control: 'password', value: '******', source: 'user', inherits: false, configured: true }),
          field({ key: 'OPENAI_BASE_URL', title: 'OpenAI Base URL', inherits: false }),
          field({ key: 'LLM_TEMPERATURE', title: '温度', source: 'system_default', configured: true }),
        ].map(withEditable),
      },
      {
        id: 'search',
        title: '搜索',
        fields: [field({ key: 'TAVILY_API_KEYS', group: 'search', title: 'Tavily API Keys', is_secret: true, ui_control: 'password', inherits: false })].map(withEditable),
      },
      { ...PUSH_GROUP, fields: PUSH_GROUP.fields.map(withEditable) },
      {
        id: 'position',
        title: '仓位规则',
        fields: [field({ key: 'POSITION_MAX_HOLDINGS', group: 'position', title: '最大持仓数', value: '8', source: 'user', configured: true })].map(withEditable),
      },
    ],
  };
}

// --- personal push channels (docs/design/multi-user.md section 21.1): the catalog as the API returns it ----------
type CatalogField = { name: string; label: string; kind: string; required: boolean; secret: boolean };
const cf = (name: string, label: string, kind: string, required = false, secret = false): CatalogField => ({ name, label, kind, required, secret });
const CHANNEL_CATALOG = [
  { channel_type: 'feishu', label: '飞书机器人 Webhook', fields: [cf('webhook_url', 'Webhook 地址', 'url', true, true), cf('secret', '签名密钥（可选）', 'token', false, true), cf('keyword', '关键词（可选）', 'text')] },
  { channel_type: 'dingtalk', label: '钉钉机器人 Webhook', fields: [cf('webhook_url', 'Webhook 地址', 'url', true, true), cf('secret', '加签密钥（可选）', 'token', false, true)] },
  { channel_type: 'wecom', label: '企业微信机器人 Webhook', fields: [cf('webhook_url', 'Webhook 地址', 'url', true, true)] },
  { channel_type: 'telegram', label: 'Telegram', fields: [cf('bot_token', 'Bot Token', 'telegram_token', true, true), cf('chat_id', 'Chat ID', 'telegram_chat', true), cf('message_thread_id', '话题 ID（可选）', 'thread')] },
  { channel_type: 'pushplus', label: 'PushPlus', fields: [cf('token', 'Token', 'alnum_token', true, true), cf('topic', '群组编码（可选）', 'topic')] },
  { channel_type: 'serverchan3', label: 'Server酱3', fields: [cf('sendkey', 'SendKey', 'alnum_token', true, true)] },
  {
    channel_type: 'email',
    label: '邮件（自己的 SMTP 账号）',
    fields: [cf('smtp_host', 'SMTP 服务器', 'host', true), cf('smtp_port', 'SMTP 端口（465 / 587 / 25）', 'port', true), cf('username', '登录用户名（可选，默认同发件人）', 'text'), cf('password', '密码 / 授权码', 'password', true, true), cf('sender', '发件人邮箱', 'email', true), cf('receivers', '收件人邮箱（最多 5 个，留空发给自己）', 'email_list')],
  },
  { channel_type: 'custom', label: '自定义 Webhook', fields: [cf('webhook_url', 'Webhook 地址', 'url', true, true), cf('bearer_token', 'Bearer Token（可选）', 'token', false, true), cf('body_template', '请求体模板（可选，JSON）', 'template')] },
];

interface MockChannel {
  id: number;
  channel_type: string;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
}

const SEED_CHANNELS: MockChannel[] = [
  { id: 1, channel_type: 'feishu', name: '我的飞书群', enabled: true, config: { webhook_url: '******', secret: '******', keyword: '计划' } },
  {
    id: 2,
    channel_type: 'email',
    name: '邮件（自己的 SMTP 账号）',
    enabled: true,
    config: { smtp_host: 'smtp.example.com', smtp_port: 465, username: '', password: '******', sender: 'alice@example.com', receivers: ['alice@example.com'] },
  },
];

type TestOutcome = 'sent' | 'failed' | 'blocked' | 'rate_limited';

const channelView = (channel: MockChannel) => ({
  id: channel.id,
  channel_type: channel.channel_type,
  name: channel.name,
  enabled: channel.enabled,
  source: 'user',
  editable: true,
  readable: true,
  config: channel.config,
  created_at: '2026-11-10T08:00:00',
  updated_at: null,
});

const PRIMARY_CHANNELS = {
  user_id: 'u-admin',
  is_primary: true,
  managed_in_system_settings: true,
  note: '主用户的推送渠道就是系统设置里的 .env 渠道，请在系统设置中管理。',
  max_channels: 0,
  channels: [
    { id: null, channel_type: 'feishu', name: '飞书', enabled: true, source: 'env', editable: false, readable: true, config: {}, created_at: null, updated_at: null },
    { id: null, channel_type: 'wechat', name: '企业微信', enabled: true, source: 'env', editable: false, readable: true, config: {}, created_at: null, updated_at: null },
  ],
  channel_types: [],
};

function scheduleFor(primary: boolean) {
  return {
    user_id: primary ? 'u-admin' : 'u-alice',
    is_primary: primary,
    managed_in_system_settings: primary,
    daily_plan: { enabled: true, times: ['17:30'], default_time: '18:00', earliest_time: '17:30' },
    preopen_push: { enabled: false, time: '08:15', default_time: '08:15', latest_time: '09:00' },
  };
}

interface MockOptions {
  role?: Role;
  modelKeyConfigured?: boolean;
  /** The primary user: read-only `.env` channels and schedule. */
  primary?: boolean;
  /** Initial personal channels (default: a Feishu group and an email account). */
  channels?: MockChannel[];
}

async function mockMultiUserApi(page: Page, options: MockOptions = {}) {
  const role = options.role ?? 'user';
  const primary = options.primary ?? false;
  const calls: Array<{ method: string; path: string }> = [];
  const channels: MockChannel[] = (options.channels ?? SEED_CHANNELS).map((channel) => ({ ...channel, config: { ...channel.config } }));
  const testOutcome: { current: TestOutcome } = { current: 'sent' };
  const settingsPuts: Array<{ items: Array<{ key: string; value: string }> }> = [];
  const channelBodies: Array<{ method: string; path: string; body: Record<string, unknown> }> = [];
  let nextId = 100;

  const channelList = () => ({
    user_id: 'u-alice',
    is_primary: false,
    managed_in_system_settings: false,
    note: '',
    max_channels: 10,
    channels: channels.map(channelView),
    channel_types: CHANNEL_CATALOG,
  });

  // Only the backend API: the Vite dev server also serves source modules under /src/api/.
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    calls.push({ method, path });

    if (path === '/api/v1/auth/status') {
      return json(route, { authMode: 'user_center', authEnabled: true, loggedIn: false, setupState: 'enabled' });
    }
    if (path === '/api/session') {
      return json(route, {
        authenticated: true,
        authorized: true,
        username: role === 'admin' ? 'Root' : 'Alice',
        role,
        csrfToken: 'csrf-test',
        loginUrl: 'https://user.example.test/login',
        logoutUrl: 'https://user.example.test/logout',
      });
    }
    if (path === '/api/v1/me') {
      return json(route, {
        user_id: role === 'admin' ? 'u-admin' : 'u-alice',
        handle: role === 'admin' ? 'root' : 'alice',
        display_name: role === 'admin' ? 'Root' : 'Alice',
        role,
        is_admin: role === 'admin',
        auth_mode: 'user_center',
        model_key_configured: options.modelKeyConfigured ?? true,
      });
    }
    if (path === '/api/v1/me/settings' && method === 'GET') return json(route, settingsFor(primary));
    if (path === '/api/v1/me/settings' && method === 'PUT') {
      const body = request.postDataJSON() as { items: Array<{ key: string; value: string }> };
      settingsPuts.push(body);
      const plan = body.items.find((item) => item.key === 'DAILY_PLAN_TIME');
      if (plan && plan.value < '17:30') {
        return json(route, {
          error: 'plan_time_too_early',
          message: 'The next-day plan needs the day\'s board data: the earliest time is 17:30',
          detail: { issues: [{ key: 'DAILY_PLAN_TIME', code: 'plan_time_too_early', message: 'x', severity: 'error', earliest_time: '17:30' }] },
        }, 400);
      }
      return json(route, settingsFor(primary));
    }
    if (path === '/api/v1/me/settings/test-model') {
      return json(route, { success: false, model: 'openai/x', error_code: 'model_call_failed', error_category: 'auth_failed', http_status: 401, message: 'x' });
    }
    if (path === '/api/v1/me/schedule') return json(route, scheduleFor(primary));
    if (path === '/api/v1/me/notification-channels') {
      if (primary) return json(route, PRIMARY_CHANNELS);
      if (method === 'GET') return json(route, channelList());
      if (method === 'POST') {
        const body = request.postDataJSON() as { channel_type: string; name?: string; enabled?: boolean; config: Record<string, unknown> };
        channelBodies.push({ method, path, body });
        const type = CHANNEL_CATALOG.find((item) => item.channel_type === body.channel_type);
        if (!type) return json(route, { error: 'invalid_channel_type', message: 'Unsupported channel type' }, 400);
        const unsafe = type.fields.find((item) => item.kind === 'url' && String(body.config[item.name] ?? '').startsWith('http://'));
        if (unsafe) {
          return json(route, {
            error: 'unsafe_target',
            message: 'The channel target is not allowed',
            detail: { issues: [{ key: unsafe.name, code: 'scheme_not_allowed', message: 'Not an acceptable value', severity: 'error' }] },
          }, 400);
        }
        const config: Record<string, unknown> = {};
        for (const item of type.fields) {
          const value = body.config[item.name];
          config[item.name] = item.secret ? (value ? '******' : '') : (value ?? '');
        }
        const created: MockChannel = { id: nextId++, channel_type: type.channel_type, name: body.name || type.label, enabled: body.enabled ?? true, config };
        channels.push(created);
        return json(route, channelView(created), 201);
      }
    }
    const channelMatch = path.match(/^\/api\/v1\/me\/notification-channels\/(\d+)(\/test)?$/);
    if (channelMatch && !primary) {
      const id = Number(channelMatch[1]);
      const channel = channels.find((item) => item.id === id);
      if (!channel) return json(route, { error: 'channel_not_found', message: 'Push channel not found' }, 404);
      if (channelMatch[2] && method === 'POST') {
        if (testOutcome.current === 'rate_limited') {
          return json(route, { error: 'rate_limited', message: 'Wait a few seconds before testing this channel again' }, 429);
        }
        if (testOutcome.current === 'sent') return json(route, { success: true, status: 'sent', error_category: null, latency_ms: 412 });
        if (testOutcome.current === 'blocked') return json(route, { success: false, status: 'blocked', error_category: 'target_not_allowed', latency_ms: null });
        return json(route, { success: false, status: 'failed', error_category: 'send_failed', latency_ms: 230 });
      }
      if (method === 'PUT') {
        const body = request.postDataJSON() as { name?: string; enabled?: boolean; config?: Record<string, unknown> };
        channelBodies.push({ method, path, body });
        if (body.name !== undefined) channel.name = body.name;
        if (body.enabled !== undefined) channel.enabled = body.enabled;
        for (const [key, value] of Object.entries(body.config ?? {})) {
          const secret = CHANNEL_CATALOG.find((item) => item.channel_type === channel.channel_type)?.fields.find((item) => item.name === key)?.secret;
          if (secret && (value === '' || value === '******')) continue;
          channel.config[key] = secret ? '******' : value;
        }
        return json(route, channelView(channel));
      }
      if (method === 'DELETE') {
        channels.splice(channels.indexOf(channel), 1);
        return json(route, channelList());
      }
    }
    if (path === '/api/v1/me/usage') {
      return json(route, {
        period: 'month', from_date: '2026-11-01', to_date: '2026-11-09', total_calls: 3, total_prompt_tokens: 1200,
        total_completion_tokens: 3400, total_tokens: 4600, by_call_type: [], by_model: [], recent_calls: [],
      });
    }
    if (path === '/api/v1/history') return json(route, { total: 0, page: 1, limit: 20, items: [] });
    if (path === '/api/v1/history/stocks') return json(route, { total: 0, items: [] });
    if (path === '/api/v1/stocks/watchlist') return json(route, { stock_codes: [] });
    if (path === '/api/v1/analysis/tasks') return json(route, { total: 0, pending: 0, processing: 0, tasks: [] });
    if (path === '/api/v1/agent/skills') return json(route, { skills: [], default_skill_id: '' });
    return json(route, {});
  });

  return { calls, channels, testOutcome, settingsPuts, channelBodies };
}

test.describe('multi-user web UI', () => {
  test('keeps the account in the lower-left corner clear of the navigation', async ({ page }, testInfo) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await mockMultiUserApi(page, { role: 'admin' });
    await mockPlanApi(page, { includeBaseApi: false });
    await page.goto('/mine');
    await expect(page.getByRole('heading', { level: 1, name: '我的股票' })).toBeVisible();
    const account = page.getByTestId('current-user-badge');
    const nav = page.getByRole('navigation', { name: '主导航' });
    await expect(account).toContainText('管理员');
    await expect(nav.getByTestId('current-user-badge')).toHaveCount(0);

    for (const height of [800, 440]) {
      await page.setViewportSize({ width: 1280, height });
      const footer = await account.boundingBox();
      const links = await nav.boundingBox();
      expect(footer).not.toBeNull();
      expect(links).not.toBeNull();
      expect(footer!.x).toBeLessThan(40);
      expect(height - footer!.y - footer!.height).toBeCloseTo(16, 0);
      expect(links!.y + links!.height).toBeLessThan(footer!.y);
      const logout = nav.getByRole('link', { name: '退出' });
      await logout.scrollIntoViewIfNeeded();
      await expect(logout).toBeInViewport();
      if (height === 800) {
        await page.screenshot({ path: testInfo.outputPath('plan-account-position.png') });
      }
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(account).toBeHidden();
    await expect(nav.getByRole('link', { name: '退出' })).toBeInViewport();
  });

  test('personal settings: groups, source badges, masked secret, test-model category', async ({ page }, testInfo) => {
    await mockMultiUserApi(page, { role: 'user' });
    await page.goto('/settings/personal');

    await expect(page.getByRole('heading', { level: 1, name: '个人设置' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('current-user-badge')).toContainText('Alice');
    await expect(page.getByTestId('personal-source-LITELLM_MODEL')).toHaveText('我的设置');
    await expect(page.getByTestId('personal-source-LLM_TEMPERATURE')).toHaveText('系统默认');
    await expect(page.getByTestId('personal-source-OPENAI_BASE_URL')).toHaveText('未配置');
    await expect(page.locator('#setting-OPENAI_API_KEY')).toHaveValue('******');
    await page.screenshot({ path: testInfo.outputPath('personal-settings.png'), fullPage: true });

    await page.getByRole('button', { name: '测试模型连接' }).click();
    await expect(page.getByText(/服务商拒绝了 API Key/)).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('personal-settings-test-model.png'), fullPage: true });
  });

  test('a regular user cannot reach the system settings or the screening run', async ({ page }, testInfo) => {
    await mockMultiUserApi(page, { role: 'user' });

    await page.goto('/settings');
    await expect(page).toHaveURL(/\/settings\/personal$/, { timeout: 20_000 });

    await page.goto('/screening');
    await expect(page.getByRole('heading', { name: '需要管理员权限' })).toBeVisible({ timeout: 20_000 });
    const nav = page.getByRole('navigation', { name: '主导航' });
    await expect(nav.getByRole('link', { name: '选股', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: '设置', exact: true })).toHaveCount(0);
    await expect(nav.getByRole('link', { name: '个人设置' })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('admin-required.png'), fullPage: true });
  });

  test('model-key banner on the home page links to the personal settings', async ({ page }, testInfo) => {
    await mockMultiUserApi(page, { role: 'user', modelKeyConfigured: false });
    await page.goto('/home');

    const banner = page.getByTestId('model-key-banner');
    await expect(banner).toContainText('尚未配置你自己的模型 Key', { timeout: 20_000 });
    await expect(page.getByRole('button', { name: '大盘复盘' })).toHaveCount(0);
    await page.screenshot({ path: testInfo.outputPath('home-model-key-banner.png'), fullPage: true });
    await banner.getByRole('link', { name: '去个人设置' }).click();
    await expect(page).toHaveURL(/\/settings\/personal$/);
  });

  test('usage page reads the own usage for a regular user', async ({ page }, testInfo) => {
    const api = await mockMultiUserApi(page, { role: 'user' });
    await page.goto('/usage');

    await expect(page.getByRole('heading', { name: 'Token 用量监控' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('4,600').first()).toBeVisible();
    expect(api.calls.some((call) => call.path === '/api/v1/me/usage')).toBe(true);
    expect(api.calls.some((call) => call.path === '/api/v1/usage/dashboard')).toBe(false);
    await page.screenshot({ path: testInfo.outputPath('usage-own.png'), fullPage: true });
  });

  test('an administrator keeps the system settings and gets a switch to the personal ones', async ({ page }) => {
    await mockMultiUserApi(page, { role: 'admin' });
    await page.goto('/settings/personal');

    await expect(page.getByTestId('settings-scope-tabs')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('settings-scope-tabs').getByRole('link', { name: '系统设置' })).toHaveAttribute('href', '/settings');
    await expect(page.getByTestId('current-user-badge')).toContainText('管理员');
  });
});

// --- P3b: personal push channels, push & schedule, plan push status ---------------------------------------------

function planFixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(HERE, 'plan', name), 'utf8')) as Record<string, unknown>;
}

async function gotoPersonalTab(page: Page, tab: string) {
  await page.goto('/settings/personal');
  await expect(page.getByRole('heading', { level: 1, name: '个人设置' })).toBeVisible({ timeout: 20_000 });
  await page.getByRole('navigation', { name: '个人设置分组' }).getByRole('button', { name: new RegExp(`^${tab}`) }).click();
}

test.describe('personal push channels and schedule (P3b)', () => {
  test('channels: masked list, add with an unsafe address rejected inline, then saved; test message results', async ({ page }) => {
    const api = await mockMultiUserApi(page, { role: 'user' });
    await gotoPersonalTab(page, '推送渠道');

    const feishu = page.getByTestId('channel-1');
    await expect(feishu).toContainText('我的飞书群');
    await expect(feishu.getByTestId('channel-config-1')).toContainText('******');
    await expect(page.getByTestId('channel-count')).toContainText('已添加 2 / 10 个渠道');
    // Never a plaintext address or secret in the DOM.
    expect(await page.locator('body').innerText()).not.toContain('open.feishu');
    await page.screenshot({ path: prShot('p3b-channels-list'), fullPage: true });

    // Add: the unsafe address is rejected by the server and explained under the field.
    await page.getByRole('button', { name: '添加渠道' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('渠道类型').selectOption('feishu');
    await dialog.getByLabel(/名称/).fill('备用群');
    await dialog.getByLabel(/Webhook 地址/).fill('http://10.0.0.8/hook/abc');
    await page.screenshot({ path: prShot('p3b-channel-add-dialog'), fullPage: true });
    await dialog.getByRole('button', { name: '保存渠道' }).click();
    await expect(dialog.getByText('目标地址不被允许')).toBeVisible();
    await expect(dialog.getByText('只允许 https 地址。')).toBeVisible();
    await page.screenshot({ path: prShot('p3b-channel-add-unsafe-target'), fullPage: true });

    await dialog.getByLabel(/Webhook 地址/).fill('https://open.feishu.cn/open-apis/bot/v2/hook/abc');
    await dialog.getByRole('button', { name: '保存渠道' }).click();
    await expect(page.getByText('已保存渠道：备用群')).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByTestId('channel-count')).toContainText('已添加 3 / 10 个渠道');
    expect(api.channelBodies.at(-1)?.body).toEqual({
      channel_type: 'feishu',
      name: '备用群',
      enabled: true,
      config: { webhook_url: 'https://open.feishu.cn/open-apis/bot/v2/hook/abc' },
    });
    await page.screenshot({ path: prShot('p3b-channels-list-after-add'), fullPage: true });

    // Test message: delivered, then not delivered, then rate limited.
    await page.getByRole('button', { name: '发送测试消息：我的飞书群' }).click();
    await expect(page.getByTestId('channel-test-1')).toContainText('测试消息已发送（412 ms）');
    await page.screenshot({ path: prShot('p3b-channel-test-sent'), fullPage: true });

    api.testOutcome.current = 'failed';
    await page.getByRole('button', { name: '发送测试消息：我的飞书群' }).click();
    await expect(page.getByTestId('channel-test-1')).toContainText('测试消息未送达');
    await expect(page.getByTestId('channel-test-1')).toContainText('渠道没有接受这条消息');
    await page.screenshot({ path: prShot('p3b-channel-test-failed'), fullPage: true });

    api.testOutcome.current = 'rate_limited';
    await page.getByRole('button', { name: '发送测试消息：我的飞书群' }).click();
    await expect(page.getByTestId('channel-test-1')).toContainText('操作太频繁');
    await page.screenshot({ path: prShot('p3b-channel-test-rate-limited'), fullPage: true });
  });

  test('channels: edit keeps the masked secret, delete asks for confirmation', async ({ page }) => {
    const api = await mockMultiUserApi(page, { role: 'user' });
    await gotoPersonalTab(page, '推送渠道');

    await page.getByRole('button', { name: '编辑渠道：我的飞书群' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByLabel(/Webhook 地址/)).toHaveValue('******');
    await expect(dialog.getByRole('button', { name: '保存渠道' })).toBeDisabled();
    await dialog.getByLabel(/关键词/).fill('明日计划');
    await page.screenshot({ path: prShot('p3b-channel-edit-dialog'), fullPage: true });
    await dialog.getByRole('button', { name: '保存渠道' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    // Only the changed field went over the wire; the secret was left alone.
    expect(api.channelBodies.at(-1)?.body).toEqual({ config: { keyword: '明日计划' } });

    await page.getByRole('switch', { name: '启用渠道：我的飞书群' }).click();
    await expect(page.getByTestId('channel-1')).toContainText('未启用');

    await page.getByRole('button', { name: '删除渠道：我的飞书群' }).click();
    await expect(page.getByText('删除推送渠道')).toBeVisible();
    await page.screenshot({ path: prShot('p3b-channel-delete-confirm'), fullPage: true });
    await page.getByRole('button', { name: '删除', exact: true }).click();
    await expect(page.getByText('已删除渠道：我的飞书群')).toBeVisible();
    await expect(page.getByTestId('channel-1')).toHaveCount(0);
  });

  test('channels: the English UI', async ({ page }) => {
    await page.addInitScript(() => window.localStorage.setItem('dsa.uiLanguage', 'en'));
    await mockMultiUserApi(page, { role: 'user' });
    await page.goto('/settings/personal');
    await page.getByRole('navigation', { name: 'Personal settings groups' }).getByRole('button', { name: /^Push channels/ }).click();

    await expect(page.getByTestId('channel-count')).toContainText('2 of 10 channels added');
    await page.getByRole('button', { name: 'Add channel' }).click();
    await page.getByRole('dialog').getByLabel('Channel type').selectOption('telegram');
    await expect(page.getByRole('dialog').getByLabel(/Bot token/)).toBeVisible();
    await page.screenshot({ path: prShot('p3b-channel-add-dialog-en'), fullPage: true });
  });

  test('schedule: summary, range hints, plan_time_too_early inline, then saved', async ({ page }) => {
    const api = await mockMultiUserApi(page, { role: 'user' });
    await gotoPersonalTab(page, '推送与定时');

    await expect(page.getByTestId('schedule-summary-plan')).toContainText('次日计划：每个交易日 17:30 自动生成');
    await expect(page.getByTestId('schedule-summary-preopen')).toContainText('盘前推送：未开启');
    await expect(page.getByText(/最早可选 17:30/)).toBeVisible();
    await expect(page.getByText(/最晚可选 09:00/)).toBeVisible();
    await expect(page.getByText('未配置推送渠道，定时结果只在网页查看')).toHaveCount(0);
    await page.screenshot({ path: prShot('p3b-schedule'), fullPage: true });

    await page.getByLabel('生成时间').fill('16:00');
    await page.getByTestId('schedule-switch-PREOPEN_PUSH_ENABLED').check();
    await page.getByRole('button', { name: '保存推送与定时' }).click();
    await expect(page.getByTestId('schedule-plan').getByRole('alert')).toContainText('最早可选 17:30');
    await page.screenshot({ path: prShot('p3b-schedule-plan-time-too-early'), fullPage: true });

    await page.getByLabel('生成时间').fill('18:10');
    await page.getByRole('button', { name: '保存推送与定时' }).click();
    await expect(page.getByText('推送与定时已保存。')).toBeVisible();
    expect(api.settingsPuts.at(-1)).toEqual({
      items: [
        { key: 'DAILY_PLAN_TIME', value: '18:10' },
        { key: 'PREOPEN_PUSH_ENABLED', value: 'true' },
      ],
    });
    await page.screenshot({ path: prShot('p3b-schedule-saved'), fullPage: true });
  });

  test('schedule: without a channel the results stay on the web and the notice leads to the channel list', async ({ page }) => {
    await mockMultiUserApi(page, { role: 'user', channels: [] });
    await gotoPersonalTab(page, '推送与定时');

    await expect(page.getByText('未配置推送渠道，定时结果只在网页查看')).toBeVisible();
    await page.screenshot({ path: prShot('p3b-schedule-no-channel'), fullPage: true });
    await page.getByRole('button', { name: '去添加渠道' }).click();
    await expect(page.getByText(/还没有推送渠道/)).toBeVisible();
    await page.screenshot({ path: prShot('p3b-channels-empty'), fullPage: true });
  });

  test('legacy channel credential keys are not offered', async ({ page }) => {
    await mockMultiUserApi(page, { role: 'user' });
    await gotoPersonalTab(page, '推送偏好');

    await expect(page.getByTestId('personal-field-NOTIFICATION_QUIET_HOURS')).toBeVisible();
    await expect(page.getByTestId('personal-field-FEISHU_WEBHOOK_URL')).toHaveCount(0);
    await expect(page.getByTestId('personal-field-TELEGRAM_BOT_TOKEN')).toHaveCount(0);
    await expect(page.getByTestId('personal-field-DAILY_PLAN_ENABLED')).toHaveCount(0);
    await page.screenshot({ path: prShot('p3b-push-preferences-no-legacy-keys'), fullPage: true });
  });

  test('primary user: the .env channel types and the schedule are read-only', async ({ page }) => {
    await mockMultiUserApi(page, { role: 'admin', primary: true });
    await gotoPersonalTab(page, '推送渠道');

    await expect(page.getByText('你是主用户，推送渠道来自系统设置')).toBeVisible();
    await expect(page.getByTestId('channel-env-feishu')).toContainText('飞书');
    await expect(page.getByRole('button', { name: '添加渠道' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: /编辑渠道|删除渠道|发送测试消息/ })).toHaveCount(0);
    await page.screenshot({ path: prShot('p3b-channels-primary-readonly'), fullPage: true });

    await page.getByRole('navigation', { name: '个人设置分组' }).getByRole('button', { name: /^推送与定时/ }).click();
    await expect(page.getByTestId('schedule-summary-plan')).toBeVisible();
    await expect(page.getByRole('switch')).toHaveCount(0);
    await page.screenshot({ path: prShot('p3b-schedule-primary-readonly'), fullPage: true });
  });

  test('plan run: a personal push shows the per-channel results, and a missing model says so', async ({ page }) => {
    await mockMultiUserApi(page, { role: 'admin' });
    await mockPlanApi(page, { includeBaseApi: false });
    await page.route((url) => url.pathname === '/api/v1/system/config', (route) => json(route, planFixture('preferences/system-config.json')));
    await page.route('**/api/v1/system/scheduler/status', (route) => json(route, planFixture('preferences/scheduler-status.json')));
    await page.route('**/api/v1/data/overview', (route) => json(route, planFixture('preferences/data-overview.json')));
    const run = planFixture('latest-run.json') as { run: Record<string, unknown> };
    // A regular user's run labels the push step "个人渠道推送" (DailyPlanService._fresh_steps).
    const steps = (run.run.steps as Array<Record<string, unknown>>).map((step) => (step.key === 'push' ? { ...step, label: '个人渠道推送' } : step));
    const withPush = (push: Record<string, unknown>) => ({ run: { ...run.run, steps, push } });
    let latest = withPush({
      channel: 'personal',
      status: 'sent',
      sent_at: '2026-11-06T16:07:02+08:00',
      error: null,
      configured: true,
      reason: null,
      channels: [{ channel: 'feishu', success: true }, { channel: 'email', success: false }],
    });
    await page.route('**/api/v1/plan/runs/latest', (route) => json(route, latest));

    await page.goto('/preferences?tab=run');
    await expect(page.getByRole('heading', { level: 1, name: '设置' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('push-status')).toContainText('个人渠道推送：已发送');
    await expect(page.getByTestId('push-channels')).toContainText('飞书：已送达');
    await expect(page.getByTestId('push-channels')).toContainText('邮件：发送失败');
    await page.screenshot({ path: prShot('p3b-plan-push-personal'), fullPage: true });

    latest = withPush({ channel: 'personal', status: 'skipped', sent_at: null, error: null, configured: true, reason: 'no_model', channels: [] });
    await page.reload();
    await expect(page.getByTestId('push-status')).toContainText('未配置模型');
    await page.screenshot({ path: prShot('p3b-plan-push-no-model'), fullPage: true });

    latest = withPush({ channel: 'personal', status: 'skipped', sent_at: null, error: null, configured: false, reason: null, channels: [] });
    await page.reload();
    await expect(page.getByTestId('push-status')).toContainText('没有可用的推送渠道');
    await page.screenshot({ path: prShot('p3b-plan-push-no-channel'), fullPage: true });
  });
});
