import { type ScreeningScope } from '../../api/screening';

import { BOARD_LABELS, sameTopic } from './scope';

type Props = {
  value: ScreeningScope;
  onChange: (scope: ScreeningScope) => void;
  query: string;
  onQueryChange: (query: string) => void;
  disabled: boolean;
};

export function ScopeControls({ value, onChange, query, onQueryChange, disabled }: Props) {
  const name = query.trim();
  const added = value.topics.some(topic => topic.name === name);
  const addTopic = () => {
    if (disabled || !name || added || value.topics.length >= 5) return;
    onChange({ ...value, topics: [...value.topics, { name, source: 'custom' }] });
    onQueryChange('');
  };
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
        <input aria-label="自定义题材" value={query} maxLength={80} onChange={event => onQueryChange(event.target.value)} placeholder="输入自定义题材，按回车添加" onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) { event.preventDefault(); addTopic(); } }} className="h-11 min-w-0 shrink-0 sm:flex-1 rounded-xl border border-border bg-surface px-3 text-sm text-foreground focus:border-cyan" />
        <button type="button" onClick={addTopic} disabled={!name || added || value.topics.length >= 5} className={`${chip} ${selected}`}>{added ? '已加入' : '添加题材'}</button>
        <select aria-label="题材匹配方式" value={value.topicMatch} onChange={event => onChange({ ...value, topicMatch: event.target.value as 'any' | 'all' })} className="h-11 rounded-xl border border-border bg-surface px-3 text-sm text-foreground">
          <option value="any">属于任一题材</option><option value="all">同时属于全部题材</option>
        </select>
      </div>
      <p className="text-xs text-secondary-text">点击上方推荐题材或直接输入添加，无需搜索。先按交易板块和策略收集候选，再由模型判断题材匹配度并排序。</p>
      <p className="text-xs text-secondary-text">题材判断是模型推断，会附匹配理由；证据不足的股票不入选。未选择题材时不限制题材。</p>
    </fieldset>
  </div>;
}
