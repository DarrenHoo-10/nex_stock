import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';
import { mockPlanApi } from './plan/mockPlanApi';

// Deterministic fixture spec for the assistant drawer: no backend and no LLM; the SSE stream is mocked.

const FIXTURE_DIR = join(dirname(fileURLToPath(import.meta.url)), 'plan', 'assistant');
const STREAM = JSON.parse(readFileSync(join(FIXTURE_DIR, 'stream-answer.json'), 'utf8')) as {
  answer: string;
  progress: string[];
};

const WIDTHS: Array<[number, number]> = [
  [1366, 900],
  [1024, 768],
  [375, 812],
  [320, 640],
];

interface StreamBody {
  message: string;
  session_id: string;
  request_id: string;
  context?: { daily_plan?: Record<string, unknown>; stock_code?: string; stock_name?: string };
}

type StreamMode = 'ok' | 'error';

async function mockAgent(page: Page, mode: { current: StreamMode } = { current: 'ok' }) {
  const bodies: StreamBody[] = [];
  await page.route('**/api/v1/agent/chat/sessions*', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sessions: [] }) }),
  );
  await page.route('**/api/v1/agent/chat/stream', async (route) => {
    const body = route.request().postDataJSON() as StreamBody;
    bodies.push(body);
    const ids = { request_id: body.request_id, session_id: body.session_id };
    const events =
      mode.current === 'error'
        ? [
            { type: 'accepted', backend: 'litellm', ...ids },
            { type: 'error', message: '助手暂时不可用，请稍后重试', ...ids },
          ]
        : [
            { type: 'accepted', backend: 'litellm', ...ids },
            ...STREAM.progress.map((message, index) => ({
              type: index === STREAM.progress.length - 1 ? 'generating' : 'thinking',
              message,
              ...ids,
            })),
            { type: 'done', success: true, content: STREAM.answer, ...ids },
          ];
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(''),
    });
  });
  return { bodies };
}

/**
 * Other pages call `usePlanUiStore().openAssistant(context)`; this does the same from the test by importing the
 * store module the app itself loaded (same URL, so the same instance).
 */
async function openAssistantWith(page: Page, context: Record<string, unknown>) {
  await page.evaluate(async (ctx) => {
    const loaded = performance
      .getEntriesByType('resource')
      .map((entry) => entry.name)
      .find((name) => /\/src\/stores\/planUiStore\.ts/.test(name));
    const store = (await import(/* @vite-ignore */ loaded ?? '/src/stores/planUiStore.ts')) as {
      usePlanUiStore: { getState: () => { openAssistant: (value?: Record<string, unknown>) => void } };
    };
    store.usePlanUiStore.getState().openAssistant(ctx);
  }, context);
}

