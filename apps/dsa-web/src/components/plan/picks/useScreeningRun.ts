import { useCallback, useEffect, useRef, useState } from 'react';
import { screeningApi } from '../../../api/screening';
import type { ScreeningRequest, ScreeningRunSummary, ScreeningScreenResponse } from '../../../api/screening';
import { toApiErrorMessage } from '../../../api/error';

/** Same cadence as the existing screening page. */
export const PICK_POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_POLL_FAILURES = 5;
const HISTORY_LOOKUP_LIMIT = 10;

export type PickRunPhase =
  /** Looking for the last matching run in the screening history. */
  | 'restoring'
  | 'idle'
  /** Task submitted, waiting for the first status. */
  | 'starting'
  | 'running'
  | 'done'
  | 'failed';

export interface PickRunState {
  phase: PickRunPhase;
  /** 0-100, null when the backend reports none. */
  progress: number | null;
  message: string;
  result: ScreeningScreenResponse | null;
  /** The result came from the history (an earlier run) rather than from a task started on this page. */
  fromHistory: boolean;
  error: string | null;
}

const INITIAL: PickRunState = { phase: 'restoring', progress: null, message: '', result: null, fromHistory: false, error: null };

function isRunning(status: string): boolean {
  return status === 'pending' || status === 'processing';
}

/**
 * Drives one screening task through the existing screening API (start -> poll -> result) and restores the
 * latest matching run from the history on revisit. No backend endpoint is added.
 */
export function usePickRun() {
  const [state, setState] = useState<PickRunState>(INITIAL);
  // Bumped by every start/restore/unmount so late responses of an older request are dropped.
  const generation = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimer = useCallback(() => {
    if (timer.current !== null) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  useEffect(
    () => () => {
      generation.current += 1;
      clearTimer();
    },
    [clearTimer],
  );

  const start = useCallback(
    async (request: ScreeningRequest) => {
      const current = ++generation.current;
      clearTimer();
      const alive = () => generation.current === current;
      setState({ phase: 'starting', progress: 0, message: '正在提交选股任务…', result: null, fromHistory: false, error: null });
      try {
        const accepted = await screeningApi.startScreen(request);
        if (!alive()) return;
        setState((prev) => ({ ...prev, phase: 'running', message: accepted.message || '选股任务已提交' }));

        let failures = 0;
        const poll = async () => {
          try {
            const task = await screeningApi.getScreenTask(accepted.taskId);
            if (!alive()) return;
            failures = 0;
            if (task.status === 'completed') {
              setState(
                task.result
                  ? { phase: 'done', progress: 100, message: '', result: task.result, fromHistory: false, error: null }
                  : { ...INITIAL, phase: 'failed', error: '选股任务已完成，但服务端没有返回候选结果。' },
              );
              return;
            }
            if (task.status === 'failed') {
              setState({ ...INITIAL, phase: 'failed', error: task.error || task.message || '选股任务失败，请稍后重试。' });
              return;
            }
            if (!isRunning(task.status)) {
              setState({ ...INITIAL, phase: 'failed', error: `选股任务返回未知状态：${task.status || 'unknown'}` });
              return;
            }
            const progress = Number(task.progress);
            setState((prev) => ({
              ...prev,
              phase: 'running',
              progress: Number.isFinite(progress) ? Math.min(100, Math.max(0, progress)) : prev.progress,
              message: task.message || prev.message,
            }));
            timer.current = setTimeout(poll, PICK_POLL_INTERVAL_MS);
          } catch (err) {
            if (!alive()) return;
            failures += 1;
            if (failures >= MAX_CONSECUTIVE_POLL_FAILURES) {
              setState({ ...INITIAL, phase: 'failed', error: toApiErrorMessage(err, '无法读取选股进度，请稍后重试。') });
              return;
            }
            timer.current = setTimeout(poll, PICK_POLL_INTERVAL_MS);
          }
        };
        void poll();
      } catch (err) {
        if (!alive()) return;
        setState({ ...INITIAL, phase: 'failed', error: toApiErrorMessage(err, '选股任务提交失败，请稍后重试。') });
      }
    },
    [clearTimer],
  );

  /** Show the newest history run accepted by `match`. History failures are not fatal: the page just starts empty. */
  const restoreLatest = useCallback(
    async (match: (run: ScreeningRunSummary) => boolean) => {
      const current = ++generation.current;
      clearTimer();
      const alive = () => generation.current === current;
      setState(INITIAL);
      try {
        const history = await screeningApi.getHistory({ limit: HISTORY_LOOKUP_LIMIT });
        if (!alive()) return;
        const run = (history.runs ?? []).find(match);
        if (!run) {
          setState({ ...INITIAL, phase: 'idle' });
          return;
        }
        const detail = await screeningApi.getRun(run.runId);
        if (!alive()) return;
        if (!detail?.result) {
          setState({ ...INITIAL, phase: 'idle' });
          return;
        }
        setState({
          phase: 'done',
          progress: 100,
          message: '',
          result: {
            ...detail.result,
            strategy: detail.result.strategy || detail.strategy,
            createdAt: detail.result.createdAt || detail.createdAt || undefined,
          },
          fromHistory: true,
          error: null,
        });
      } catch {
        if (alive()) setState({ ...INITIAL, phase: 'idle' });
      }
    },
    [clearTimer],
  );

  /** No history lookup is needed (for example the board scope could not be resolved). */
  const skipRestore = useCallback(() => {
    generation.current += 1;
    clearTimer();
    setState({ ...INITIAL, phase: 'idle' });
  }, [clearTimer]);

  return { state, start, restoreLatest, skipRestore };
}
