import { useCallback, useEffect, useState } from 'react';
import { getLatestRun, isPlanServiceUnavailable } from '../api/dailyPlan';
import { usePlanUiStore } from '../stores/planUiStore';
import type { PlanRun } from '../types/dailyPlan';

export type LatestPlanRunState =
  | { status: 'loading' }
  /** `run` is null when no run exists yet (`{"run": null}`). */
  | { status: 'ready'; run: PlanRun | null }
  /** 503 `daily_plan_unavailable`: the backend plan service is not installed or failed to import. */
  | { status: 'unavailable' }
  | { status: 'error' };

/**
 * Loads GET /plan/runs/latest. Reloads when `usePlanUiStore().refreshRun()` is called (for example after a
 * manual run is triggered) or when `reload()` is called.
 */
export function useLatestPlanRun(): LatestPlanRunState & { reload: () => void } {
  const [state, setState] = useState<LatestPlanRunState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);
  const refreshToken = usePlanUiStore((store) => store.runRefreshToken);

  useEffect(() => {
    let cancelled = false;
    getLatestRun()
      .then((run) => {
        if (!cancelled) {
          setState({ status: 'ready', run });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState(isPlanServiceUnavailable(error) ? { status: 'unavailable' } : { status: 'error' });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey, refreshToken]);

  const reload = useCallback(() => {
    setState({ status: 'loading' });
    setReloadKey((key) => key + 1);
  }, []);

  return { ...state, reload };
}