async function horizontalOverflow(page: Page) {
  return page.evaluate(() => ({
    viewport: window.innerWidth,
    root: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
}

const STOCK_CONTEXT = {
  kind: 'stock',
  stockCode: '000988',
  stockName: '华工科技',
  horizon: 'short',
  held: false,
};

test.describe('plan assistant drawer', () => {
  test('opens from the header button without context, streams an answer, Escape closes and focus returns', async ({
    page,
  }) => {
    await mockPlanApi(page);
    const agent = await mockAgent(page);
    await page.goto('/preferences');
    const trigger = page.getByRole('button', { name: '助手', exact: true });
    await expect(trigger).toBeVisible({ timeout: 20_000 });
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await trigger.click();
    const drawer = page.getByRole('dialog', { name: '助手' });
    await expect(drawer).toBeVisible({ timeout: 20_000 });
    await expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await expect(drawer.getByTestId('assistant-context-chip')).toHaveText('未选中');
    await expect(drawer.getByRole('button', { name: '取消选中对象' })).toHaveCount(0);
    await expect(drawer.getByRole('link', { name: '在问股页打开' })).toHaveAttribute('href', '/chat');
    await expect(drawer.getByLabel('输入问题')).toBeFocused();
    await expect(drawer.getByRole('group', { name: '常用问题' }).getByRole('button')).toHaveCount(3);

    await drawer.getByLabel('输入问题').fill('今天哪个板块最值得关注');
    await drawer.getByRole('button', { name: '发送' }).click();
    await expect(drawer.getByText('不追高，等回踩。')).toBeVisible();
    await expect(drawer.locator('.assist-md table')).toBeVisible();
    await expect(drawer.locator('.msg.me')).toHaveText('今天哪个板块最值得关注');

    expect(agent.bodies).toHaveLength(1);
    expect(agent.bodies[0].message).toBe('今天哪个板块最值得关注');
    expect(agent.bodies[0].context?.daily_plan).toMatchObject({
      kind: 'none',
      board_id: null,
      stock_code: null,
      horizon: null,
      trade_date: '2026-11-06',
    });

    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
    await expect(trigger).toBeFocused();
    await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  test('a board and a stock context are sent as context.daily_plan and can be removed', async ({ page }) => {
    await mockPlanApi(page);
    const agent = await mockAgent(page);
    await page.goto('/preferences');
    await expect(page.getByRole('button', { name: '助手', exact: true })).toBeVisible({ timeout: 20_000 });

    await openAssistantWith(page, { kind: 'board', boardId: 'industry:BK0448', boardName: '通信设备' });
    const drawer = page.getByRole('dialog', { name: '助手' });
    await expect(drawer.getByTestId('assistant-context-chip')).toHaveText('当前：板块 通信设备');
    await drawer.getByRole('button', { name: '这个板块还能持续吗' }).click();
    await expect(drawer.getByText('不追高，等回踩。')).toBeVisible();
    expect(agent.bodies[0].context?.daily_plan).toMatchObject({
      kind: 'board',
      board_id: 'industry:BK0448',
      stock_code: null,
    });
    expect(agent.bodies[0].context?.stock_code).toBeUndefined();

    // A different context starts a fresh conversation.
    await page.keyboard.press('Escape');
    await expect(drawer).toBeHidden();
    await openAssistantWith(page, STOCK_CONTEXT);
    await expect(drawer.getByTestId('assistant-context-chip')).toHaveText('当前：华工科技 · 短线');
    await expect(drawer.locator('.msg')).toHaveCount(0);
    await drawer.getByRole('button', { name: '如果明天低开怎么办' }).click();
    await expect(drawer.getByText('不追高，等回踩。')).toBeVisible();
    expect(agent.bodies[1].context).toMatchObject({
      daily_plan: { kind: 'stock', board_id: null, stock_code: '000988', horizon: 'short' },
      stock_code: '000988',
      stock_name: '华工科技',
    });
    expect(agent.bodies[1].session_id).not.toBe(agent.bodies[0].session_id);

    // Removing the chip keeps the conversation but stops sending the object.
    await drawer.getByRole('button', { name: '取消选中对象' }).click();
    await expect(drawer.getByTestId('assistant-context-chip')).toHaveText('未选中');
    await expect(drawer.locator('.msg.me')).toHaveCount(1);
    await drawer.getByLabel('输入问题').fill('换个问题');
    await drawer.getByRole('button', { name: '发送' }).click();
    await expect.poll(() => agent.bodies.length).toBe(3);
    expect(agent.bodies[2].context?.daily_plan).toMatchObject({ kind: 'none', stock_code: null });
    expect(agent.bodies[2].session_id).toBe(agent.bodies[1].session_id);
  });

  test('shows the error state and retries the last question', async ({ page }) => {
    await mockPlanApi(page);
    const mode: { current: StreamMode } = { current: 'error' };
    const agent = await mockAgent(page, mode);
    await page.goto('/preferences');
    await expect(page.getByRole('button', { name: '助手', exact: true })).toBeVisible({ timeout: 20_000 });
    await page.getByRole('button', { name: '助手', exact: true }).click();
    const drawer = page.getByRole('dialog', { name: '助手' });
    await drawer.getByRole('button', { name: '我的持仓明天怎么做' }).click();
    await expect(drawer.getByRole('alert')).toContainText('助手暂时不可用');

    mode.current = 'ok';
    await drawer.getByRole('button', { name: '重试' }).click();
    await expect(drawer.getByText('不追高，等回踩。')).toBeVisible();
    await expect(drawer.getByRole('alert')).toHaveCount(0);
    expect(agent.bodies.map((body) => body.message)).toEqual(['我的持仓明天怎么做', '我的持仓明天怎么做']);
  });

  for (const [width, height] of WIDTHS) {
    test(`no horizontal overflow at ${width}px`, async ({ page }) => {
      await mockPlanApi(page);
      await mockAgent(page);
      await page.setViewportSize({ width, height });
      await page.goto('/preferences');
      await expect(page.getByRole('button', { name: '助手', exact: true })).toBeVisible({ timeout: 20_000 });

      await openAssistantWith(page, STOCK_CONTEXT);
      const drawer = page.getByRole('dialog', { name: '助手' });
      await expect(drawer).toBeVisible();
      await drawer.getByRole('button', { name: '如果明天低开怎么办' }).click();
      await expect(drawer.getByText('不追高，等回踩。')).toBeVisible();
      await page.waitForTimeout(800);

      const sizes = await horizontalOverflow(page);
      expect(sizes.root).toBeLessThanOrEqual(sizes.viewport);
      expect(sizes.body).toBeLessThanOrEqual(sizes.viewport);
      const box = await drawer.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(width + 0.5);
      if (width <= 860) {
        // Full screen on small screens.
        expect(box!.width).toBeGreaterThanOrEqual(width - 1);
        expect(box!.height).toBeGreaterThanOrEqual(height - 1);
      }
      const inner = await drawer.evaluate((element) => element.scrollWidth <= element.clientWidth + 1);
      expect(inner, 'drawer content does not scroll horizontally').toBe(true);
      await page.screenshot({ path: `test-results/fixtures/plan-assistant-${width}.png`, animations: 'disabled' });
    });
  }
});
