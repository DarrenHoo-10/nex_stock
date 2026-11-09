import { expect, test } from '@playwright/test';

test('opens the latest daily reports and switches to an earlier market-only day', async ({ page }, testInfo) => {
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith('/api/')) return route.continue();
    if (path.endsWith('/auth/status')) {
      return route.fulfill({ json: { authEnabled: false, loggedIn: true } });
    }
    if (path.endsWith('/dates')) return route.fulfill({ json: ['2026-11-06', '2026-11-05'] });
    if (path.endsWith('/2026-11-06')) return route.fulfill({ json: {
      daily_plan: '## 今日结论（持仓）\n\n示例组合维持观察，等待下一交易日确认。',
      market_review: '## 指数表现\n\n| 指数 | 收盘涨跌 |\n| --- | ---: |\n| 示例指数 A | +0.48% |\n| 示例指数 B | −0.21% |',
      stock_summary: '## 自选股观察\n\n今日样例组合中，两只个股上涨，一只回落。',
    } });
    if (path.endsWith('/2026-11-05')) return route.fulfill({ json: {
      market_review: '前一交易日报告', stock_summary: null,
    } });
    return route.fulfill({ json: { items: [], total: 0 } });
  });

  await page.goto('/');
  await expect(page).toHaveURL(/\/mine$/);
  const nav = page.locator('.plan-shell nav');
  await nav.getByRole('link', { name: '当日复盘' }).click();
  await expect(page.getByLabel('报告日期')).toHaveValue('2026-11-06');
  await expect(page.getByRole('heading', { name: '自选股观察' })).toBeVisible();
  await expect(page.getByRole('table')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('closing-analysis.png'), animations: 'disabled' });

  await page.setViewportSize({ width: 375, height: 812 });
  await expect(nav.getByRole('link', { name: '当日复盘' })).toHaveAttribute('aria-current', 'page');
  await expect(nav.getByRole('link', { name: '设置' })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('closing-mobile.png'), animations: 'disabled' });
  await page.getByLabel('报告日期').selectOption('2026-11-05');
  await expect(page.getByText('前一交易日报告')).toBeVisible();
  await expect(page.getByRole('heading', { name: '个股汇总' })).toHaveCount(0);
});
