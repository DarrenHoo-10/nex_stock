import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';
import { mockPlanApi } from './plan/mockPlanApi';
import type { PlanApiMock } from './plan/mockPlanApi';

// Deterministic fixture spec for the 我的股票 page: no backend, every API call is mocked.

const FIXTURE_DIR = dirname(fileURLToPath(import.meta.url));

const WIDTHS: Array<[number, number]> = [
  [1366, 900],
  [1024, 768],
  [375, 812],
  [320, 640],
];

function mineFixture(name: string): string {
  return readFileSync(join(FIXTURE_DIR, 'plan', 'mine', name), 'utf8');
}

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
}

async function expectNoOverflow(page: Page, label: string) {
  const sizes = await horizontalOverflow(page);
  expect(sizes.root, `${label} root width`).toBeLessThanOrEqual(sizes.viewport);
  expect(sizes.body, `${label} body width`).toBeLessThanOrEqual(sizes.viewport);
}

function calls(mock: PlanApiMock, method: string, path: string) {
  return mock.calls.filter((call) => call.method === method && call.path === path);
}

async function openMine(page: Page, scenario: 'default' | 'empty-account' = 'default', path = '/mine') {
  const mock = await mockPlanApi(page, { scenario });
  await page.goto(path);
  await expect(page.getByRole('heading', { level: 1, name: '我的股票' })).toBeVisible({ timeout: 20_000 });
  return mock;
}

