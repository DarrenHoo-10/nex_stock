import { useCallback, useEffect, useRef, useState } from 'react';
import { getParsedApiError } from '../../../api/error';
import { getPlanAccount, getStockPlan, isPlanServiceUnavailable, listStockPlans } from '../../../api/dailyPlan';
import type { AccountResponse, StockPlan, StockPlanListResponse } from '../../../types/dailyPlan';

export type Loadable<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  /** 404: nothing stored for this request. */
  | { status: 'notfound' }
  /** 503 `daily_plan_unavailable`. */
  | { status: 'unavailable' }
  | { status: 'error'; message: string };

export function classifyError<T>(error: unknown): Loadable<T> {
  if (isPlanServiceUnavailable(error)) {
    return { status: 'unavailable' };
  }
  const parsed = getParsedApiError(error);
  if (parsed.status === 404) {
    return { status: 'notfound' };
  }
  return { status: 'error', message: parsed.message };
}

interface Keyed<T> {
  key: string;
  state: Loadable<T>;
}

/**
 * Loads a resource per `key`; `load === null` means nothing to load (stays loading). A key change shows the
 * loading state; a `refreshKey` change or `reload(true)` refetches while keeping the current data (silent refresh
 * after a save). `reload()` clears to loading first. Late responses of an older request are dropped.
 */
export function useResource<T>(
  load: (() => Promise<T>) | null,
  key: string,
  refreshKey = 0,
): { state: Loadable<T>; reload: (silent?: boolean) => void; set: (data: T) => void } {
  const [entry, setEntry] = useState<Keyed<T> | null>(null);
  const [token, setToken] = useState(0);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });
  const enabled = load !== null;

  useEffect(() => {
    const run = loadRef.current;
    if (!enabled || !run) {
      return undefined;
    }
    let cancelled = false;
    run()
      .then((data) => {
        if (!cancelled) {
          setEntry({ key, state: { status: 'ready', data } });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setEntry({ key, state: classifyError<T>(error) });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, key, token, refreshKey]);

  const reload = useCallback((silent = false) => {
    if (!silent) {
      setEntry((previous) => (previous ? { key: previous.key, state: { status: 'loading' } } : previous));
    }
    setToken((value) => value + 1);
  }, []);
  const set = useCallback(
    (data: T) => {
      setEntry({ key, state: { status: 'ready', data } });
    },
    [key],
  );

  const state: Loadable<T> = entry && entry.key === key ? entry.state : { status: 'loading' };
  return { state, reload, set };
}

export interface MineOverview {
  account: Loadable<AccountResponse>;
  list: Loadable<StockPlanListResponse>;
  /** Refetch account and list; silent keeps the current screen while loading. */
  reload: (silent?: boolean) => void;
  reloadAccount: (silent?: boolean) => void;
  setAccount: (account: AccountResponse) => void;
  /** Bumped by every reload; the detail refetches when it changes. */
  version: number;
}

/** Account (cash, holdings, rules) and the plan summary list of the 我的股票 page. */
export function useMineOverview(): MineOverview {
  const accountResource = useResource(getPlanAccount, 'account');
  const listResource = useResource(() => listStockPlans(), 'list');
  const [version, setVersion] = useState(0);
  const { reload: reloadAccountOnly } = accountResource;
  const { reload: reloadList } = listResource;

  const reload = useCallback(
    (silent = false) => {
      reloadAccountOnly(silent);
      reloadList(silent);
      setVersion((value) => value + 1);
    },
    [reloadAccountOnly, reloadList],
  );

  return {
    account: accountResource.state,
    list: listResource.state,
    reload,
    reloadAccount: reloadAccountOnly,
    setAccount: accountResource.set,
    version,
  };
}

/** Full plan of one stock; refetches silently when `version` changes (after holdings are saved). */
export function useStockPlanDetail(code: string | null, version: number) {
  return useResource<StockPlan>(code ? () => getStockPlan(code) : null, code ?? '', version);
}
