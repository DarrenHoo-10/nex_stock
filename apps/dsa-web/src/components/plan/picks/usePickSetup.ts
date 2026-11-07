import { useCallback, useEffect, useState } from 'react';
import { screeningApi } from '../../../api/screening';
import type { ScreeningStrategy } from '../../../api/screening';
import { toApiErrorMessage } from '../../../api/error';
import { systemConfigApi } from '../../../api/systemConfig';

export type PickSetupState =
  | { status: 'loading' }
  | { status: 'ready'; strategies: ScreeningStrategy[] }
  /** Screening is switched off or unavailable in the backend configuration. */
  | { status: 'disabled'; message: string }
  | { status: 'error'; message: string };

/** Screening status plus the strategy list (existing `/screening/status` and `/screening/strategies`). */
export function usePickSetup(): PickSetupState & { reload: () => void } {
  const [state, setState] = useState<PickSetupState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const status = await screeningApi.getStatus();
        if (cancelled) return;
        if (!status.enabled || !status.available) {
          setState({
            status: 'disabled',
            message: status.enabled ? '选股功能当前不可用，请检查策略配置和服务日志。' : '选股功能还没有开启。',
          });
          return;
        }
        const result = await screeningApi.getStrategies();
        if (cancelled) return;
        setState({ status: 'ready', strategies: result.strategies ?? [] });
      } catch (err) {
        if (!cancelled) setState({ status: 'error', message: toApiErrorMessage(err, '选股策略加载失败，请稍后重试。') });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const reload = useCallback(() => {
    setState({ status: 'loading' });
    setReloadKey((key) => key + 1);
  }, []);

  return { ...state, reload };
}

/** Watchlist codes (existing `/stocks/watchlist`). A failed read leaves the set empty; adding still works. */
export function useWatchlist() {
  const [codes, setCodes] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    let cancelled = false;
    systemConfigApi
      .getWatchlist()
      .then((list) => {
        if (!cancelled) setCodes(new Set(list));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  /** Adds the code; rejects with the API error so the caller can show it. */
  const add = useCallback(async (code: string) => {
    const list = await systemConfigApi.addToWatchlist(code);
    setCodes((previous) => new Set(list.length ? list : [...previous, code]));
  }, []);

  return { codes, add };
}
