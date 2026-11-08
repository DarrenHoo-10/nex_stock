import { useCallback, useEffect, useRef, useState } from 'react';
import { screeningApi } from '../../../api/screening';
import type { ScreeningRequest, ScreeningScreenTaskStatus } from '../../../api/screening';
import { toApiErrorMessage } from '../../../api/error';

/** Refresh cadence while a task is pending or running. */
export const PICK_POLL_INTERVAL_MS = 3000;
export const PICK_TASK_PAGE_SIZE = 20;

export type PickTask = ScreeningScreenTaskStatus;

export function isActiveTask(task: Pick<PickTask, 'status'>): boolean {
  return task.status === 'pending' || task.status === 'processing';
}

export type PickTaskListState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; tasks: PickTask[]; total: number };

/**
 * Persisted screening tasks (newest first). Refreshes every few seconds while any listed task is still running;
 * `loadMore` appends the next page.
 */
export function usePickTaskList() {
  const [state, setState] = useState<PickTaskListState>({ status: 'loading' });
  const [pages, setPages] = useState(1);
  const [refreshKey, setRefreshKey] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const response = await screeningApi.listScreenTasks({ limit: PICK_TASK_PAGE_SIZE * pages, offset: 0 });
        if (cancelled) return;
        const tasks = response.tasks ?? [];
        setState({ status: 'ready', tasks, total: response.total ?? tasks.length });
        if (tasks.some(isActiveTask)) {
          timer.current = setTimeout(() => void load(), PICK_POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        setState((prev) =>
          prev.status === 'ready' ? prev : { status: 'error', message: toApiErrorMessage(err, '选股任务读取失败，请稍后重试。') },
        );
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, [pages, refreshKey]);

  const reload = useCallback(() => setRefreshKey((key) => key + 1), []);
  const loadMore = useCallback(() => setPages((count) => count + 1), []);

  return { state, reload, loadMore };
}

export type PickTaskDetailState =
  | { status: 'loading' }
  | { status: 'missing' }
  | { status: 'error'; message: string }
  | { status: 'ready'; task: PickTask };

/** One task with its result once completed; refreshes while the task is pending or running. */
export function usePickTask(taskId: string | null) {
  const [state, setState] = useState<PickTaskDetailState>({ status: 'loading' });
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (!taskId) return undefined;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setState({ status: 'loading' });
    const load = async () => {
      try {
        const task = await screeningApi.getScreenTask(taskId);
        if (cancelled) return;
        setState({ status: 'ready', task });
        if (isActiveTask(task)) {
          timer = setTimeout(() => void load(), PICK_POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (cancelled) return;
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 404) {
          setState({ status: 'missing' });
          return;
        }
        setState((prev) => {
          if (prev.status === 'ready' && isActiveTask(prev.task)) {
            // A failed refresh keeps the last progress on screen and tries again.
            timer = setTimeout(() => void load(), PICK_POLL_INTERVAL_MS);
            return prev;
          }
          return { status: 'error', message: toApiErrorMessage(err, '选股任务读取失败，请稍后重试。') };
        });
      }
    };
    void load();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [taskId, refreshKey]);

  const reload = useCallback(() => setRefreshKey((key) => key + 1), []);
  return { state, reload };
}

/** Submit one screening task; resolves with its id. */
export async function startPickTask(request: ScreeningRequest): Promise<string> {
  const accepted = await screeningApi.startScreen(request);
  return accepted.taskId;
}
