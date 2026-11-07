import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  AccountResponse,
  HoldingInput,
  HoldingsSnapshotRequest,
  LatestRunResponse,
  PlanRun,
  Sizing,
  SizingRequest,
  StockPlan,
  StockPlanListResponse,
} from '../types/dailyPlan';

export { isPlanRunConflict, isPlanServiceUnavailable } from './planErrors';

function tradeDateParams(tradeDate?: string): Record<string, string> | undefined {
  return tradeDate ? { trade_date: tradeDate } : undefined;
}

function toSnakeHolding(holding: HoldingInput): Record<string, unknown> {
  return {
    code: holding.code,
    shares: holding.shares,
    cost: holding.cost,
    buy_date: holding.buyDate ?? null,
    horizon: holding.horizon ?? null,
  };
}

/** GET /api/v1/plan/account — cash, holdings and effective position rules. */
export async function getPlanAccount(): Promise<AccountResponse> {
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/plan/account');
  return toCamelCase<AccountResponse>(response.data);
}

/** PUT /api/v1/plan/holdings — replaces cash and the whole holding list. 400 carries the Chinese reason. */
export async function putPlanHoldings(snapshot: HoldingsSnapshotRequest): Promise<AccountResponse> {
  const response = await apiClient.put<Record<string, unknown>>('/api/v1/plan/holdings', {
    cash: snapshot.cash,
    holdings: snapshot.holdings.map(toSnakeHolding),
  });
  return toCamelCase<AccountResponse>(response.data);
}

/** GET /api/v1/plan/stocks — plan summaries for watchlist plus holdings. */
export async function listStockPlans(tradeDate?: string): Promise<StockPlanListResponse> {
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/plan/stocks', {
    params: tradeDateParams(tradeDate),
  });
  return toCamelCase<StockPlanListResponse>(response.data);
}

/** GET /api/v1/plan/stocks/{code} — full plan with both horizons (404 when missing). */
export async function getStockPlan(code: string, tradeDate?: string): Promise<StockPlan> {
  const response = await apiClient.get<Record<string, unknown>>(
    `/api/v1/plan/stocks/${encodeURIComponent(code)}`,
    { params: tradeDateParams(tradeDate) },
  );
  return toCamelCase<StockPlan>(response.data);
}

/** POST /api/v1/plan/sizing — share count preview for recording a buy or manual what-if. */
export async function previewSizing(request: SizingRequest): Promise<Sizing> {
  const response = await apiClient.post<Record<string, unknown>>('/api/v1/plan/sizing', {
    code: request.code,
    price: request.price,
    horizon: request.horizon,
  });
  return toCamelCase<Sizing>(response.data);
}

/** GET /api/v1/plan/runs/latest — resolves to null when there is no run yet. */
export async function getLatestRun(): Promise<PlanRun | null> {
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/plan/runs/latest');
  return toCamelCase<LatestRunResponse>(response.data).run ?? null;
}

/** GET /api/v1/plan/runs/{run_id} (404 when unknown). */
export async function getRun(runId: string): Promise<PlanRun> {
  const response = await apiClient.get<Record<string, unknown>>(`/api/v1/plan/runs/${encodeURIComponent(runId)}`);
  return toCamelCase<PlanRun>(response.data);
}

/** POST /api/v1/plan/runs — manual run (202). 409 `daily_plan_run_conflict` when one is in progress. */
export async function triggerRun(): Promise<PlanRun> {
  const response = await apiClient.post<Record<string, unknown>>('/api/v1/plan/runs');
  return toCamelCase<PlanRun>(response.data);
}
