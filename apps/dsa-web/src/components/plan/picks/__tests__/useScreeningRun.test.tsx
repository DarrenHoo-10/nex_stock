import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PICK_POLL_INTERVAL_MS, usePickRun } from '../useScreeningRun';

const { startScreen, getScreenTask, getHistory, getRun } = vi.hoisted(() => ({
  startScreen: vi.fn(),
  getScreenTask: vi.fn(),
  getHistory: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock('../../../../api/screening', () => ({
  screeningApi: { startScreen, getScreenTask, getHistory, getRun },
}));

const REQUEST = { market: 'cn', strategy: 'volume_breakout', maxResults: 10 };
const RESULT = { enabled: true, candidates: [{ rank: 1, code: '000988', name: '华工科技', reason: '', raw: {} }], candidateCount: 1 };

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  startScreen.mockReset();
  getScreenTask.mockReset();
  getHistory.mockReset();
  getRun.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('usePickRun', () => {
  it('starts a task, polls until it completes and exposes the result', async () => {
    startScreen.mockResolvedValue({ taskId: 't1', status: 'pending', message: '已提交' });
    getScreenTask
      .mockResolvedValueOnce({ taskId: 't1', status: 'processing', progress: 40, message: '计算中' })
      .mockResolvedValueOnce({ taskId: 't1', status: 'completed', progress: 100, result: RESULT });
    const { result } = renderHook(() => usePickRun());

    await act(async () => {
      void result.current.start(REQUEST);
    });
    await flush();
    expect(startScreen).toHaveBeenCalledWith(REQUEST);
    expect(result.current.state).toMatchObject({ phase: 'running', progress: 40, message: '计算中' });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(PICK_POLL_INTERVAL_MS);
    });
    expect(result.current.state.phase).toBe('done');
    expect(result.current.state.result?.candidates).toHaveLength(1);
    expect(result.current.state.fromHistory).toBe(false);
    expect(getScreenTask).toHaveBeenCalledTimes(2);
  });

  it('reports a failed task and a rejected submission', async () => {
    startScreen.mockResolvedValue({ taskId: 't1', status: 'pending', message: '' });
    getScreenTask.mockResolvedValue({ taskId: 't1', status: 'failed', error: '数据源不可用' });
    const { result } = renderHook(() => usePickRun());
    await act(async () => {
      void result.current.start(REQUEST);
    });
    await flush();
    expect(result.current.state).toMatchObject({ phase: 'failed', error: '数据源不可用' });

    startScreen.mockRejectedValue(new Error('boom'));
    await act(async () => {
      void result.current.start(REQUEST);
    });
    await flush();
    expect(result.current.state.phase).toBe('failed');
    expect(result.current.state.error).toBeTruthy();
  });

  it('gives up after repeated poll failures instead of spinning forever', async () => {
    startScreen.mockResolvedValue({ taskId: 't1', status: 'pending', message: '' });
    getScreenTask.mockRejectedValue(new Error('offline'));
    const { result } = renderHook(() => usePickRun());
    await act(async () => {
      void result.current.start(REQUEST);
    });
    await flush();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PICK_POLL_INTERVAL_MS * 6);
    });
    expect(result.current.state.phase).toBe('failed');
    expect(getScreenTask).toHaveBeenCalledTimes(5);
  });

  it('restores the newest matching run from the history', async () => {
    getHistory.mockResolvedValue({
      runs: [
        { runId: 'r-board', strategy: 'a', scope: { topics: [{ name: '商业航天', source: 'concept' }] } },
        { runId: 'r-all', strategy: 'b', scope: { topics: [] } },
      ],
    });
    getRun.mockResolvedValue({ runId: 'r-all', strategy: 'b', createdAt: '2026-11-06T16:00:00+08:00', result: RESULT });
    const { result } = renderHook(() => usePickRun());
    expect(result.current.state.phase).toBe('restoring');

    await act(async () => {
      await result.current.restoreLatest((run) => (run.scope?.topics ?? []).length === 0);
    });
    expect(getRun).toHaveBeenCalledWith('r-all');
    expect(result.current.state).toMatchObject({ phase: 'done', fromHistory: true });
    expect(result.current.state.result?.strategy).toBe('b');
  });

  it('starts empty when the history has no match or fails', async () => {
    getHistory.mockResolvedValue({ runs: [] });
    const { result } = renderHook(() => usePickRun());
    await act(async () => {
      await result.current.restoreLatest(() => true);
    });
    expect(result.current.state.phase).toBe('idle');

    getHistory.mockRejectedValue(new Error('down'));
    await act(async () => {
      await result.current.restoreLatest(() => true);
    });
    expect(result.current.state.phase).toBe('idle');
  });
});
