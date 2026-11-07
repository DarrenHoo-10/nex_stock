import { expect, test, type Page } from '@playwright/test';
import { mockPlanApi } from './plan/mockPlanApi';

// Deterministic fixture spec for the plan shell: no backend, every API call is mocked.

const WIDTHS: Array<[number, number]> = [
  [1366, 900],
  [1024, 768],
  [375, 812],
  [320, 640],
];

const PAGES: Array<{ path: string; ornament: string; title: string }> = [
  { path: '/boards', ornament: '板块异动', title: '板块异动' },
  { path: '/picks', ornament: '机会选股', title: '机会选股' },
  { path: '/mine', ornament: '我的股票', title: '我的股票' },
  { path: '/preferences', ornament: '设置', title: '设置' },
];

const OLD_PAGES: Array<[string, string]> = [
  ['首页', '/home'],
  ['问股', '/chat'],
  ['持仓台账', '/portfolio'],
  ['信号', '/decision-signals'],
  ['选股工具', '/screening'],
  ['回测', '/backtest'],
  ['提醒', '/alerts'],
  ['数据中心', '/data'],
  ['用量', '/usage'],
  ['高级设置', '/settings'],
];

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
}

test.describe('plan shell', () => {
  test('/ redirects to /mine and the capsule shows the latest run', async ({ page }) => {
    await mockPlanApi(page);
    await page.goto('/');
    // The first load in a worker pays the Vite on-demand compile of the lazy page chunks.
    await expect(page).toHaveURL(/\/mine$/, { timeout: 20_000 });
    await expect(page.getByRole('heading', { level: 1, name: '我的股票' })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('link', { name: '我的股票' })).toHaveAttribute('aria-current', 'page');
    const capsule = page.getByTestId('plan-run-capsule');
    await expect(capsule).toContainText('11-06');
    await expect(capsule).toContainText('部分股票未完成');
    await expect(page.getByRole('button', { name: '助手', exact: true })).toBeVisible();
    await expect(page.locator('main#main')).toHaveAttribute('tabindex', '-1');
  });

  test('ornament nav switches pages and moves aria-current', async ({ page }) => {
    await mockPlanApi(page);
    await page.goto('/mine');
    for (const item of PAGES) {
      await page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: item.ornament, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`${item.path}$`));
      await expect(page.getByRole('heading', { level: 1, name: item.title })).toBeVisible();
      await expect(page.getByRole('link', { name: item.ornament, exact: true })).toHaveAttribute('aria-current', 'page');
      await expect(page.locator('[aria-current="page"]')).toHaveCount(1);
    }
  });

  test('the more popover lists the old pages and closes with Escape', async ({ page }) => {
    await mockPlanApi(page);
    await page.goto('/mine');
    const more = page.getByRole('button', { name: '更多', exact: true });
    await more.click();
    const popover = page.getByRole('navigation', { name: '更多页面' });
    await expect(popover).toBeVisible();
    for (const [name, href] of OLD_PAGES) {
      await expect(popover.getByRole('link', { name, exact: true })).toHaveAttribute('href', href);
    }
    await page.keyboard.press('Escape');
    await expect(popover).toBeHidden();
    await expect(more).toBeFocused();
  });

  test('the capsule handles no run and the unavailable service', async ({ page }) => {
    await mockPlanApi(page, { latestRun: 'none' });
    await page.goto('/boards');
    await expect(page.getByTestId('plan-run-capsule')).toContainText('还没有运行记录');

    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await mockPlanApi(page, { unavailable: true });
    await page.goto('/boards');
    await expect(page.getByTestId('plan-run-capsule')).toContainText('次日计划服务未就绪');
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow at ${width}px`, async ({ page }) => {
      await mockPlanApi(page);
      await page.setViewportSize({ width, height });

      for (const item of PAGES) {
        await page.goto(item.path);
        await expect(page.getByRole('heading', { level: 1, name: item.title })).toBeVisible();
        await page.waitForLoadState('networkidle');
        const sizes = await horizontalOverflow(page);
        expect(sizes.root, `${item.path} root width`).toBeLessThanOrEqual(sizes.viewport);
        expect(sizes.body, `${item.path} body width`).toBeLessThanOrEqual(sizes.viewport);
      }

      await page.goto('/mine');
      await expect(page.getByTestId('plan-run-capsule')).toContainText('11-06');
      // Settle the entrance animations before taking the visual evidence.
      await page.waitForTimeout(900);
      await page.screenshot({ path: `test-results/fixtures/plan-shell-${width}.png`, animations: 'disabled' });

      await page.getByRole('button', { name: '更多', exact: true }).click();
      const popover = page.getByRole('navigation', { name: '更多页面' });
      await expect(popover).toBeVisible();
      await page.waitForTimeout(600);
      const box = await popover.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      const sizes = await horizontalOverflow(page);
      expect(sizes.root).toBeLessThanOrEqual(sizes.viewport);
      await page.screenshot({ path: `test-results/fixtures/plan-shell-${width}-more.png`, animations: 'disabled' });
    });
  }
});
