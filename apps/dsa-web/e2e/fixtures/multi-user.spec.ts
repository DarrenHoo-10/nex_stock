import { expect, test, type Page, type Route } from '@playwright/test';

// Deterministic fixture spec for the multi-user Web UI (AUTH_MODE=user_center): no backend, every API call is
// mocked. Besides the assertions it writes screenshots (test-results/fixtures/**) for the PR description.

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

const SETTINGS = {
  user_id: 'u-alice',
  is_primary: false,
  managed_in_system_settings: false,
  groups: [
    {
      id: 'model',
      title: '模型',
      fields: [
        field({ key: 'LITELLM_MODEL', title: '主模型', value: 'openai/auto/deepseek-v4.1-flash', source: 'user', configured: true }),
        field({ key: 'OPENAI_API_KEY', title: 'OpenAI API Key', is_secret: true, ui_control: 'password', value: '******', source: 'user', inherits: false, configured: true }),
        field({ key: 'OPENAI_BASE_URL', title: 'OpenAI Base URL', inherits: false }),
        field({ key: 'LLM_TEMPERATURE', title: '温度', source: 'system_default', configured: true }),
      ],
    },
    {
      id: 'search',
      title: '搜索',
      fields: [field({ key: 'TAVILY_API_KEYS', group: 'search', title: 'Tavily API Keys', is_secret: true, ui_control: 'password', inherits: false })],
    },
    {
      id: 'position',
      title: '仓位规则',
      fields: [field({ key: 'POSITION_MAX_HOLDINGS', group: 'position', title: '最大持仓数', value: '8', source: 'user', configured: true })],
    },
  ],
};

async function mockMultiUserApi(page: Page, options: { role?: Role; modelKeyConfigured?: boolean } = {}) {
  const role = options.role ?? 'user';
  const calls: Array<{ method: string; path: string }> = [];

  // Only the backend API: the Vite dev server also serves source modules under /src/api/.
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    calls.push({ method: request.method(), path });

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
    if (path === '/api/v1/me/settings' && request.method() === 'GET') return json(route, SETTINGS);
    if (path === '/api/v1/me/settings/test-model') {
      return json(route, { success: false, model: 'openai/x', error_code: 'model_call_failed', error_category: 'auth_failed', http_status: 401, message: 'x' });
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

  return { calls };
}

test.describe('multi-user web UI', () => {
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
