import type { ScreeningScope, ScreeningStrategy } from '../../../api/screening';
import { strategyLabel } from './pickMeta';
import type { PickTask } from './usePickTasks';

export function formatPickTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function scopeNames(scope: ScreeningScope | null | undefined): string[] {
  return (scope?.topics ?? []).map((topic) => topic.name).filter(Boolean);
}

export function scopeText(scope: ScreeningScope | null | undefined): string {
  const names = scopeNames(scope);
  return names.length ? names.join('、') : '全市场';
}

export function strategyName(id: string | null | undefined, strategies: ScreeningStrategy[]): string {
  if (!id) return '未记录策略';
  const match = strategies.find((item) => item.id === id);
  return match ? strategyLabel(match) : id;
}

export type TaskTone = 'run' | 'ok' | 'bad' | 'off';

/** Short status text and tone for a task row. */
export function taskStatus(task: PickTask): { label: string; tone: TaskTone } {
  switch (task.status) {
    case 'pending':
      return { label: '排队中', tone: 'run' };
    case 'processing': {
      const progress = Number(task.progress);
      return { label: Number.isFinite(progress) && progress > 0 ? `进行中 ${Math.round(progress)}%` : '进行中', tone: 'run' };
    }
    case 'completed':
      return { label: `完成 · ${task.candidateCount ?? task.result?.candidateCount ?? 0} 只`, tone: 'ok' };
    case 'failed':
      return { label: '失败', tone: 'bad' };
    case 'interrupted':
      return { label: '已中断', tone: 'off' };
    default:
      return { label: task.status || '未知', tone: 'off' };
  }
}
