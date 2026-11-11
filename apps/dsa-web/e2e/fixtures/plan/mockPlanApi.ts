import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Page, Route } from '@playwright/test';

/**
 * Playwright mock of the next-day plan / board anomaly API (contract v1), backed by the JSON files in this
 * folder. Bodies are snake_case exactly like the backend; the app converts them with `toCamelCase`.
 *
 * Usage:
 *   const api = await mockPlanApi(page, { scenario: 'default', latestRun: 'ok' });
 *   await page.goto('/mine');
 *   expect(api.calls.some((call) => call.method === 'PUT' && call.path === '/api/v1/plan/holdings')).toBe(true);
 *
 * Fixture scenario (default): holding 002475 (2,400 shares @ 40.20, max 1 holding, so 000988 is `buy` but
 * sizing is `max_holdings`). Scenario `empty-account`: cash 100,000 and no holdings, so 000988 sizes to
 * 2,100 shares / stop 42.30 (the numbers from core-product-spec 5.3).
 */

const FIXTURE_DIR = dirname(fileURLToPath(import.meta.url));

type Json = Record<string, unknown>;

function readFixture<T = Json>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURE_DIR, name), 'utf8')) as T;
}

export type PlanScenario = 'default' | 'empty-account';
export type LatestRunMode = 'ok' | 'none' | 'unavailable';

export interface MockPlanApiOptions {
  /** Account scenario, default `default`. */
  scenario?: PlanScenario;
  /** GET /plan/runs/latest behaviour, default `ok`. */
  latestRun?: LatestRunMode;
  /** Answer every plan/boards request with 503 `daily_plan_unavailable`. */
  unavailable?: boolean;
  /**
   * Also answer the non-plan `/api/**` calls the shell needs (auth status disabled/logged in, everything else an
   * empty JSON object). Default true. Turn off when the spec already routes the rest of the API.
   */
  includeBaseApi?: boolean;
}

export interface RecordedCall {
  method: string;
  path: string;
  query: Record<string, string>;
  body: unknown;
}

