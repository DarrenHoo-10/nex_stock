import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page, type Route } from '@playwright/test';
import { mockPlanApi, type RecordedCall } from './plan/mockPlanApi';

// Deterministic fixture spec for /preferences: no backend, every API call is mocked.

const PLAN_DIR = join(dirname(fileURLToPath(import.meta.url)), 'plan');

function fixture<T = Record<string, unknown>>(name: string): T {
  return JSON.parse(readFileSync(join(PLAN_DIR, name), 'utf8')) as T;
}

const WIDTHS: Array<[number, number]> = [
  [1366, 900],
  [1024, 768],
  [375, 812],
  [320, 640],
];

const TABS = [
  { id: 'rules', name: '仓位规则', heading: '仓位规则' },
  { id: 'models', name: '模型', heading: '模型' },
  { id: 'thresholds', name: '异动阈值', heading: '异动阈值' },
  { id: 'run', name: '运行和推送', heading: '运行和推送' },
  { id: 'data', name: '数据状态', heading: '数据状态' },
];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

interface PreferenceMocks {
  calls: RecordedCall[];
  configPuts: Array<Record<string, unknown>>;
  thresholdPuts: Array<{ overrides: Record<string, unknown> }>;
  runPosts: number;
  configGets: number;
  /** PUT /system/config behaviour. */
  configPutMode: { current: 'ok' | 'conflict' | 'invalid' };
  /** POST /plan/runs behaviour. */
  runMode: { current: 'ok' | 'conflict' };
}

async function mockPreferences(page: Page): Promise<PreferenceMocks> {
  const plan = await mockPlanApi(page);
  const mocks: PreferenceMocks = {
    calls: plan.calls,
    configPuts: [],
    thresholdPuts: [],
    runPosts: 0,
    configGets: 0,
    configPutMode: { current: 'ok' },
    runMode: { current: 'ok' },
  };

  await page.route((url) => url.pathname === '/api/v1/system/config', async (route) => {
    const method = route.request().method();
    if (method === 'GET') {
      mocks.configGets += 1;
      await json(route, fixture('preferences/system-config.json'));
      return;
    }
    if (method === 'PUT') {
      mocks.configPuts.push(route.request().postDataJSON() as Record<string, unknown>);
      if (mocks.configPutMode.current === 'conflict') {
        await json(route, { error: 'config_version_conflict', message: '配置已被修改，请刷新后重试' }, 409);
      } else if (mocks.configPutMode.current === 'invalid') {
        await json(
          route,
          {
            error: 'validation_error',
            message: '配置校验失败',
            issues: [
              { key: 'POSITION_MAX_TOTAL_PCT', code: 'out_of_range', message: '总仓位不能超过 100', severity: 'error' },
            ],
          },
          400,
        );
      } else {
        await json(route, {
          success: true,
          config_version: 'cfg-v2',
          applied_count: 1,
          skipped_masked_count: 0,
          reload_triggered: true,
          updated_keys: [],
          warnings: [],
        });
      }
      return;
    }
    await route.fallback();
  });
  await page.route('**/api/v1/system/scheduler/status', (route) => json(route, fixture('preferences/scheduler-status.json')));
  await page.route('**/api/v1/data/overview', (route) => json(route, fixture('preferences/data-overview.json')));

  // Thresholds persist across GET/PUT within one test and use the wrapped `{items}` shape of the real API.
  let overrides: Record<string, number> = {};
  const baseThresholds = fixture<unknown[]>('thresholds.json') as Array<Record<string, unknown>>;
  const thresholdItems = () =>
    baseThresholds.map((item) => {
      const key = String(item.key);
      const overridden = key in overrides;
      return { ...item, value: overridden ? overrides[key] : item.default, overridden };
    });
  await page.route('**/api/v1/boards/thresholds', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { overrides: Record<string, number> };
      mocks.thresholdPuts.push(body);
      overrides = body.overrides;
    }
    await json(route, { items: thresholdItems() });
  });

  // Manual run: the shared plan mock answers 202; this spec also needs the 409 branch.
  await page.route('**/api/v1/plan/runs', async (route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    mocks.runPosts += 1;
    if (mocks.runMode.current === 'conflict') {
      await json(route, { error: 'daily_plan_run_conflict', message: '已有运行在进行中' }, 409);
      return;
    }
    const run = (fixture('latest-run.json') as { run: Record<string, unknown> }).run;
    await json(route, { ...run, status: 'running', trigger: 'manual', finished_at: null, duration_sec: null }, 202);
  });
  return mocks;
}

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
}

async function gotoTab(page: Page, tab: string) {
  await page.goto(`/preferences?tab=${tab}`);
  await expect(page.getByRole('heading', { level: 1, name: '设置' })).toBeVisible({ timeout: 20_000 });
}

