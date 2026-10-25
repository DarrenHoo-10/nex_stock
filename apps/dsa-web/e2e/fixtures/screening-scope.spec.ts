import { expect, test } from '@playwright/test';

test('board and topic selection stays attached to results and survives history restoration', async ({ page }, testInfo) => {
  const robot = { name: '机器人', source: 'custom' };
  const electronics = { name: '消费电子', source: 'custom' };
  const requests: Record<string, unknown>[] = [];
  let result: Record<string, unknown> | null = null;
  let searches = 0;
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = decodeURIComponent(url.pathname);
    if (!path.startsWith('/api/')) return route.continue();
    let json: unknown = { items: [], total: 0 };
    if (path.endsWith('/auth/status')) json = { authEnabled: false, loggedIn: true, setupState: 'no_password' };
    else if (path.endsWith('/screening/status')) json = { enabled: true, available: true };
    else if (path.endsWith('/screening/strategies')) json = { strategies: [{ id: 'volume_breakout', name: '底部箱体突破回踩', category: 'trend', description: '筛选整理后突破或回踩的候选股票。' }] };
    else if (path.endsWith('/screening/topics')) { searches++; json = { items: [] }; }
    else if (path.endsWith('/screening/hotspots')) json = { hotspots: [{ topic: robot.name, name: robot.name, heat_score: 90, limit_up_count: 8, trade_date: '20261023', stock_count: 80 }], hotspot_count: 1 };
    else if (path.endsWith('/screening/hotspots/机器人')) json = { topic: robot.name, stocks: [], route: [] };
    else if (path.endsWith('/screening/history')) json = { runs: result ? [{ run_id: 'scope-run', strategy: 'volume_breakout', market: 'cn', candidate_count: 1, scope: result.scope }] : [] };
    else if (path.endsWith('/screening/history/scope-run')) json = { run_id: 'scope-run', strategy: 'volume_breakout', market: 'cn', result };
    else if (path.endsWith('/screening/screen/tasks')) {
      const body = route.request().postDataJSON();
      requests.push(body);
      result = {
        enabled: true, run_id: 'scope-run', strategy: body.strategy, market: body.market,
        max_results: body.max_results, scope: body.scope, strategy_version: '1', created_at: '2026-10-25T12:00:00Z',
        scope_counts: { board_count: 3200, model_reviewed_count: 12, topic_count: 1 }, after_filter_count: 26,
        candidate_count: 1, ranking_mode: 'llm', topic_filter_mode: 'model', warnings: [],
        topic_evidence: [{ ...robot, provider: 'eastmoney', retrieved_at: '2026-10-25T12:00:00Z', member_count: 80, coverage: 'provider_returned' }],
        candidates: [{ code: '600001', name: '示例候选', rank: 1, board: 'main_board', matched_topics: [robot], price: 10, score: 85,
          why_selected: [{ code: 'test', text: '满足策略成交额与价格条件', source: 'snapshot', quality: 'observed' }], reason: '测试候选', raw: {} }],
      };
      json = { task_id: 'scope-task', status: 'pending', strategy: body.strategy, market: body.market, max_results: body.max_results };
    } else if (path.endsWith('/screening/screen/tasks/scope-task')) json = { task_id: 'scope-task', status: 'completed', progress: 100, result };
    await route.fulfill({ json });
  });
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto('/screening');
  await expect(page.getByRole('button', { name: '运行选股', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '沪深主板', exact: true }).click();
  await page.getByRole('button', { name: /展开热点题材/ }).click();
  await page.getByRole('button', { name: '查看题材 机器人', exact: true }).click();
  await expect(page.getByRole('button', { name: '移除题材 机器人' })).toHaveCount(0);
  await page.getByRole('button', { name: '加入筛选 机器人', exact: true }).click();
  await expect(page.getByRole('button', { name: '移除题材 机器人' })).toBeVisible();
  expect(requests).toHaveLength(0);
  await page.getByRole('button', { name: '加入筛选 机器人', exact: true }).click();
  await expect(page.getByRole('button', { name: '移除题材 机器人' })).toHaveCount(1);
  await page.getByLabel('自定义题材').fill('消费电子');
  await page.getByLabel('自定义题材').press('Enter');
  expect(searches).toBe(0);
  await page.getByLabel('题材匹配方式').selectOption('all');
  await page.getByRole('button', { name: '运行选股', exact: true }).click();
  await expect(page.getByText('示例候选', { exact: true })).toBeVisible();
  expect(requests[0].scope).toEqual({ boards: ['main_board'], topics: [robot, electronics], topic_match: 'all' });
  await expect(page.getByText('运行范围：沪深主板 · 机器人且消费电子')).toBeVisible();
  await expect(page.getByText('命中题材：机器人')).toBeVisible();
  await page.getByRole('button', { name: '创业板', exact: true }).click();
  await expect(page.getByText(/条件已修改/)).toBeVisible();
  await expect(page.getByText('运行范围：沪深主板 · 机器人且消费电子')).toBeVisible();
  await page.getByRole('button', { name: '使用这些条件重新选股' }).click();
  await expect(page.getByRole('button', { name: '创业板', exact: true })).toHaveAttribute('aria-pressed', 'false');
  expect(requests).toHaveLength(1);
  await page.getByRole('button', { name: /收起热点题材/ }).click();
  await page.screenshot({ path: testInfo.outputPath('scope-desktop.png'), fullPage: true });
  await page.reload();
  await expect(page.getByText('示例候选', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '沪深主板', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: '移除题材 消费电子' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('scope-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('custom themes never search and model failure never produces unfiltered candidates', async ({ page }) => {
  let searches = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    let json: unknown = { items: [], total: 0 };
    if (path.endsWith('/auth/status')) json = { authEnabled: false, loggedIn: true };
    else if (path.endsWith('/screening/status')) json = { enabled: true, available: true };
    else if (path.endsWith('/screening/strategies')) json = { strategies: [{ id: 'dual_low', name: '双低' }] };
    else if (path.endsWith('/screening/history')) json = { runs: [] };
    else if (path.endsWith('/screening/hotspots')) json = { hotspots: [] };
    else if (path.endsWith('/screening/topics')) { searches++; json = { items: [] }; }
    else if (path.endsWith('/screening/screen/tasks')) json = { task_id: 'failed-scope', status: 'pending' };
    else if (path.endsWith('/screening/screen/tasks/failed-scope')) json = { status: 'failed', error: '模型未能完成题材判断，请稍后重试；未返回未核验的候选股票' };
    await route.fulfill({ json });
  });
  await page.goto('/screening');
  await expect(page.getByRole('button', { name: '运行选股', exact: true })).toBeEnabled();
  await page.getByLabel('自定义题材').fill('机器人');
  await page.getByRole('button', { name: '添加题材' }).click();
  expect(searches).toBe(0);
  await page.getByRole('button', { name: '运行选股', exact: true }).click();
  await expect(page.getByText(/模型未能完成题材判断/)).toBeVisible();
  await expect(page.getByRole('heading', { name: '选股结果', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '移除题材 机器人' })).toBeVisible();
});
