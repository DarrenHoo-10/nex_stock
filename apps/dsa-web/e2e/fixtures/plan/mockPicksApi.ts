import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page, Route } from '@playwright/test';

/**
 * Playwright mock of the EXISTING screening / watchlist endpoints used by the 机会选股 page, backed by the
 * JSON files in `plan/picks/`. Register it AFTER `mockPlanApi(page)` (the last registered route wins):
 *
 *   await mockPlanApi(page);
 *   const picks = await mockPicksApi(page, { history: 'latest' });
 *
 * Task flow: POST /screening/screen/tasks answers 202-style acceptance; the first poll of GET .../tasks/{id}
 * reports `processing` (progress 40), the following ones `completed` with `run-result.json` (or `failed`).
 */

const DIR = join(dirname(fileURLToPath(import.meta.url)), 'picks');

type Json = Record<string, unknown>;

function readFixture<T = Json>(name: string): T {
  return JSON.parse(readFileSync(join(DIR, name), 'utf8')) as T;
}

export interface MockPicksApiOptions {
  /** GET /screening/status: `enabled` (default) or `disabled`. */
  status?: 'enabled' | 'disabled';
  /** GET /screening/history: `none` (default, no earlier runs) or `latest` (fixture history). */
  history?: 'none' | 'latest';
  /** Final task outcome, default `completed`. */
  task?: 'completed' | 'failed';
  /** Number of `processing` polls before the final status, default 1. */
  processingPolls?: number;
  /** Codes already in the watchlist, default []. */
  watchlist?: string[];
}

export interface PicksApiCall {
  method: string;
  path: string;
  query: Record<string, string>;
  body: unknown;
}

export interface PicksApiMock {
  calls: PicksApiCall[];
  /** Current watchlist (changes after /watchlist/add). */
  watchlist: string[];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

export async function mockPicksApi(page: Page, options: MockPicksApiOptions = {}): Promise<PicksApiMock> {
  const { status = 'enabled', history = 'none', task = 'completed', processingPolls = 1 } = options;
  const mock: PicksApiMock = { calls: [], watchlist: [...(options.watchlist ?? [])] };
  let polls = 0;
  let lastStrategy = 'volume_breakout';

  const handle = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    mock.calls.push({ method, path, query: Object.fromEntries(url.searchParams.entries()), body });

    if (path === '/api/v1/screening/status') {
      await json(route, { enabled: status === 'enabled', available: status === 'enabled', engine: 'builtin' });
      return;
    }
    if (path === '/api/v1/screening/strategies') {
      await json(route, readFixture('strategies.json'));
      return;
    }
    if (path === '/api/v1/screening/screen/tasks' && method === 'POST') {
      polls = 0;
      lastStrategy = (body as { strategy?: string } | null)?.strategy ?? lastStrategy;
      await json(
        route,
        { task_id: 'task-fixture-1', status: 'pending', message: '选股任务已提交', strategy: lastStrategy, market: 'cn', max_results: 10 },
        202,
      );
      return;
    }
    if (path === '/api/v1/screening/screen/tasks/task-fixture-1' && method === 'GET') {
      polls += 1;
      if (polls <= processingPolls) {
        await json(route, { task_id: 'task-fixture-1', status: 'processing', progress: 40, message: '正在计算因子并筛选候选股' });
        return;
      }
      if (task === 'failed') {
        await json(route, { task_id: 'task-fixture-1', status: 'failed', error: '数据源暂时不可用，请稍后重试' });
        return;
      }
      await json(route, {
        task_id: 'task-fixture-1',
        status: 'completed',
        progress: 100,
        message: '任务执行完成',
        result: { ...readFixture('run-result.json'), strategy: lastStrategy },
      });
      return;
    }
    if (path === '/api/v1/screening/history' && method === 'GET') {
      await json(route, history === 'latest' ? readFixture('history.json') : { enabled: true, run_count: 0, runs: [] });
      return;
    }
    const runMatch = path.match(/^\/api\/v1\/screening\/history\/([^/]+)$/);
    if (runMatch && method === 'GET') {
      const result = readFixture('run-result.json');
      await json(route, {
        run_id: decodeURIComponent(runMatch[1]),
        strategy: result.strategy,
        market: 'cn',
        candidate_count: result.candidate_count,
        created_at: result.created_at,
        enabled: true,
        result,
      });
      return;
    }
    if (path === '/api/v1/stocks/watchlist' && method === 'GET') {
      await json(route, { stock_codes: mock.watchlist });
      return;
    }
    if (path === '/api/v1/stocks/watchlist/add' && method === 'POST') {
      const code = (body as { stock_code?: string } | null)?.stock_code;
      if (code && !mock.watchlist.includes(code)) mock.watchlist.push(code);
      await json(route, { stock_codes: mock.watchlist });
      return;
    }
    await json(route, { error: 'not_found', message: `未模拟的接口 ${method} ${path}` }, 404);
  };

  await page.route('**/api/v1/screening/**', handle);
  await page.route('**/api/v1/stocks/watchlist**', handle);
  return mock;
}
