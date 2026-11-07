import { expect, test, type Page } from '@playwright/test';
import { mockPlanApi } from './plan/mockPlanApi';

// Deterministic fixture spec for /boards (板块异动): no backend, every API call is mocked.

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

async function openBoards(page: Page) {
  await page.goto('/boards');
  // The first load in a worker pays the Vite on-demand compile of the lazy page chunks.
  await expect(page.getByRole('heading', { level: 1, name: '板块异动' })).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole('button', { name: /^商业航天/ })).toBeVisible({ timeout: 20_000 });
}

test.describe('plan boards page', () => {
  test('renders the market strip and the board list, and filters by type and state', async ({ page }) => {
    const api = await mockPlanApi(page);
    await openBoards(page);

    const strip = page.getByTestId('market-strip');
    await expect(strip).toContainText('中证全指');
    await expect(strip).toContainText('+0.38%');
    await expect(strip).toContainText('3,126');
    await expect(strip).toContainText('普涨保护未触发');
    await expect(page.getByText('板块历史积累 3 天，部分判定不可用')).toBeVisible();
    await expect(page.locator('header.head .sub')).toContainText('2026-11-06 收盘');

    const rows = page.locator('button.board-row');
    await expect(rows).toHaveCount(8);
    const mainline = page.getByRole('button', { name: /^商业航天/ });
    await expect(mainline).toContainText('概念');
    await expect(mainline).toContainText('持续走强');
    await expect(mainline).toContainText('主线');
    await expect(mainline).toContainText('+4.58%');
    await expect(mainline).toContainText('2.6×');
    await expect(mainline).toContainText('涨停 7');
    await expect(page.getByRole('button', { name: /^机器人执行器/ })).toContainText('单股驱动');
    await expect(page.getByRole('button', { name: /^可控核聚变/ })).toContainText('无基准');
    await expect(page.getByRole('button', { name: /^可控核聚变/ })).toContainText('冷却');

    // Type filter.
    await page.getByRole('group', { name: '板块类型' }).getByRole('button', { name: '概念' }).click();
    await expect(rows).toHaveCount(4);
    await expect(page.getByRole('button', { name: /^白酒/ })).toHaveCount(0);

    // State chips combine with the type filter.
    const strengthening = page.getByRole('group', { name: '状态' }).getByRole('button', { name: '持续走强' });
    await expect(strengthening).toHaveAttribute('aria-pressed', 'false');
    await strengthening.click();
    await expect(strengthening).toHaveAttribute('aria-pressed', 'true');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('商业航天');

    // A combination without rows shows the empty state; 「全部」 resets.
    await page.getByRole('group', { name: '板块类型' }).getByRole('button', { name: '行业' }).click();
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('半导体设备');
    await page.getByRole('group', { name: '状态' }).getByRole('button', { name: '高位分歧' }).click();
    await expect(page.getByRole('heading', { name: '没有符合条件的板块' })).toBeVisible();
    await page.getByRole('group', { name: '状态' }).getByRole('button', { name: '全部' }).click();
    await page.getByRole('group', { name: '板块类型' }).getByRole('button', { name: '全部' }).click();
    await expect(rows).toHaveCount(8);

    // Filtering happens on the client: toggling the filters never sends board_type / state queries
    // (dev StrictMode may load the list twice, hence no exact count).
    const listCalls = api.calls.filter((call) => call.path === '/api/v1/boards/anomalies');
    expect(listCalls.length).toBeGreaterThan(0);
    expect(listCalls.every((call) => Object.keys(call.query).length === 0)).toBe(true);
  });

  test('opens a board detail with the chart, representatives and the pick entry', async ({ page }) => {
    const api = await mockPlanApi(page);
    await openBoards(page);

    // The first board is shown by default.
    const first = page.getByRole('button', { name: /^通信设备/ });
    await expect(first).toHaveAttribute('aria-expanded', 'true');
    const detail = page.getByRole('article', { name: '板块详情' });
    await expect(detail.getByRole('heading', { level: 2, name: '通信设备' })).toBeVisible();
    await expect(detail.getByRole('heading', { name: '为什么判定为首次启动' })).toBeVisible();
    await expect(detail).toContainText('成交额是 5 日均值的 2.7 倍');
    for (const label of ['今日', '5 日', '超额收益', '成交额 / 5 日均', '上涨家数占比', '涨停 / 炸板率']) {
      await expect(detail.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(detail.getByText('+3.86%', { exact: true })).toBeVisible();
    await expect(detail.getByText('消息驱动')).toBeVisible();

    // Representatives with roles.
    const reps = detail.locator('.reps li');
    await expect(reps).toHaveCount(4);
    await expect(reps.nth(0)).toContainText('华工科技');
    await expect(reps.nth(0)).toContainText('龙头');
    await expect(reps.nth(1)).toContainText('成交额');
    await expect(reps.nth(2)).toContainText('跟风');

    // Excess-return chart: 8 points, a zero baseline, a 2px line and a hover tooltip.
    const chart = detail.getByTestId('excess-chart');
    await expect(chart).toBeVisible();
    await expect(chart).toHaveAttribute('aria-label', /最近 8 个交易日相对中证全指的超额收益.*11-06 \+3\.48%/);
    await expect(chart.locator('circle.excess-dot')).toHaveCount(8);
    await expect(chart.locator('.zero-line')).toHaveCount(1);
    await expect(chart.locator('polyline.excess-line')).toHaveCSS('stroke-width', '2px');
    await expect(page.getByTestId('excess-tooltip')).toHaveCount(0);
    await chart.scrollIntoViewIfNeeded();
    const box = await chart.boundingBox();
    expect(box).not.toBeNull();
    await chart.hover({ position: { x: box!.width - 4, y: box!.height / 2 } });
    const tooltip = page.getByTestId('excess-tooltip');
    await expect(tooltip).toBeVisible();
    await expect(tooltip).toContainText('2026-11-06');
    await expect(tooltip).toContainText('+3.48%');
    await chart.hover({ position: { x: 2, y: box!.height / 2 } });
    await expect(tooltip).toContainText('2026-10-28');
    await page.mouse.move(0, 0);
    await expect(tooltip).toHaveCount(0);
    // Keyboard: arrow keys move the tooltip.
    await chart.focus();
    await page.keyboard.press('ArrowLeft');
    await expect(tooltip).toContainText('2026-11-06');
    await page.keyboard.press('ArrowLeft');
    await expect(tooltip).toContainText('2026-11-05');

    // Switching rows loads that board's own detail.
    await page.getByRole('button', { name: /^商业航天/ }).click();
    await expect(page.getByRole('button', { name: /^商业航天/ })).toHaveAttribute('aria-expanded', 'true');
    await expect(first).toHaveAttribute('aria-expanded', 'false');
    await expect(detail.getByRole('heading', { level: 2, name: '商业航天' })).toBeVisible();
    expect(api.calls.some((call) => call.path === '/api/v1/boards/concept%3ABK1168' || call.path === '/api/v1/boards/concept:BK1168')).toBe(true);

    // Boards that cannot be picked hide the entry.
    await page.getByRole('button', { name: /^机器人执行器/ }).click();
    await expect(detail.getByRole('heading', { level: 2, name: '机器人执行器' })).toBeVisible();
    await expect(detail.getByRole('button', { name: '在这个板块里选股' })).toHaveCount(0);

    // 「在这个板块里选股」 goes to /picks?board=<id>.
    await first.click();
    await expect(detail.getByRole('heading', { level: 2, name: '通信设备' })).toBeVisible();
    await detail.getByRole('button', { name: '在这个板块里选股' }).click();
    await expect(page).toHaveURL(/\/picks\?board=industry%3ABK0448$/);
  });

  test('the header and detail assistant buttons open the assistant flag', async ({ page }) => {
    await mockPlanApi(page);
    await openBoards(page);
    const header = page.locator('header.head').getByRole('button', { name: '助手', exact: true });
    await expect(header).toHaveAttribute('aria-expanded', 'false');
    await page.getByRole('article', { name: '板块详情' }).getByRole('button', { name: '问助手' }).click();
    await expect(header).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByTestId('assistant-context-chip')).toHaveText('当前：板块 通信设备');
  });

  test('shows the not-ready panel when the plan service answers 503', async ({ page }) => {
    await mockPlanApi(page, { unavailable: true });
    await page.goto('/boards');
    await expect(page.getByRole('heading', { level: 1, name: '板块异动' })).toBeVisible({ timeout: 20_000 });
    const panel = page.getByTestId('plan-unavailable');
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('次日计划服务未就绪');
    await expect(page.locator('button.board-row')).toHaveCount(0);
    await expect(page.getByTestId('market-strip')).toHaveCount(0);
  });

  test('shows the not-ready panel inside the detail when only the detail answers 503', async ({ page }) => {
    await mockPlanApi(page);
    await page.route('**/api/v1/boards/industry*', async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/v1/boards/anomalies') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'daily_plan_unavailable', message: '次日计划服务未就绪' }),
      });
    });
    await openBoards(page);
    const detail = page.getByRole('article', { name: '板块详情' });
    await expect(detail.getByTestId('plan-unavailable')).toContainText('次日计划服务未就绪');
    // The list stays usable.
    await expect(page.locator('button.board-row')).toHaveCount(8);
  });

  test('shows an error state with retry, then the list', async ({ page }) => {
    await mockPlanApi(page);
    // React StrictMode runs the load effect twice in dev, so fail every request until the retry click.
    let failing = true;
    await page.route('**/api/v1/boards/anomalies*', async (route) => {
      if (failing) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'internal_error', message: '板块服务暂时出错' }),
        });
        return;
      }
      await route.fallback();
    });
    await page.goto('/boards');
    const error = page.getByTestId('plan-error');
    await expect(error).toBeVisible({ timeout: 20_000 });
    await expect(error).toContainText('板块数据加载失败');
    failing = false;
    await error.getByRole('button', { name: '重试' }).click();
    await expect(page.locator('button.board-row')).toHaveCount(8);
    await expect(error).toHaveCount(0);
  });

  test('shows an empty state when there are no boards', async ({ page }) => {
    await mockPlanApi(page);
    await page.route('**/api/v1/boards/anomalies*', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          trade_date: '2026-11-06',
          market: { index_name: '中证全指', index_change_pct: 0, up_count: null, down_count: null, limit_up_count: null, limit_down_count: null, turnover_yi: null, broad_rally: true, note: null },
          boards: [],
          history_note: null,
        }),
      });
    });
    await page.goto('/boards');
    await expect(page.getByRole('heading', { name: '今天没有板块数据' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('market-strip')).toContainText('普涨保护已触发');
    await expect(page.getByText('普涨降权')).toBeVisible();
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow at ${width}px (screenshot)`, async ({ page }) => {
      await mockPlanApi(page);
      await page.setViewportSize({ width, height });
      await openBoards(page);
      const detail = page.getByRole('article', { name: '板块详情' });
      const narrow = width <= 860;
      if (!narrow) {
        await expect(detail.getByRole('heading', { level: 2, name: '通信设备' })).toBeVisible();
        await expect(detail.getByTestId('excess-chart')).toBeVisible();
      }
      await page.waitForLoadState('networkidle');
      // Settle the entrance animations before taking the visual evidence.
      await page.waitForTimeout(1200);
      let sizes = await horizontalOverflow(page);
      expect(sizes.root, 'root width').toBeLessThanOrEqual(sizes.viewport);
      expect(sizes.body, 'body width').toBeLessThanOrEqual(sizes.viewport);
      await page.screenshot({ path: `test-results/fixtures/plan-boards-${width}.png`, animations: 'disabled', fullPage: true });

      if (narrow) {
        // Narrow layout: the row opens the detail window in place of the list, 「返回列表」 comes back.
        await page.getByRole('button', { name: /^通信设备/ }).click();
        await expect(detail.getByRole('heading', { level: 2, name: '通信设备' })).toBeVisible();
        await expect(detail.getByTestId('excess-chart')).toBeVisible();
        await expect(page.getByRole('region', { name: '板块列表' })).toBeHidden();
        await page.waitForTimeout(1200);
        sizes = await horizontalOverflow(page);
        expect(sizes.root, 'detail root width').toBeLessThanOrEqual(sizes.viewport);
        expect(sizes.body, 'detail body width').toBeLessThanOrEqual(sizes.viewport);
        await page.screenshot({ path: `test-results/fixtures/plan-boards-${width}-detail.png`, animations: 'disabled', fullPage: true });
        await detail.getByRole('button', { name: '返回列表' }).click();
        await expect(page.getByRole('region', { name: '板块列表' })).toBeVisible();
      }
    });
  }
});