export interface PlanApiMock {
  /** Every plan/boards request seen, in order. */
  calls: RecordedCall[];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function notFound(route: Route, message: string) {
  return json(route, { error: 'not_found', message }, 404);
}

function unavailableResponse(route: Route) {
  return json(route, { error: 'daily_plan_unavailable', message: '次日计划服务未就绪' }, 503);
}

/** A detail for boards that have no dedicated fixture, derived from the list row. */
function deriveBoardDetail(row: Json): Json {
  const base = readFixture('board-detail.json');
  return {
    ...base,
    ...row,
    verdict_text: row.reason,
    drivers: String(row.reason).includes('原因未明')
      ? '原因未明：没有找到当天的公告或新闻。'
      : `${String(row.name)}：${String(row.reason)}`,
    driver_kind: String(row.reason).includes('原因未明') ? 'unknown' : 'capital',
    can_pick: !(row.tags as string[]).includes('single_stock') && row.state !== 'insufficient_history',
    representatives: [],
    evidence: [],
  };
}

export async function mockPlanApi(page: Page, options: MockPlanApiOptions = {}): Promise<PlanApiMock> {
  const { scenario = 'default', latestRun = 'ok', unavailable = false, includeBaseApi = true } = options;
  const mock: PlanApiMock = { calls: [] };

  if (includeBaseApi) {
    await page.route('**/api/**', async (route) => {
      const path = new URL(route.request().url()).pathname;
      // Vite source modules also contain /api/; only intercept HTTP API paths.
      if (!path.startsWith('/api/')) {
        await route.continue();
        return;
      }
      if(path === '/api/v1/finance/assistant/status') {await json(route,{managed:false,bound:false});return;}
      if(path === '/api/v1/finance/assistant/feishu-settings') {await json(route,{managed:false,owner:'stock',version:0,appliesToCurrentApp:false});return;}
      if (path === '/api/v1/auth/status') {
        await json(route, { authEnabled: false, loggedIn: true, passwordSet: true, setupState: 'enabled' });
        return;
      }
      await json(route, { success: true, items: [], total: 0 });
    });
  }

  const handle = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const query = Object.fromEntries(url.searchParams.entries());
    let body: unknown = null;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    mock.calls.push({ method, path, query, body });

    if (unavailable) {
      await unavailableResponse(route);
      return;
    }

    // ---- plan
    if (path === '/api/v1/plan/account' && method === 'GET') {
      await json(route, readFixture(scenario === 'empty-account' ? 'account-empty.json' : 'account.json'));
      return;
    }
    if (path === '/api/v1/plan/holdings' && method === 'PUT') {
      await json(route, readFixture(scenario === 'empty-account' ? 'account-empty.json' : 'account.json'));
      return;
    }
    if (path === '/api/v1/plan/stocks' && method === 'GET') {
      await json(route, readFixture('stocks.json'));
      return;
    }
    const stockMatch = path.match(/^\/api\/v1\/plan\/stocks\/([^/]+)$/);
    if (stockMatch && method === 'GET') {
      const code = decodeURIComponent(stockMatch[1]);
      const variant = code === '000988' && scenario === 'empty-account' ? 'stock-000988-empty-account.json' : `stock-${code}.json`;
      try {
        await json(route, readFixture(variant));
      } catch {
        await notFound(route, '没有找到该股票的计划');
      }
      return;
    }
    if (path === '/api/v1/plan/sizing' && method === 'POST') {
      await json(route, readFixture('sizing-demo.json'));
      return;
    }
    if (path === '/api/v1/plan/runs/latest' && method === 'GET') {
      if (latestRun === 'unavailable') {
        await unavailableResponse(route);
      } else if (latestRun === 'none') {
        await json(route, { run: null });
      } else {
        await json(route, readFixture('latest-run.json'));
      }
      return;
    }
    const runMatch = path.match(/^\/api\/v1\/plan\/runs\/([^/]+)$/);
    if (runMatch && method === 'GET') {
      const run = (readFixture('latest-run.json') as { run: Json }).run;
      if (decodeURIComponent(runMatch[1]) === run.id) {
        await json(route, run);
      } else {
        await notFound(route, '没有找到这次运行');
      }
      return;
    }
    if (path === '/api/v1/plan/runs' && method === 'POST') {
      const run = (readFixture('latest-run.json') as { run: Json }).run;
      await json(route, { ...run, status: 'running', trigger: 'manual', finished_at: null, duration_sec: null }, 202);
      return;
    }

    // ---- boards
    if (path === '/api/v1/boards/anomalies' && method === 'GET') {
      const data = readFixture<{ boards: Json[] } & Json>('boards.json');
      const boards = data.boards.filter(
        (board) => (!query.board_type || board.board_type === query.board_type) && (!query.state || board.state === query.state),
      );
      await json(route, { ...data, boards });
      return;
    }
    if (path === '/api/v1/boards/thresholds') {
      // GET returns the definitions; PUT answers with the same list (the fixtures do not persist overrides).
      await json(route, { items: readFixture<unknown[]>('thresholds.json') });
      return;
    }
    const boardMatch = path.match(/^\/api\/v1\/boards\/([^/]+)$/);
    if (boardMatch && method === 'GET') {
      const id = decodeURIComponent(boardMatch[1]);
      const detail = readFixture('board-detail.json');
      if (detail.id === id) {
        await json(route, detail);
        return;
      }
      const row = readFixture<{ boards: Json[] }>('boards.json').boards.find((board) => board.id === id);
      if (row) {
        await json(route, deriveBoardDetail(row));
      } else {
        await notFound(route, '没有找到该板块');
      }
      return;
    }

    await notFound(route, `未模拟的接口 ${method} ${path}`);
  };

  await page.route('**/api/v1/plan/**', handle);
  await page.route('**/api/v1/boards/**', handle);
  return mock;
}
