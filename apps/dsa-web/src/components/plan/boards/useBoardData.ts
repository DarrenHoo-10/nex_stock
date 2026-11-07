import { useCallback, useEffect, useState } from 'react';
import { getBoardAnomalies, getBoardDetail, isPlanServiceUnavailable } from '../../../api/boards';
import { toApiErrorMessage } from '../../../api/error';
import type { BoardAnomalyListResponse, BoardDetail } from '../../../types/boards';

export type LoadState<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  /** 503 `daily_plan_unavailable`: the backend service module is not installed or failed to import. */
  | { status: 'unavailable' }
  | { status: 'error'; message: string };

/** GET /boards/anomalies once (the list is filtered on the client so the chips never need a reload). */
export function useBoardAnomalies(): LoadState<BoardAnomalyListResponse> & { reload: () => void } {
  const [state, setState] = useState<LoadState<BoardAnomalyListResponse>>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    getBoardAnomalies()
      .then((data) => {
        if (!cancelled) setState({ status: 'ready', data });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setState(
          isPlanServiceUnavailable(err)
            ? { status: 'unavailable' }
            : { status: 'error', message: toApiErrorMessage(err, '板块数据加载失败，请稍后重试。') },
        );
      });
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

type DetailEntry = { id: string | null; state: LoadState<BoardDetail> };

/** GET /boards/{id}. A change of `boardId` shows the loading state until its own response arrives. */
export function useBoardDetail(boardId: string | null): LoadState<BoardDetail> & { reload: () => void } {
  const [entry, setEntry] = useState<DetailEntry>({ id: null, state: { status: 'loading' } });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!boardId) return undefined;
    let cancelled = false;
    getBoardDetail(boardId)
      .then((data) => {
        if (!cancelled) setEntry({ id: boardId, state: { status: 'ready', data } });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setEntry({
          id: boardId,
          state: isPlanServiceUnavailable(err)
            ? { status: 'unavailable' }
            : { status: 'error', message: toApiErrorMessage(err, '板块详情加载失败，请稍后重试。') },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [boardId, reloadKey]);

  const reload = useCallback(() => {
    setEntry({ id: null, state: { status: 'loading' } });
    setReloadKey((key) => key + 1);
  }, []);

  const state: LoadState<BoardDetail> = entry.id === boardId ? entry.state : { status: 'loading' };
  return { ...state, reload };
}