test.describe('plan preferences', () => {
  test('every tab renders and the tablist supports deep links and arrow keys', async ({ page }) => {
    await mockPreferences(page);
    await page.goto('/preferences');
    await expect(page.getByRole('heading', { level: 1, name: '设置' })).toBeVisible({ timeout: 20_000 });

    const tablist = page.getByRole('tablist', { name: '设置分类' });
    await expect(tablist.getByRole('tab')).toHaveCount(5);
    await expect(tablist.getByRole('tab', { name: '仓位规则' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tabpanel', { name: '仓位规则' }).getByLabel('止损幅度（%）')).toHaveValue('10');
    await expect(page.getByRole('tabpanel', { name: '仓位规则' }).getByLabel('总仓位上限（%）')).toHaveValue('100');
    await expect(page.getByTestId('position-derived')).toContainText('现在是 100.0%');

    // Arrow keys move the selection and the focus; Home/End jump.
    await tablist.getByRole('tab', { name: '仓位规则' }).focus();
    await page.keyboard.press('ArrowDown');
    await expect(tablist.getByRole('tab', { name: '模型' })).toBeFocused();
    await expect(tablist.getByRole('tab', { name: '模型' })).toHaveAttribute('aria-selected', 'true');
    await expect(page).toHaveURL(/tab=models/);
    await page.keyboard.press('End');
    await expect(tablist.getByRole('tab', { name: '数据状态' })).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('Home');
    await expect(tablist.getByRole('tab', { name: '仓位规则' })).toHaveAttribute('aria-selected', 'true');

    for (const tab of TABS) {
      await tablist.getByRole('tab', { name: tab.name, exact: true }).click();
      const panel = page.getByRole('tabpanel', { name: tab.name, exact: true });
      await expect(panel).toBeVisible();
      await expect(panel.getByRole('heading', { level: 2, name: tab.heading, exact: true })).toBeVisible();
    }

    await gotoTab(page, 'thresholds');
    await expect(page.getByRole('tab', { name: '异动阈值' })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tabpanel', { name: '异动阈值' }).getByLabel('温和放量 当前值')).toHaveValue('1.5');
  });

  test('position rules save sends the changed items with the config version', async ({ page }) => {
    const mocks = await mockPreferences(page);
    await gotoTab(page, 'rules');
    const panel = page.getByRole('tabpanel', { name: '仓位规则' });

    await expect(panel.getByRole('button', { name: '保存规则' })).toBeDisabled();
    await panel.getByLabel('止损幅度（%）').fill('8');
    await panel.getByLabel('最多持有（只）').fill('2');
    await expect(page.getByTestId('position-derived')).toContainText('现在是 50.0%');
    await panel.getByLabel('止损方式').selectOption('nearer');
    await panel.getByRole('button', { name: '保存规则' }).click();

    await expect.poll(() => mocks.configPuts.length).toBe(1);
    expect(mocks.configPuts[0]).toMatchObject({
      config_version: 'cfg-v1',
      mask_token: '******',
      reload_now: true,
      items: [
        { key: 'POSITION_STOP_LOSS_PCT', value: '8' },
        { key: 'POSITION_MAX_HOLDINGS', value: '2' },
        { key: 'POSITION_STOP_MODE', value: 'nearer' },
      ],
    });
    await expect(page.getByRole('status').filter({ hasText: '仓位规则已保存' })).toBeVisible();
    // The sizing try-out is an optional backend call; the explanation text is always there.
    await expect(panel.getByText('买入股数 =', { exact: false })).toBeVisible();
  });

  test('a 409 shows the reload prompt and reloads the config; a 400 marks the field', async ({ page }) => {
    const mocks = await mockPreferences(page);
    await gotoTab(page, 'rules');
    const panel = page.getByRole('tabpanel', { name: '仓位规则' });

    mocks.configPutMode.current = 'conflict';
    await panel.getByLabel('止损幅度（%）').fill('8');
    await panel.getByRole('button', { name: '保存规则' }).click();
    const alert = panel.getByRole('alert').filter({ hasText: '配置已变化' });
    await expect(alert).toBeVisible();
    const before = mocks.configGets;
    await alert.getByRole('button', { name: '重新加载' }).click();
    await expect.poll(() => mocks.configGets).toBeGreaterThan(before);
    await expect(panel.getByLabel('止损幅度（%）')).toHaveValue('10');
    await expect(panel.getByRole('alert').filter({ hasText: '配置已变化' })).toHaveCount(0);

    mocks.configPutMode.current = 'invalid';
    await panel.getByLabel('总仓位上限（%）').fill('90');
    await panel.getByRole('button', { name: '保存规则' }).click();
    await expect(panel.getByText('总仓位不能超过 100')).toBeVisible();
    await expect(panel.getByLabel('总仓位上限（%）')).toHaveAttribute('aria-invalid', 'true');
  });

  test('models are shown by role over the existing keys and save only changed keys', async ({ page }) => {
    const mocks = await mockPreferences(page);
    await gotoTab(page, 'models');
    const panel = page.getByRole('tabpanel', { name: '模型' });

    await expect(panel.getByLabel('分析模型')).toHaveValue('deepseek/deepseek-v4-flash');
    await expect(panel.getByLabel('助手模型')).toHaveValue('');
    await expect(panel.getByLabel('识图模型')).toHaveValue('gemini/gemini-3-flash-preview');
    await expect(panel.getByRole('link', { name: '高级设置' })).toHaveAttribute('href', '/settings?category=ai_model');
    await expect(panel.getByText('密钥已配置', { exact: false }).first()).toBeVisible();
    // Secret values never reach the DOM.
    expect(await page.locator('body').innerText()).not.toContain('******');

    await panel.getByLabel('助手模型').selectOption('deepseek/deepseek-v4-pro');
    await panel.getByRole('button', { name: '保存模型设置' }).click();
    await expect.poll(() => mocks.configPuts.length).toBe(1);
    expect(mocks.configPuts[0]).toMatchObject({
      config_version: 'cfg-v1',
      items: [{ key: 'AGENT_LITELLM_MODEL', value: 'deepseek/deepseek-v4-pro' }],
    });
  });

  test('thresholds: grouped with evidence, save changed items, reset one, restore all', async ({ page }) => {
    const mocks = await mockPreferences(page);
    await gotoTab(page, 'thresholds');
    const panel = page.getByRole('tabpanel', { name: '异动阈值' });

    await expect(panel.getByRole('heading', { level: 3, name: '放量与超额排名' })).toBeVisible();
    await expect(panel.getByRole('heading', { level: 3, name: '上涨家数与涨停' })).toBeVisible();
    await expect(panel.getByRole('list', { name: '依据等级说明' }).or(panel.getByLabel('依据等级说明'))).toContainText('单一来源或经验说法');
    await expect(panel.getByText('依据：中').first()).toBeVisible();
    await expect(panel.getByText('依据：推断').first()).toBeVisible();
    await expect(panel.getByText('默认 1.5倍')).toBeVisible();

    await panel.getByLabel('温和放量 当前值').fill('2');
    await panel.getByLabel('强势排名 当前值').fill('15');
    await panel.getByRole('button', { name: '保存阈值' }).click();
    await expect.poll(() => mocks.thresholdPuts.length).toBe(1);
    expect(mocks.thresholdPuts[0]).toEqual({ overrides: { amount_ratio_mild: 2, strong_rank_pct: 15 } });
    await expect(panel.getByLabel('温和放量 当前值')).toHaveValue('2');
    await expect(panel.getByText('已修改')).toHaveCount(2);

    // Reset one item as a draft, then save: the other override stays.
    await panel.getByRole('button', { name: '强势排名 恢复默认' }).click();
    await expect(panel.getByLabel('强势排名 当前值')).toHaveValue('10');
    await panel.getByRole('button', { name: '保存阈值' }).click();
    await expect.poll(() => mocks.thresholdPuts.length).toBe(2);
    expect(mocks.thresholdPuts[1]).toEqual({ overrides: { amount_ratio_mild: 2 } });

    await panel.getByRole('button', { name: '全部恢复默认' }).click();
    await page.getByRole('dialog', { name: '全部恢复默认' }).getByRole('button', { name: '确认恢复' }).click();
    await expect.poll(() => mocks.thresholdPuts.length).toBe(3);
    expect(mocks.thresholdPuts[2]).toEqual({ overrides: {} });
    await expect(panel.getByLabel('温和放量 当前值')).toHaveValue('1.5');
  });

  test('thresholds show the service-unavailable state', async ({ page }) => {
    await mockPreferences(page);
    await page.route('**/api/v1/boards/thresholds', (route) =>
      json(route, { error: 'daily_plan_unavailable', message: '次日计划服务未就绪' }, 503),
    );
    await gotoTab(page, 'thresholds');
    const panel = page.getByRole('tabpanel', { name: '异动阈值' });
    await expect(panel.getByRole('alert')).toContainText('次日计划服务未就绪');
    await expect(panel.getByRole('button', { name: '重试' })).toBeVisible();
  });

  test('run and push: schedule, Feishu status without the webhook, latest run, preview and the 409 run', async ({
    page,
  }) => {
    const mocks = await mockPreferences(page);
    await gotoTab(page, 'run');
    const panel = page.getByRole('tabpanel', { name: '运行和推送' });

    await expect(panel.getByLabel('运行时间')).toHaveValue('16:00');
    await expect(panel.getByRole('switch', { name: '每个交易日自动生成次日计划' })).toBeChecked();
    await expect(page.getByTestId('scheduler-status')).toContainText('已启用');
    await expect(page.getByTestId('feishu-status')).toHaveText('已配置');
    await expect(panel.getByRole('link', { name: '在高级设置里配置' })).toHaveAttribute(
      'href',
      '/settings?category=notification',
    );
    await expect(panel.getByRole('list', { name: '运行步骤' }).getByRole('listitem')).toHaveCount(5);
    await expect(page.getByTestId('push-status')).toContainText('已发送');
    await expect(page.getByTestId('push-preview')).toContainText('持仓：立讯精密 持有');
    await expect(page.getByTestId('push-preview').locator('strong').first()).toBeVisible();
    expect(await page.locator('body').innerText()).not.toContain('open.feishu');

    // Save schedule edits through the config API.
    await panel.getByLabel('运行时间').fill('1600x');
    await panel.getByRole('button', { name: '保存运行设置' }).click();
    await expect(panel.getByText('按 24 小时制填写')).toBeVisible();
    expect(mocks.configPuts).toHaveLength(0);
    await panel.getByLabel('运行时间').fill('16:30');
    await panel.getByRole('switch', { name: '每个交易日自动生成次日计划' }).uncheck();
    await panel.getByRole('button', { name: '保存运行设置' }).click();
    await expect.poll(() => mocks.configPuts.length).toBe(1);
    expect(mocks.configPuts[0]).toMatchObject({
      config_version: 'cfg-v1',
      items: [
        { key: 'SCHEDULE_TIME', value: '16:30' },
        { key: 'DAILY_PLAN_ENABLED', value: 'false' },
      ],
    });

    // Manual run: success, then the 409 toast.
    await panel.getByRole('button', { name: '立即运行' }).click();
    await expect(page.getByRole('status').filter({ hasText: '已开始运行' })).toBeVisible();
    expect(mocks.runPosts).toBe(1);
    mocks.runMode.current = 'conflict';
    await expect(panel.getByRole('button', { name: '立即运行' })).toBeEnabled();
    await panel.getByRole('button', { name: '立即运行' }).click();
    await expect(page.getByRole('status').filter({ hasText: '已有运行在进行中' })).toBeVisible();
    expect(mocks.runPosts).toBe(2);
  });

  test('data status shows datasets, providers and the latest run counts', async ({ page }) => {
    await mockPreferences(page);
    await gotoTab(page, 'data');
    const panel = page.getByRole('tabpanel', { name: '数据状态' });
    await expect(panel.getByTestId('run-counts')).toContainText('8 只');
    await expect(panel.getByTestId('run-counts')).toContainText('7 只');
    await expect(panel.getByRole('list', { name: '失败的股票' })).toContainText('中科曙光');
    await expect(panel.getByRole('region', { name: '数据集' })).toContainText('板块历史');
    await expect(panel.getByRole('region', { name: '数据集' })).toContainText('部分不足');
    await expect(panel.getByRole('region', { name: '数据集' })).toContainText('不可用');
    await expect(panel.getByRole('region', { name: '数据源' })).toContainText('AkShare');
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow at ${width}px`, async ({ page }) => {
      await mockPreferences(page);
      await page.setViewportSize({ width, height });

      for (const tab of TABS) {
        await gotoTab(page, tab.id);
        await expect(page.getByRole('tabpanel', { name: tab.name, exact: true })).toBeVisible();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(600);
        const sizes = await horizontalOverflow(page);
        expect(sizes.root, `${tab.id} root width`).toBeLessThanOrEqual(sizes.viewport);
        expect(sizes.body, `${tab.id} body width`).toBeLessThanOrEqual(sizes.viewport);
        const clipped = await page
          .getByRole('tabpanel', { name: tab.name, exact: true })
          .evaluate((element) => element.scrollWidth <= element.clientWidth + 1);
        expect(clipped, `${tab.id} panel content fits`).toBe(true);
        await page.screenshot({
          path: `test-results/fixtures/plan-preferences-${tab.id}-${width}.png`,
          fullPage: true,
          animations: 'disabled',
        });
      }

      await gotoTab(page, 'rules');
      await page.waitForTimeout(700);
      await page.screenshot({ path: `test-results/fixtures/plan-preferences-${width}.png`, animations: 'disabled' });
    });
  }
});
