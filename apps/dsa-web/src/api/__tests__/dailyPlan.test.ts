import { beforeEach, describe, expect, it, vi } from 'vitest';
import { attachParsedApiError } from '../error';
import { getBoardAnomalies, getBoardDetail, putBoardThresholds } from '../boards';
import {
  getLatestRun,
  getStockPlan,
  isPlanRunConflict,
  isPlanServiceUnavailable,
  previewSizing,
  putPlanHoldings,
} from '../dailyPlan';

const { get, post, put } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));

vi.mock('../index', () => ({ default: { get, post, put } }));

function httpError(status: number, data: unknown): Error {
  const error = Object.assign(new Error('Request failed'), { response: { status, data } });
  attachParsedApiError(error);
  return error;
}

describe('dailyPlan / boards api', () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    put.mockReset();
  });

  it('camel-cases responses and unwraps the latest run', async () => {
    get.mockResolvedValueOnce({ data: { run: { id: 'r1', trade_date: '2026-11-06', push: { sent_at: null } } } });
    await expect(getLatestRun()).resolves.toMatchObject({ id: 'r1', tradeDate: '2026-11-06', push: { sentAt: null } });
    expect(get).toHaveBeenCalledWith('/api/v1/plan/runs/latest');

    get.mockResolvedValueOnce({ data: { run: null } });
    await expect(getLatestRun()).resolves.toBeNull();
  });

  it('carries the personal push fields (channel, reason, per-type results) of a regular user\'s run', async () => {
    get.mockResolvedValueOnce({
      data: {
        run: {
          id: 'r2',
          trade_date: '2026-11-06',
          push: {
            channel: 'personal',
            status: 'sent',
            sent_at: '2026-11-06T09:05:00',
            error: null,
            configured: true,
            reason: null,
            channels: [{ channel: 'feishu', success: true }, { channel: 'email', success: false }],
          },
        },
      },
    });

    const run = await getLatestRun();

    expect(run?.push).toMatchObject({
      channel: 'personal',
      sentAt: '2026-11-06T09:05:00',
      channels: [{ channel: 'feishu', success: true }, { channel: 'email', success: false }],
    });
  });

  it('sends snake_case query params and bodies', async () => {
    get.mockResolvedValue({ data: { trade_date: '2026-11-06', boards: [], change_5d_pct: 1 } });
    await getBoardAnomalies({ tradeDate: '2026-11-06', boardType: 'industry', state: 'first_launch' });
    expect(get).toHaveBeenCalledWith('/api/v1/boards/anomalies', {
      params: { trade_date: '2026-11-06', board_type: 'industry', state: 'first_launch' },
    });

    await getBoardDetail('industry:BK0448');
    expect(get).toHaveBeenLastCalledWith('/api/v1/boards/industry%3ABK0448', { params: undefined });

    await getStockPlan('000988', '2026-11-06');
    expect(get).toHaveBeenLastCalledWith('/api/v1/plan/stocks/000988', { params: { trade_date: '2026-11-06' } });

    put.mockResolvedValue({ data: { cash: 1184, holdings: [] } });
    await putPlanHoldings({
      cash: 1184,
      holdings: [{ code: '002475', shares: 2400, cost: 40.2, buyDate: '2026-10-28', horizon: 'swing' }],
    });
    expect(put).toHaveBeenCalledWith('/api/v1/plan/holdings', {
      cash: 1184,
      holdings: [{ code: '002475', shares: 2400, cost: 40.2, buy_date: '2026-10-28', horizon: 'swing' }],
    });

    post.mockResolvedValue({ data: { status: 'ok', by_single: 2100 } });
    await expect(previewSizing({ code: '000988', price: 47, horizon: 'short' })).resolves.toMatchObject({ bySingle: 2100 });
    expect(post).toHaveBeenCalledWith('/api/v1/plan/sizing', { code: '000988', price: 47, horizon: 'short' });

    put.mockResolvedValue({ data: { items: [{ key: 'amount_ratio_mild', value: 1.5, default: 1.5 }] } });
    await expect(putBoardThresholds({})).resolves.toEqual([{ key: 'amount_ratio_mild', value: 1.5, default: 1.5 }]);
    expect(put).toHaveBeenLastCalledWith('/api/v1/boards/thresholds', { overrides: {} });
  });

  it('detects the 503 daily_plan_unavailable and 409 conflict errors', () => {
    const unavailable = httpError(503, { error: 'daily_plan_unavailable', message: '次日计划服务未就绪' });
    expect(isPlanServiceUnavailable(unavailable)).toBe(true);
    expect(isPlanServiceUnavailable(httpError(503, { detail: { error: 'daily_plan_unavailable' } }))).toBe(true);
    expect(isPlanServiceUnavailable(httpError(503, { error: 'other' }))).toBe(false);
    expect(isPlanServiceUnavailable(httpError(500, { error: 'internal_error' }))).toBe(false);
    expect(isPlanServiceUnavailable(new Error('boom'))).toBe(false);

    expect(isPlanRunConflict(httpError(409, { error: 'daily_plan_run_conflict', message: '已有运行' }))).toBe(true);
    expect(isPlanRunConflict(unavailable)).toBe(false);
  });
});
