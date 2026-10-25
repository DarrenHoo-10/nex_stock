import type { ScreeningScope, ScreeningTopic } from '../../api/screening';

export const BOARD_LABELS: Record<string, string> = {
  main_board: '沪深主板', chinext: '创业板', star_market: '科创板', bse: '北交所',
};
export const emptyScope = (): ScreeningScope => ({ boards: [], topics: [], topicMatch: 'any' });
export const sameTopic = (a: ScreeningTopic, b: ScreeningTopic) => a.name === b.name && a.source === b.source;
export const scopeLabel = (scope?: ScreeningScope) => scope
  ? `${scope.boards.map(board => BOARD_LABELS[board] || board).join('、') || '全部 A 股'} · ${scope.topics.map(topic => topic.name).join(scope.topicMatch === 'all' ? '且' : '或') || '全部题材'}`
  : '旧记录未保存筛选范围';

export function parseScope(value: unknown): ScreeningScope {
  if (!value || typeof value !== 'object') return emptyScope();
  const data = value as Partial<ScreeningScope>;
  return {
    boards: Array.isArray(data.boards) ? [...new Set(data.boards.filter(board => board in BOARD_LABELS))] : [],
    topics: Array.isArray(data.topics) ? data.topics.filter(topic => topic && typeof topic.name === 'string' && topic.name.trim() && ['concept', 'industry'].includes(topic.source)).slice(0, 5) : [],
    topicMatch: data.topicMatch === 'all' ? 'all' : 'any',
  };
}