test.describe('我的股票 page', () => {
  test('renders the account strip and the list with holdings first', async ({ page }) => {
    await openMine(page);
    const account = page.locator('.acct');
    await expect(account).toContainText('总资金');
    await expect(account.locator('[data-count="100184"]')).toBeVisible();
    await expect(account).toContainText('可用现金');
    await expect(account.locator('[data-count="1184"]')).toBeVisible();
    await expect(account).toContainText('持仓市值');
    await expect(account.locator('[data-count="99000"]')).toBeVisible();
    await expect(account.locator('[data-count="98.82"]')).toBeVisible();
    await expect(account.locator('.rules-line')).toContainText('止损 10%');
    await expect(account.locator('.rules-line')).toContainText('最多 1 只');
    await expect(account.locator('.rules-line')).toContainText('单只上限 100%');

    const held = page.getByRole('list', { name: '持仓' });
    await expect(held.locator('.row')).toHaveCount(1);
    await expect(held.locator('.row')).toContainText('立讯精密');
    await expect(held.locator('.row .tag')).toHaveText('持有');
    const watch = page.getByRole('list', { name: '自选' });
    await expect(watch.locator('.row').first()).toContainText('华工科技');
    await expect(watch.locator('.row').first().locator('.tag')).toHaveText('买入');
    await expect(watch.locator('.row[data-code="603019"] .tag')).toHaveText('停牌');
    await expect(watch.locator('.row[data-code="688072"] .tag')).toHaveText('不买');
    await expect(watch.locator('.row[data-code="002463"] .tag')).toHaveText('等待');

    // The held stock is selected by default; exactly one row is current.
    await expect(page.locator('.row[aria-current="true"]')).toHaveCount(1);
    await expect(page.locator('.row[data-code="002475"]')).toHaveAttribute('aria-current', 'true');
    await expect(page.locator('main [title]')).toHaveCount(0);
  });

  test('empty account: 000988 sizes to 2,100 shares with the stop and the max loss', async ({ page }) => {
    await openMine(page, 'empty-account');
    await page.locator('.row[data-code="000988"]').click();
    await expect(page).toHaveURL(/\/mine\?code=000988$/);
    const detail = page.locator('.detail');
    await expect(detail.getByRole('heading', { level: 2, name: '华工科技' })).toBeVisible();
    await expect(detail.getByTestId('mine-verdict')).toHaveText('买入');
    const sizing = detail.getByRole('group', { name: '买入股数' });
    await expect(sizing.locator('[data-count="2100"]')).toBeVisible();
    await expect(sizing).toContainText('股，在 47.00 买入');
    await expect(sizing).toContainText('98,700 元');
    await expect(sizing).toContainText('98.7%');
    await expect(sizing).toContainText('42.30');
    await expect(sizing).toContainText('9,870 元');
    await expect(sizing).toContainText('跌停');
    await expect(sizing).toContainText('18,753 元');
    await expect(detail.locator('.figs')).toContainText('42.30');
  });

  test('怎么算的 shows the backend steps and the three caps', async ({ page }) => {
    await openMine(page, 'empty-account', '/mine?code=000988');
    const calc = page.locator('.detail .calc');
    await expect(calc).toBeVisible();
    await expect(calc.locator('ol')).toBeHidden();
    await calc.getByText('怎么算的').click();
    await expect(calc.locator('ol li')).toHaveCount(5);
    await expect(calc.locator('ol li').nth(3)).toContainText('取最小值 2,127，按每手 100 股向下取整 = 2,100 股');
    await expect(calc.locator('ol li').nth(4)).toContainText('止损价 = 47.00 × 0.90 = 42.30');
    await expect(calc.locator('.caps')).toContainText('单只上限');
    await expect(calc.locator('.caps')).toContainText('2,127 股');
  });

  test('horizon switch changes the conditional plan', async ({ page }) => {
    await openMine(page, 'empty-account', '/mine?code=000988');
    const detail = page.locator('.detail');
    const group = detail.getByRole('group', { name: '周期' });
    await expect(group.getByRole('button', { name: /短线/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(detail.locator('.ifthen')).toContainText('47.0 附近买入');
    await expect(detail.locator('.ifthen')).toContainText('不追');
    await expect(detail.locator('.summary')).toContainText('回踩 46.8–47.2 企稳就买');

    await group.getByRole('button', { name: /波段/ }).click();
    await expect(group.getByRole('button', { name: /波段/ })).toHaveAttribute('aria-pressed', 'true');
    await expect(group.getByRole('button', { name: /短线/ })).toHaveAttribute('aria-pressed', 'false');
    await expect(detail.locator('.ifthen')).toContainText('卖出一半');
    await expect(detail.locator('.ifthen')).toContainText('下一交易日全部卖出');
    await expect(detail.locator('.summary')).toContainText('计划持有 2–4 周');
    // The thumb exists exactly once (shared layout).
    await expect(group.locator('.thumb')).toHaveCount(1);
  });

  test('max-holdings sizing explains why there is no share count', async ({ page }) => {
    await openMine(page, 'default', '/mine?code=000988');
    const detail = page.locator('.detail');
    await expect(detail.getByTestId('mine-verdict')).toHaveText('买入');
    await expect(detail.locator('.notice').filter({ hasText: '不新开仓' })).toContainText('已持有 1/1 只（立讯精密）');
    await expect(detail.getByRole('group', { name: '买入股数' })).toHaveCount(0);
    await detail.getByRole('button', { name: '看换股判断' }).click();
    await expect(page).toHaveURL(/code=002475/);
    await expect(page.locator('.detail #switch')).toBeVisible();
  });

  test('held 002475 detail shows the hold plan, the switch card and the notes', async ({ page }) => {
    await openMine(page);
    const detail = page.locator('.detail');
    await expect(detail.getByRole('heading', { level: 2, name: '立讯精密' })).toBeVisible();
    await expect(detail.getByTestId('mine-verdict')).toHaveText('持有');
    const figs = detail.locator('.figs');
    await expect(figs).toContainText('持仓');
    await expect(figs.locator('[data-count="2400"]')).toBeVisible();
    await expect(figs.locator('[data-count="2520"]')).toBeVisible();
    await expect(figs.locator('[data-count="36.18"]')).toBeVisible();
    await expect(figs.locator('[data-count="37.13"]')).toBeVisible();
    // default horizon of this holding is swing
    await expect(detail.getByRole('group', { name: '周期' }).getByRole('button', { name: /波段/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await detail.getByRole('group', { name: '周期' }).getByRole('button', { name: /短线/ }).click();
    await expect(detail.locator('.ifthen')).toContainText('卖出 1/3（800 股）');
    await expect(detail.locator('.ifthen')).toContainText('短线减半（1,200 股）');

    const card = detail.locator('#switch');
    await expect(card).toContainText('换股判断：不换');
    await expect(card).toContainText('华工科技');
    await expect(card.locator('.cond li')).toHaveCount(2);
    await expect(card.locator('.cond li').first()).toContainText('未满足');
    await expect(card).toContainText('T+1');

    await expect(detail.locator('.notice')).toContainText('主力资金流 16:00 时尚未发布');
    await expect(detail.locator('.mine-foot')).toContainText('deepseek/deepseek-v4-flash');
    await expect(detail.locator('.mine-foot')).not.toContainText('已回退');
    await expect(detail.getByRole('button', { name: '记录买入' })).toHaveCount(0);
    await expect(detail.getByRole('button', { name: '移出自选' })).toHaveCount(0);
  });

  test('the price ladder is an image with a text list of the levels', async ({ page }) => {
    await openMine(page);
    const ladder = page.locator('.detail .ladder');
    await expect(ladder.getByRole('img', { name: '价格梯子' })).toBeVisible();
    const items = ladder.locator('ul.vh li');
    await expect(items).toContainText([
      '成本 +15% 卖出一半 46.23',
      '前高压力 43.10',
      '11-06 收盘 41.25',
      '持仓成本 40.20',
      '技术支撑（仅提示） 39.60',
      '明天跌停价 37.13',
      '止损，跌破次日清仓 36.18',
    ]);
    await expect(ladder.locator('.mk-limit-down')).toHaveCount(1);
    await expect(ladder.locator('.mk-close')).toHaveCount(1);
    await expect(ladder.locator('.mk-stop')).toHaveCount(1);

    // A buy plan draws the entry zone as a band and adds the limit-down line.
    await page.locator('.row[data-code="000988"]').click();
    await expect(ladder.locator('.mk-band')).toHaveCount(1);
    await expect(ladder.locator('ul.vh li', { hasText: '明天跌停价 43.52' })).toHaveCount(1);
    await expect(ladder.locator('ul.vh li', { hasText: '买入区 46.80–47.20' })).toHaveCount(1);
  });

  test('model footnote names the fallback', async ({ page }) => {
    await openMine(page, 'default', '/mine?code=000988');
    const foot = page.locator('.detail .mine-foot');
    await expect(foot).toContainText('gemini/gemini-3-flash-preview');
    await expect(foot).toContainText('已回退：原定 deepseek/deepseek-v4-flash');
    await expect(foot).toContainText('等待 60 秒超时');
  });

  test('holdings dialog validates inline and saves with the full-overwrite PUT body', async ({ page }) => {
    const mock = await openMine(page);
    await page.getByRole('button', { name: '持仓与资金' }).click();
    const dialog = page.getByRole('dialog', { name: '持仓与资金' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel('可用现金（元）')).toHaveValue('1184');
    const row = dialog.locator('.holding-edit').first();
    await expect(row.getByLabel(/股票代码/)).toHaveValue('002475');
    await expect(row.getByLabel('数量（股）')).toHaveValue('2400');
    await expect(row.getByLabel('成本价')).toHaveValue('40.2');
    await expect(row.getByLabel('买入日（选填）')).toHaveValue('2026-10-28');
    await expect(row.getByLabel('计划周期（选填）')).toHaveValue('swing');

    // client validation: nothing is sent
    await row.getByLabel('数量（股）').fill('0');
    await dialog.getByRole('button', { name: '保存' }).click();
    await expect(row.getByLabel('数量（股）')).toHaveAttribute('aria-invalid', 'true');
    await expect(dialog).toContainText('第 1 条持仓：数量要填大于 0 的整数');
    expect(calls(mock, 'PUT', '/api/v1/plan/holdings')).toHaveLength(0);
    await row.getByLabel('股票代码（立讯精密）').fill('12');
    await dialog.getByRole('button', { name: '保存' }).click();
    await expect(dialog).toContainText('股票代码要填 6 位数字');

    // fix and save
    await row.getByLabel(/股票代码/).fill('002475');
    await row.getByLabel('数量（股）').fill('2,600');
    await row.getByLabel('数量（股）').fill('2600');
    await dialog.getByLabel('可用现金（元）').fill('2,000.5');
    await dialog.getByRole('button', { name: '+ 添加持仓' }).click();
    const added = dialog.locator('.holding-edit').nth(1);
    await added.getByLabel(/股票代码/).fill('000988');
    await added.getByLabel('数量（股）').fill('100');
    await added.getByLabel('成本价').fill('47');
    await dialog.getByRole('button', { name: '保存' }).click();

    await expect(page.getByText('已保存持仓')).toBeVisible();
    await expect(dialog).toBeHidden();
    const puts = calls(mock, 'PUT', '/api/v1/plan/holdings');
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toEqual({
      cash: 2000.5,
      holdings: [
        { code: '002475', shares: 2600, cost: 40.2, buy_date: '2026-10-28', horizon: 'swing' },
        { code: '000988', shares: 100, cost: 47, buy_date: null, horizon: null },
      ],
    });
    // the page refetches the account and the plan list after saving
    await expect.poll(() => calls(mock, 'GET', '/api/v1/plan/account').length).toBeGreaterThanOrEqual(2);
    await expect.poll(() => calls(mock, 'GET', '/api/v1/plan/stocks').length).toBeGreaterThanOrEqual(2);
  });

  test('a 400 from the server is shown inline and the dialog stays open', async ({ page }) => {
    await openMine(page);
    let putCount = 0;
    await page.route('**/api/v1/plan/holdings', async (route) => {
      if (route.request().method() !== 'PUT') {
        await route.fallback();
        return;
      }
      putCount += 1;
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'daily_plan_validation', message: '当前规则最多持有 1 只，请先调整规则。' }),
      });
    });
    await page.getByRole('button', { name: '持仓与资金' }).click();
    const dialog = page.getByRole('dialog', { name: '持仓与资金' });
    await dialog.getByRole('button', { name: '保存' }).click();
    await expect(dialog.getByRole('alert')).toContainText('当前规则最多持有 1 只，请先调整规则。');
    await expect(dialog).toBeVisible();
    expect(putCount).toBe(1);
  });

  test('记录买入 prefills from the sizing and saves the new holding with the cash taken out', async ({ page }) => {
    const mock = await openMine(page, 'empty-account', '/mine?code=000988');
    await page.locator('.detail').getByRole('button', { name: '记录买入' }).click();
    const dialog = page.getByRole('dialog', { name: '记录买入：华工科技' });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel('买入数量（股）')).toHaveValue('2100');
    await expect(dialog.getByLabel('买入价')).toHaveValue('47.00');
    await expect(dialog).toContainText('买入金额 98,700.00 元');
    await dialog.getByLabel('买入日期').fill('2026-11-09');
    await dialog.getByRole('button', { name: '确认买入' }).click();

    await expect(page.getByText('已记录买入，现金扣减 98,700.00 元')).toBeVisible();
    await expect(dialog).toBeHidden();
    // the account is read again before writing
    expect(calls(mock, 'GET', '/api/v1/plan/account').length).toBeGreaterThanOrEqual(2);
    const puts = calls(mock, 'PUT', '/api/v1/plan/holdings');
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toEqual({
      cash: 1300,
      holdings: [{ code: '000988', shares: 2100, cost: 47, buy_date: '2026-11-09', horizon: 'short' }],
    });
  });

  test('记录买入 keeps the existing holdings and rejects an amount above the cash', async ({ page }) => {
    const mock = await openMine(page, 'default', '/mine?code=000988');
    await page.locator('.detail').getByRole('button', { name: '记录买入' }).click();
    const dialog = page.getByRole('dialog', { name: '记录买入：华工科技' });
    // max_holdings sizing has no share count to prefill; the entry price is still filled
    await expect(dialog.getByLabel('买入数量（股）')).toHaveValue('');
    await expect(dialog.getByLabel('买入价')).toHaveValue('47.00');
    await dialog.getByRole('button', { name: '确认买入' }).click();
    await expect(dialog).toContainText('数量要填大于 0 的整数');

    await dialog.getByLabel('买入数量（股）').fill('100');
    await dialog.getByLabel('买入日期').fill('2026-11-09');
    await dialog.getByRole('button', { name: '确认买入' }).click();
    await expect(dialog.getByRole('alert')).toContainText('这笔买入需要 4,700.00 元，可用现金只有 1,184.00 元');
    expect(calls(mock, 'PUT', '/api/v1/plan/holdings')).toHaveLength(0);

    await dialog.getByLabel('买入数量（股）').fill('20');
    await dialog.getByLabel('计划周期').selectOption('swing');
    await dialog.getByRole('button', { name: '确认买入' }).click();
    await expect(dialog).toBeHidden();
    const puts = calls(mock, 'PUT', '/api/v1/plan/holdings');
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toEqual({
      cash: 244,
      holdings: [
        { code: '002475', shares: 2400, cost: 40.2, buy_date: '2026-10-28', horizon: 'swing' },
        { code: '000988', shares: 20, cost: 47, buy_date: '2026-11-09', horizon: 'swing' },
      ],
    });
  });

  test('failed, missing and suspended plans have their own states', async ({ page }) => {
    await mockPlanApi(page);
    await page.route('**/api/v1/plan/stocks', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: mineFixture('stocks-states.json') });
    });
    await page.goto('/mine');
    const failed = page.locator('.row[data-code="300750"]');
    await expect(failed.locator('.tag')).toHaveText('分析失败');
    await expect(failed).toContainText('模型两次超时');
    const missing = page.locator('.row[data-code="600519"]');
    await expect(missing.locator('.tag')).toHaveText('暂无计划');

    await failed.click();
    const detail = page.locator('.detail');
    await expect(detail.locator('.verdict')).toHaveText('分析失败');
    await expect(detail.locator('.summary')).toContainText('模型两次超时，当日分析没有完成');
    await expect(detail.getByRole('button', { name: '移出自选' })).toBeVisible();

    await missing.click();
    await expect(detail.locator('.verdict')).toHaveText('暂无计划');
    await expect(detail.locator('.summary')).toContainText('还没有次日计划');

    await page.locator('.row[data-code="603019"]').click();
    await expect(detail.locator('.verdict')).toHaveText('停牌');
    await expect(detail.locator('.summary')).toContainText('停牌');
    await expect(detail).toContainText('复牌前不给计划');
  });

  test('add and remove watchlist stocks through the watchlist API', async ({ page }) => {
    await mockPlanApi(page);
    const seen: Array<{ path: string; body: unknown }> = [];
    await page.route('**/api/v1/stocks/watchlist/**', async (route) => {
      seen.push({ path: new URL(route.request().url()).pathname, body: route.request().postDataJSON() });
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ stock_codes: ['000988'] }) });
    });
    await page.goto('/mine');
    await page.getByRole('button', { name: '+ 加自选' }).click();
    await page.getByLabel('股票代码', { exact: true }).fill('12ab');
    await expect(page.getByLabel('股票代码', { exact: true })).toHaveValue('12');
    await page.getByRole('button', { name: '添加', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: '请输入 6 位股票代码' })).toBeVisible();
    await page.getByLabel('股票代码', { exact: true }).fill('600519');
    await page.getByRole('button', { name: '添加', exact: true }).click();
    await expect(page.getByText('已加入自选')).toBeVisible();
    expect(seen).toEqual([{ path: '/api/v1/stocks/watchlist/add', body: { stock_code: '600519' } }]);

    const watch = page.getByRole('list', { name: '自选' });
    await page.locator('.list-scroll').evaluate(el => { el.scrollTop = 0; });
    await page.locator('.win.side').screenshot({ path: test.info().outputPath('watchlist.png'), animations: 'disabled' });
    await watch.getByRole('button', { name: '将 华工科技（000988）移出自选' }).click();
    await expect(page.locator('.row[data-code="002475"]')).toHaveAttribute('aria-current', 'true');
    await expect(page.getByText('已移出自选')).toBeVisible();
    expect(seen[1]).toEqual({ path: '/api/v1/stocks/watchlist/remove', body: { stock_code: '000988' } });
  });

  test('removing a watched holding keeps the holding and other watch entries', async ({ page }) => {
    await mockPlanApi(page);
    let stockCodes = ['000988', '002475', 'AAPL'];
    await page.route('**/api/v1/stocks/watchlist', route => route.fulfill({ json: { stock_codes: stockCodes } }));
    await page.route('**/api/v1/stocks/watchlist/remove', route => {
      stockCodes = stockCodes.filter(code => code !== route.request().postDataJSON().stock_code);
      return route.fulfill({ json: { stock_codes: stockCodes } });
    });
    await page.goto('/mine');
    const extra = page.getByRole('list', { name: '自选列表' });
    await extra.getByRole('listitem').filter({ hasText: '002475' }).getByRole('button').click();
    await expect(extra).not.toContainText('002475');
    await expect(extra).toContainText('AAPL');
    await expect(page.getByRole('list', { name: '持仓' })).toContainText('002475');
  });

  test('daily candle opens the minute trend for the selected session', async ({ page }, testInfo) => {
    await mockPlanApi(page);
    await page.route('**/api/v1/stocks/*/history?*', (route) => route.fulfill({ json: {
      data: ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05'].map(date => ({
        date, open: 41, high: 42, low: 40, close: 41.2, volume: 10000,
      })),
    } }));
    await page.route('**/api/v1/stocks/*/intraday?*', (route) => {
      const params = new URL(route.request().url()).searchParams;
      expect(params.get('interval')).toBe('1min');
      const date = params.get('date');
      const data = Array.from({ length: 240 }, (_, i) => {
        const minute = (i < 120 ? 571 : 661) + i;
        const time = `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
        const close = 41.2 + Math.sin(i / 11) * 0.18 + i / 800;
        const volume = 1000 + (i * 67) % 2300;
        return { time: `${date}T${time}:00+08:00`, close, volume, amount: close * volume };
      });
      return route.fulfill({ json: { data, interval: '1min', is_stale: false } });
    });
    await page.route('**/api/v1/stocks/*/opening-auction?*', route => route.fulfill({ json: {
      source: 'biying', points: [{ time: '09:15:00', price: 41.4 }, { time: '09:25:00', price: 41.2, volume: 1000 }],
    } }));
    await page.setViewportSize({ width: 1840, height: 900 });
    await page.goto('/mine?code=002475');
    await page.getByRole('img', { name: /K 线图/ }).dblclick();
    const panel = page.getByRole('group', { name: /分时走势/ });
    await expect(panel.locator('.qi-readout')).toContainText('15:00');
    const chart = panel.locator('.qi-svg');
    await expect(chart.locator('.qi-minute-volume')).toHaveCount(240);
    await chart.scrollIntoViewIfNeeded();
    const auction = chart.locator('g[aria-label="开盘集合竞价"]');
    await expect(auction.locator('rect')).toHaveCount(1);
    const match = (await auction.locator('circle').last().boundingBox())!;
    const band = (await chart.locator('.qi-auction-band').boundingBox())!;
    const firstBar = (await chart.locator('.qi-minute-volume').first().boundingBox())!;
    const baseline = (await chart.locator('line[stroke-dasharray="5 4"]').boundingBox())!;
    expect(Math.abs(match.y + match.height / 2 - baseline.y)).toBeLessThan(1);
    expect(match.x).toBeGreaterThan(band.x);
    expect(Math.abs(match.x + match.width / 2 - (band.x + band.width))).toBeLessThan(2);
    await expect(chart.getByText('11:30/13:00')).toBeVisible();
    expect(band.x + band.width).toBeLessThan(firstBar.x);
    expect(Math.abs(band.y + band.height - firstBar.y - firstBar.height)).toBeLessThan(1);
    const matchX = Math.round(match.x + match.width / 2);
    await page.mouse.move(matchX, Math.round(match.y + match.height / 2));
    await expect(panel.locator('.qi-readout')).toContainText('09:25:00');
    await expect(panel.locator('.qi-readout')).toContainText('撮合价');
    expect(Math.abs((await chart.locator('g[pointer-events="none"] line').boundingBox())!.x - matchX)).toBeLessThan(1);
    // Use rendered volume bars as targets so the assertions also cover SVG letterboxing.
    for (const [index, time] of [[9, '09:40'], [119, '11:30'], [120, '13:01'], [200, '14:21']] as const) {
      const bar = (await chart.locator('.qi-minute-volume').nth(index).boundingBox())!;
      const x = Math.round(bar.x + bar.width / 2);
      await page.mouse.move(x, Math.round(bar.y + bar.height / 2));
      await expect(panel.locator('.qi-readout')).toContainText(time);
      const guide = await chart.locator('g[pointer-events="none"] line').boundingBox();
      expect(Math.abs(guide!.x - x)).toBeLessThan(1);
    }
    await panel.screenshot({ path: testInfo.outputPath('intraday.png'), animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(chart).toBeVisible();
    await expect.poll(() => chart.evaluate(el => el.getBoundingClientRect().width)).toBeLessThan(390);
    await panel.screenshot({ path: testInfo.outputPath('intraday-mobile.png'), animations: 'disabled' });
  });

  test('问助手 opens the assistant on the selected stock', async ({ page }) => {
    await openMine(page);
    const assistant = page.getByRole('button', { name: '助手', exact: true });
    await expect(assistant).toHaveAttribute('aria-expanded', 'false');
    await page.locator('.detail').getByRole('button', { name: '问助手' }).first().click();
    await expect(assistant).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByTestId('assistant-context-chip')).toContainText('立讯精密');
  });

  test('503 shows the service-not-ready panel with retry', async ({ page }) => {
    await mockPlanApi(page, { unavailable: true });
    await page.goto('/mine');
    const panel = page.getByRole('alert').filter({ hasText: '次日计划服务未就绪' }).first();
    await expect(panel).toBeVisible();
    await expect(page.getByRole('button', { name: '重试' })).toBeVisible();
    await expect(page.getByRole('heading', { level: 1, name: '我的股票' })).toBeVisible();
  });

  test('a user center outage shows a retry that recovers', async ({ page }, testInfo) => {
    await mockPlanApi(page);
    let fail = true;
    await page.route('**/api/v1/plan/stocks', async (route) => {
      if (fail) {
        await route.fulfill({ status: 503, json: { error: 'USER_CENTER_UNAVAILABLE', message: 'User center is unreachable' } });
        return;
      }
      await route.fallback();
    });
    await page.goto('/mine');
    await expect(page.getByRole('heading', { level: 2, name: '计划读取失败' })).toBeVisible();
    await expect(page.getByText('用户中心暂时无法验证你的登录状态，请稍后重试。')).toBeVisible();
    await page.getByRole('alert').filter({ hasText: '计划读取失败' }).screenshot({ path: testInfo.outputPath('session-unavailable.png') });
    fail = false;
    await page.getByRole('button', { name: '重试' }).click();
    await expect(page.locator('.row[data-code="002475"]')).toBeVisible();
  });

  test('empty account and watchlist guide the user to add stocks', async ({ page }) => {
    await mockPlanApi(page, { scenario: 'empty-account' });
    await page.route('**/api/v1/plan/stocks', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ trade_date: '2026-11-06', next_trade_date: '2026-11-09', items: [] }),
      });
    });
    await page.goto('/mine');
    const detail = page.locator('.detail');
    await expect(detail.getByRole('heading', { level: 2, name: '还没有自选和持仓' })).toBeVisible();
    await expect(detail.getByRole('link', { name: '去选股' })).toHaveAttribute('href', '/picks');
    await detail.getByRole('button', { name: '录入持仓' }).click();
    await expect(page.getByRole('dialog', { name: '持仓与资金' })).toBeVisible();
  });

  test('deep link selects the stock and survives the code not being in the list', async ({ page }) => {
    await openMine(page, 'default', '/mine?code=300750');
    await expect(page.locator('.row[data-code="300750"]')).toHaveAttribute('aria-current', 'true');
    await expect(page.locator('.detail')).toContainText('宁德时代');
    await page.goto('/mine?code=999999');
    await expect(page.getByRole('heading', { name: '999999 不在自选和持仓里' })).toBeVisible();
    await expect(page.getByRole('button', { name: '加入自选' })).toBeVisible();
  });

  test('mobile: the list opens a pushed detail view with a back button', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await openMine(page);
    await expect(page.locator('.side')).toBeVisible();
    await expect(page.locator('.detail')).toBeHidden();
    await page.locator('.row[data-code="000988"]').click();
    await expect(page).toHaveURL(/\/mine\?code=000988$/);
    await expect(page.locator('.detail')).toBeVisible();
    await expect(page.locator('.side')).toBeHidden();
    const back = page.getByRole('button', { name: '‹ 返回列表' });
    await expect(back).toBeVisible();
    await expect(back).toBeFocused();
    await expectNoOverflow(page, 'mobile detail');
    await back.click();
    await expect(page).toHaveURL(/\/mine$/);
    await expect(page.locator('.side')).toBeVisible();
    await expect(page.locator('.detail')).toBeHidden();
    await expect(page.locator('.row[data-code="000988"]')).toBeFocused();

    // browser back also returns to the list
    await page.locator('.row[data-code="002463"]').click();
    await expect(page.locator('.detail')).toBeVisible();
    await page.goBack();
    await expect(page.locator('.side')).toBeVisible();
  });

  test('reduced motion: numbers and the ladder render final values at once', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openMine(page, 'empty-account', '/mine?code=000988');
    const sizing = page.locator('.detail').getByRole('group', { name: '买入股数' });
    await expect(sizing.locator('[data-count="2100"]')).toHaveText('2,100');
    await expect(page.locator('.detail .ladder .lv').first()).toHaveCSS('opacity', '1');
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow and screenshots at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await openMine(page, 'empty-account');
      await expect(page.locator('.row[data-code="000988"]')).toBeVisible();
      await page.waitForLoadState('networkidle');
      await page.waitForTimeout(1200);
      await expectNoOverflow(page, `list ${width}`);
      await page.screenshot({ path: `test-results/fixtures/plan-mine-${width}.png`, animations: 'disabled' });

      await page.locator('.row[data-code="000988"]').click();
      await expect(page.locator('.detail').getByTestId('mine-verdict')).toHaveText('买入');
      await expect(page.locator('.detail .ladder .lv').first()).toBeVisible();
      await page.waitForTimeout(1400);
      await expectNoOverflow(page, `detail ${width}`);
      await page.screenshot({ path: `test-results/fixtures/plan-mine-${width}-detail.png`, animations: 'disabled' });
      await page.screenshot({
        path: `test-results/fixtures/plan-mine-${width}-detail-full.png`,
        animations: 'disabled',
        fullPage: true,
      });
    });
  }

  test('screenshots of the dialogs and the held view', async ({ page }) => {
    await page.setViewportSize({ width: 1366, height: 900 });
    await openMine(page);
    await page.waitForTimeout(1400);
    await page.screenshot({ path: 'test-results/fixtures/plan-mine-1366-held.png', animations: 'disabled', fullPage: true });
    await page.getByRole('button', { name: '持仓与资金' }).click();
    await expect(page.getByRole('dialog', { name: '持仓与资金' })).toBeVisible();
    await page.waitForTimeout(700);
    await expectNoOverflow(page, 'holdings dialog 1366');
    await page.screenshot({ path: 'test-results/fixtures/plan-mine-1366-holdings-dialog.png', animations: 'disabled' });
    await page.keyboard.press('Escape');

    for (const [width, height] of [[375, 812], [320, 640]] as Array<[number, number]>) {
      await page.setViewportSize({ width, height });
      await page.getByRole('button', { name: '持仓与资金' }).first().click();
      await expect(page.getByRole('dialog', { name: '持仓与资金' })).toBeVisible();
      await page.waitForTimeout(700);
      await expectNoOverflow(page, `holdings dialog ${width}`);
      await page.screenshot({ path: `test-results/fixtures/plan-mine-${width}-holdings-dialog.png`, animations: 'disabled' });
      await page.keyboard.press('Escape');
      await expect(page.getByRole('dialog')).toHaveCount(0);
    }
  });
});
