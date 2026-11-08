import { getParsedApiError } from './error';

/** Error codes returned by the next-day plan / board anomaly endpoints. */
export const PLAN_UNAVAILABLE_CODE = 'daily_plan_unavailable';
export const PLAN_RUN_CONFLICT_CODE = 'daily_plan_run_conflict';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * Extract the `{"error": code}` field of an API error body. Supports both the flat body
 * and a FastAPI `detail` wrapper. Returns null when the error carries no code.
 */
export function getPlanErrorCode(err: unknown): string | null {
  const response = isRecord(err) && isRecord(err.response) ? err.response : null;
  const data = response?.data;
  if (!isRecord(data)) {
    return null;
  }
  const detail = isRecord(data.detail) ? data.detail : null;
  const code = data.error ?? data.code ?? detail?.error ?? detail?.code;
  return typeof code === 'string' && code.trim() ? code.trim() : null;
}

/** True for 503 `daily_plan_unavailable` (backend service module failed to import). */
export function isPlanServiceUnavailable(err: unknown): boolean {
  if (getPlanErrorCode(err) !== PLAN_UNAVAILABLE_CODE) {
    return false;
  }
  const status = getParsedApiError(err).status;
  return status === undefined || status === 503;
}

/** True for 409 `daily_plan_run_conflict` (a run is already in progress). */
export function isPlanRunConflict(err: unknown): boolean {
  return getPlanErrorCode(err) === PLAN_RUN_CONFLICT_CODE;
}
