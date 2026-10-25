import { useEffect, useState } from 'react';
import { screeningApi, type ScreeningScope, type ScreeningTopic } from '../../api/screening';
import { toApiErrorMessage } from '../../api/error';

import { BOARD_LABELS, sameTopic } from './scope';

type Props = {
  value: ScreeningScope;
  onChange: (scope: ScreeningScope) => void;
  query: string;
  onQueryChange: (query: string) => void;
  disabled: boolean;
};

export function ScopeControls({ value, onChange, query, onQueryChange, disabled }: Props) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ query: string; attempt: number; items: ScreeningTopic[]; message: string } | null>(null);
  useEffect(() => {
    if (!query.trim() || disabled) return;
    let active = true;
    const timer = window.setTimeout(() => {
      void screeningApi.searchTopics(query).then(data => {
        if (active) setResult({ query, attempt, items: data.items, message: data.warnings.join(' ') });
      }).catch(error => {
        if (active) setResult({ query, attempt, items: [], message: toApiErrorMessage(error, '题材搜索失败，请重试。') });
      });
    }, 350);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query, disabled, attempt]);

  const current = result?.query === query && result.attempt === attempt ? result : null;
  const chip = 'rounded-lg border px-3 py-2 text-sm transition-colors disabled:opacity-50';
  const selected = 'border-cyan bg-cyan/10 text-cyan';
  const unselected = 'border-border bg-surface text-secondary-text hover:text-foreground';
  return <div className="mb-5 space-y-4 border-b border-border pb-5">
    <fieldset disabled={disabled}>
      <legend className="mb-2 text-xs font-medium text-secondary-text">交易板块</legend>
      <div className="flex flex-wrap gap-2">
        <button type="button" aria-pressed={!value.boards.length} className={`${chip} ${!value.boards.length ? selected : unselected}`} onClick={() => onChange({ ...value, boards: [] })}>全部 A 股</button>
        {Object.entries(BOARD_LABELS).map(([id, label]) => <button key={id} type="button" aria-pressed={value.boards.includes(id)} className={`${chip} ${value.boards.includes(id) ? selected : unselected}`} onClick={() => onChange({ ...value, boards: value.boards.includes(id) ? value.boards.filter(board => board !== id) : [...value.boards, id] })}>{label}</button>)}
      </div>
    </fieldset>
    <fieldset disabled={disabled} className="space-y-2">
      <legend className="mb-2 text-xs font-medium text-secondary-text">题材范围（最多 5 个）</legend>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={`${chip} ${!value.topics.length ? selected : unselected}`} aria-pressed={!value.topics.length} onClick={() => onChange({ ...value, topics: [] })}>全部题材</button>
        {value.topics.map(topic => <button type="button" key={`${topic.source}:${topic.name}`} className={`${chip} ${selected}`} aria-label={`移除题材 ${topic.name}`} onClick={() => onChange({ ...value, topics: value.topics.filter(item => !sameTopic(item, topic)) })}>{topic.name} ×</button>)}
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input aria-label="搜索题材" value={query} maxLength={80} onChange={event => onQueryChange(event.target.value)} placeholder="搜索题材或行业，不限于当前热点" className="h-11 min-w-0 shrink-0 sm:flex-1 rounded-xl border border-border bg-surface px-3 text-sm text-foreground focus:border-cyan" />
        <select aria-label="题材匹配方式" value={value.topicMatch} onChange={event => onChange({ ...value, topicMatch: event.target.value as 'any' | 'all' })} className="h-11 rounded-xl border border-border bg-surface px-3 text-sm text-foreground">
          <option value="any">属于任一题材</option><option value="all">同时属于全部题材</option>
        </select>
      </div>
      {query.trim() && !disabled && <div aria-live="polite" className="max-h-48 overflow-auto rounded-xl border border-border bg-surface p-2 text-sm">
        {!current ? <p className="p-2 text-secondary-text">正在搜索题材…</p> : <>
          {current.message && <div className="p-2"><p role="status" className="text-warning">{current.message}</p><button type="button" className="mt-2 text-cyan hover:underline" onClick={() => setAttempt(value => value + 1)}>重试题材搜索</button></div>}
          {!current.items.length && !current.message && <p className="p-2 text-secondary-text">没有匹配题材，请更换关键词。</p>}
          {current.items.map(topic => {
            const added = value.topics.some(item => sameTopic(item, topic));
            return <button key={`${topic.source}:${topic.name}`} type="button" disabled={added || value.topics.length >= 5} className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-foreground hover:bg-cyan/10 disabled:opacity-50" onClick={() => { onChange({ ...value, topics: [...value.topics, topic] }); onQueryChange(''); }}>
              <span>{topic.name} <span className="text-xs text-secondary-text">{topic.source === 'industry' ? '行业' : '概念'}</span></span><span className="text-cyan">{added ? '已加入' : '加入筛选'}</span>
            </button>;
          })}
        </>}
      </div>}
      <p className="text-xs text-secondary-text">未选择题材时筛选全部股票；指定题材后，仅纳入能够核验归属的成分股。</p>
      <p className="text-xs text-secondary-text">题材数据优先使用东方财富；概念题材可用同花顺备用。备用源当前仅覆盖成分股表格当前页，结果会注明覆盖不完整。</p>
    </fieldset>
  </div>;
}
