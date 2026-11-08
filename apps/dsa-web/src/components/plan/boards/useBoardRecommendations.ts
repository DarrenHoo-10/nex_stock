import { useCallback, useEffect, useState } from 'react';
import { getBoardRecommendations, refreshBoardRecommendations } from '../../../api/boards';
import { toApiErrorMessage } from '../../../api/error';
import type { RecommendationResponse } from '../../../types/boards';

const POLL_MS = 4000;

export type RecommendationState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: RecommendationResponse };

/** Recommended boards of the latest run; polls while a (re)generation is running. */
export function useBoardRecommendations() {
  const [state, setState] = useState<RecommendationState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = async () => {
      try {
        const data = await getBoardRecommendations();
        if (cancelled) return;
        setState({ status: 'ready', data });
        if (data.status === 'generating') timer = setTimeout(() => void load(), POLL_MS);
      } catch (err) {
        if (!cancelled) setState({ status: 'error', message: toApiErrorMessage(err, '推荐板块读取失败') });
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [reloadKey]);

  /** Ask the backend to regenerate (runs in the background), then follow its status. */
  const regenerate = useCallback(async (): Promise<string | null> => {
    try {
      const data = await refreshBoardRecommendations();
      setState({ status: 'ready', data });
      setReloadKey((key) => key + 1);
      return null;
    } catch (err) {
      return toApiErrorMessage(err, '重新生成失败，请稍后重试');
    }
  }, []);

  const reload = useCallback(() => setReloadKey((key) => key + 1), []);
  return { state, regenerate, reload };
}
