import { expect, test, type Page } from '@playwright/test';
import { mockPicksApi } from './plan/mockPicksApi';
import { mockPlanApi } from './plan/mockPlanApi';

// Deterministic fixture spec for /picks (机会选股): no backend, every API call is mocked.

const WIDTHS: Array<[number, number]> = [
  [1366, 900],
  [1024, 768],
  [375, 812],
  [320, 640],
];

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
}

async function openPicks(page: Page, query = '') {
  await page.goto(`/picks${query}`);
  // The first load in a worker pays the Vite on-demand compile of the lazy page chunks.
  await expect(page.getByRole('heading', { level: 1, name: '机会选股' })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: '放量突破', exact: true })).toBeVisible({ timeout: 20_000 });
}

test.describe('plan picks page', () => {
  test('runs a screening task, shows progress, results and adds a pick to the watchlist', async ({ page }) => {
    await mockPlanApi(page);
    const picks = await mockPicksApi(page);
    await openPicks(page);

    // Strategy chips come from the screening strategies API; the first one is selected.
    const chips = page.getByRole('group', { name: '策略' });
    await expect(chips.getByRole('button')).toHaveCount(4);
    await expect(chips.getByRole('button', { name: '放量突破' })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByText('放量突破近期平台，适合跟随短线动量。')).toBeVisible();
    await chips.getByRole('button', { name: '缩量回踩' }).click();
    await expect(chips.getByRole('button', { name: '缩量回踩' })).toHaveAttribute('aria-pressed', 'true');
    await expect(chips.getByRole('button', { name: '放量突破' })).toHaveAttribute('aria-pressed', 'false');
    await chips.getByRole('button', { name: '放量突破' }).click();

    await expect(page.getByRole('heading', { name: '还没有选股结果' })).toBeVisible();
    await page.getByRole('button', { name: '开始选股' }).click();

    // Progress while polling (the mock reports `processing` once before completing).
    const progress = page.getByTestId('pick-progress');
    await expect(progress).toBeVisible();
    await expect(progress).toContainText('正在计算因子并筛选候选股');
    await expect(progress.getByRole('progressbar', { name: '选股进度' })).toHaveAttribute('aria-valuenow', '40');
    await expect(page.getByRole('button', { name: '选股中…' })).toBeDisabled();

    // Results.
    const cards = page.locator('li.cand');
    await expect(cards).toHaveCount(4, { timeout: 15_000 });
    expect(picks.calls.some((call) => call.method === 'POST' && call.path === '/api/v1/screening/screen/tasks')).toBe(true);
    const start = picks.calls.find((call) => call.method === 'POST' && call.path === '/api/v1/screening/screen/tasks');
    expect(start?.body).toMatchObject({ market: 'cn', strategy: 'volume_breakout' });
    expect((start?.body as { scope?: unknown }).scope).toBeUndefined();
    await expect(page.locator('.cond-line')).toContainText('放量突破');
    await expect(page.locator('.cond-line')).toContainText('全部 A 股');
    await expect(page.locator('.cond-line')).toContainText('共 4 只');

    const first = cards.nth(0);
    await expect(first).toContainText('华工科技');
    await expect(first).toContainText('000988');
    await expect(first).toContainText('48.35');
    await expect(first).toContainText('+4.21%');
    await expect(first).toContainText('短线 1–5 天');
    await expect(first).toContainText('回踩 46.8–47.2 元缩量企稳，47.0 元附近买入');
    await expect(first).toContainText('收盘跌破 45.5 元');
    await expect(first).toContainText('板块只启动 1 天，持续性待确认');
    await expect(cards.nth(1)).toContainText('短线 1–5 天');
    await expect(cards.nth(1)).toContainText('波段 2–4 周');
    // The last candidate has none of the optional fields: it falls back to watch items, invalidators and risks.
    const fallback = cards.nth(3);
    await expect(fallback).toContainText('开板后能否回封；次日量能是否放大');
    await expect(fallback).toContainText('放量跌破涨停价下方 5%');
    await expect(fallback).toContainText('涨停开板两次，封板不稳');
    await expect(fallback).toContainText('涨停开板两次，只列出不建议追');
    await expect(fallback.getByText('短线 1–5 天')).toHaveCount(0);

    // Horizon filter: candidates without horizons stay visible.
    await page.getByRole('group', { name: '周期' }).getByRole('button', { name: '波段' }).click();
    await expect(cards).toHaveCount(3);
    await expect(page.locator('li.cand', { hasText: '华工科技' })).toHaveCount(0);
    await page.getByRole('group', { name: '周期' }).getByRole('button', { name: '全部' }).click();
    await expect(cards).toHaveCount(4);

    // 加入自选.
    await first.getByRole('button', { name: '把 华工科技 加入自选' }).click();
    await expect(page.getByRole('status').filter({ hasText: '已把 华工科技 加入自选' })).toBeVisible();
    await expect(first.getByRole('button', { name: '华工科技 已在自选' })).toBeDisabled();
    const add = picks.calls.find((call) => call.method === 'POST' && call.path === '/api/v1/stocks/watchlist/add');
    expect(add?.body).toMatchObject({ stock_code: '000988' });

    // 问助手 opens the assistant on that stock (华工科技 is short-term only).
    await first.getByRole('button', { name: '问助手：华工科技' }).click();
    await expect(page.getByTestId('assistant-context-chip')).toHaveText('当前：华工科技 · 短线');
  });

  test('limits the run to the board from ?board= and sends it as a topic', async ({ page }) => {
    await mockPlanApi(page);
    const picks = await mockPicksApi(page);
    await openPicks(page, '?board=industry%3ABK0448');

    const scope = page.getByTestId('pick-scope');
    await expect(scope).toContainText('范围：通信设备板块');
    await expect(page.getByRole('heading', { name: '还没有选股结果' })).toBeVisible();
    await expect(page.getByText('会在「通信设备」板块的成分股里筛选')).toBeVisible();
    await page.getByRole('button', { name: '开始选股' }).click();
    await expect(page.locator('li.cand')).toHaveCount(4, { timeout: 15_000 });
    const start = picks.calls.find((call) => call.method === 'POST' && call.path === '/api/v1/screening/screen/tasks');
    expect(start?.body).toMatchObject({
      strategy: 'volume_breakout',
      scope: { boards: [], topics: [{ name: '通信设备', source: 'industry' }], topic_match: 'any' },
    });

    // Clearing the scope drops the parameter.
    await scope.getByRole('button', { name: '清除范围' }).click();
    await expect(page).toHaveURL(/\/picks$/);
    await expect(page.getByTestId('pick-scope')).toHaveCount(0);
  });

  test('does not offer a board scope that cannot be picked', async ({ page }) => {
    await mockPlanApi(page);
    await mockPicksApi(page);
    // 机器人执行器 is single-stock driven: the board detail reports can_pick = false.
    await openPicks(page, '?board=concept%3ABK1201');
    await expect(page.getByText('不按板块选股')).toBeVisible();
    await expect(page.getByRole('button', { name: '开始选股' })).toBeDisabled();
    await page.getByRole('button', { name: '清除范围' }).click();
    await expect(page.getByRole('button', { name: '开始选股' })).toBeEnabled();
  });

  test('shows the last matching run on revisit', async ({ page }) => {
    await mockPlanApi(page);
    const picks = await mockPicksApi(page, { history: 'latest', watchlist: ['002281'] });
    await openPicks(page);
    await expect(page.locator('li.cand')).toHaveCount(4, { timeout: 15_000 });
    await expect(page.locator('.cond-line')).toContainText('上次结果');
    await expect(page.locator('.cond-line')).toContainText('运行于 11-0');
    // The newest run without a topic scope is shown (the board-scoped run is skipped) and its strategy preselected.
    expect(picks.calls.some((call) => call.path === '/api/v1/screening/history/run-fixture-1')).toBe(true);
    await expect(page.getByRole('button', { name: '放量突破', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('li.cand', { hasText: '光迅科技' }).getByRole('button', { name: '光迅科技 已在自选' })).toBeDisabled();
    expect(picks.calls.some((call) => call.method === 'POST' && call.path === '/api/v1/screening/screen/tasks')).toBe(false);
  });

  test('shows an error with retry when the task fails', async ({ page }) => {
    await mockPlanApi(page);
    await mockPicksApi(page, { task: 'failed', processingPolls: 0 });
    await openPicks(page);
    await page.getByRole('button', { name: '开始选股' }).click();
    const error = page.getByTestId('plan-error');
    await expect(error).toBeVisible({ timeout: 15_000 });
    await expect(error).toContainText('选股没有完成');
    await expect(error).toContainText('数据源暂时不可用，请稍后重试');
    await expect(error.getByRole('button', { name: '重试' })).toBeVisible();
    await expect(page.getByRole('button', { name: '开始选股' })).toBeEnabled();
  });

  test('explains when screening is switched off', async ({ page }) => {
    await mockPlanApi(page);
    await mockPicksApi(page, { status: 'disabled' });
    await page.goto('/picks');
    await expect(page.getByRole('heading', { level: 1, name: '机会选股' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { name: '选股功能未就绪' })).toBeVisible();
    await expect(page.getByText('选股功能还没有开启。')).toBeVisible();
    await expect(page.getByRole('button', { name: '开始选股' })).toBeDisabled();
  });

  test('shows the not-ready panel when the scoped board answers 503', async ({ page }) => {
    await mockPlanApi(page, { unavailable: true });
    await mockPicksApi(page);
    await openPicks(page, '?board=industry%3ABK0448');
    await expect(page.getByTestId('plan-unavailable')).toContainText('次日计划服务未就绪');
    await expect(page.getByRole('button', { name: '开始选股' })).toBeDisabled();
    // Strategy-only runs stay possible after clearing the scope.
    await page.getByRole('button', { name: '清除范围' }).click();
    await expect(page.getByRole('button', { name: '开始选股' })).toBeEnabled();
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow at ${width}px (screenshot)`, async ({ page }) => {
      await mockPlanApi(page);
      await mockPicksApi(page, { history: 'latest', watchlist: ['002281'] });
      await page.setViewportSize({ width, height });
      await openPicks(page, '');
      await expect(page.locator('li.cand')).toHaveCount(4, { timeout: 15_000 });
      await page.waitForLoadState('networkidle');
      // Settle the entrance animations before taking the visual evidence.
      await page.waitForTimeout(1200);
      const sizes = await horizontalOverflow(page);
      expect(sizes.root, 'root width').toBeLessThanOrEqual(sizes.viewport);
      expect(sizes.body, 'body width').toBeLessThanOrEqual(sizes.viewport);
      await page.screenshot({ path: `test-results/fixtures/plan-picks-${width}.png`, animations: 'disabled', fullPage: true });
    });
  }
});
