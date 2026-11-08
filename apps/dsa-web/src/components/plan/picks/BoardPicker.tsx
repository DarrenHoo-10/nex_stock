import type React from 'react';
import { useEffect, useId, useMemo, useState } from 'react';
import { getBoardAnomalies, getBoardRecommendations } from '../../../api/boards';
import { toApiErrorMessage } from '../../../api/error';
import { useBoardFollowStore } from '../../../stores/boardFollowStore';
import type { BoardState, BoardType, RecommendationResponse } from '../../../types/boards';

/** A board chosen as screening scope (sent as a screening topic). */
export interface PickedBoard {
  id: string;
  name: string;
  boardType: BoardType;
}

export const MAX_PICKED_BOARDS = 5;
const MAX_LISTED = 40;

const STATE_LABEL: Partial<Record<BoardState, string>> = {
  first_launch: '首次启动',
  strengthening: '走强',
  divergence: '分歧',
  ebbing: '退潮',
  abnormal_drop: '异常下跌',
};

const TYPE_LABEL: Record<BoardType, string> = { industry: '行业', concept: '概念' };

/** One selectable board in the picker. */
interface Option {
  id: string;
  name: string;
  boardType: BoardType;
  state: BoardState;
  /** Why it is recommended (recommendation section only). */
  why?: string;
}

type Catalog =
  | { status: 'idle' | 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; signalled: Option[]; all: Option[]; recommended: Option[]; recSource: RecommendationResponse['source'] };

interface BoardPickerProps {
  value: PickedBoard[];
  onChange: (next: PickedBoard[]) => void;
  disabled?: boolean;
}

function toOption(row: { id: string; name: string; boardType: BoardType; state: BoardState }): Option {
  return { id: row.id, name: row.name, boardType: row.boardType, state: row.state };
}

/**
 * Optional board scope for a screening task: chosen boards as removable chips plus an inline picker. Without a
 * search it lists the boards recommended after the latest board run, the followed boards and today's signalled
 * boards; a search covers every board of the latest run. Empty selection = whole market.
 */
export const BoardPicker: React.FC<BoardPickerProps> = ({ value, onChange, disabled }) => {
  const idPrefix = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [catalog, setCatalog] = useState<Catalog>({ status: 'idle' });
  const [reloadKey, setReloadKey] = useState(0);
  const followed = useBoardFollowStore((state) => state.boards);
  const loadFollows = useBoardFollowStore((state) => state.load);

  useEffect(() => {
    if (!open || catalog.status === 'ready' || catalog.status === 'loading') return undefined;
    let cancelled = false;
    void loadFollows();
    // Signalled boards (default list) plus the remaining boards (state=none) make up the full catalog; the
    // recommendations are optional (a failure only hides that section).
    void Promise.all([
      getBoardAnomalies(),
      getBoardAnomalies({ state: 'none' }),
      getBoardRecommendations().catch(() => null),
    ])
      .then(([signalled, rest, recs]) => {
        if (cancelled) return;
        const all = signalled.boards.map(toOption);
        const seen = new Set(all.map((row) => row.id));
        for (const row of rest.boards) {
          if (!seen.has(row.id)) {
            seen.add(row.id);
            all.push(toOption(row));
          }
        }
        setCatalog({
          status: 'ready',
          signalled: signalled.boards.map(toOption),
          all,
          recommended: (recs?.items ?? []).map((item) => ({ ...toOption(item.board), why: item.reason })),
          recSource: recs?.source ?? null,
        });
      })
      .catch((err) => {
        if (!cancelled) setCatalog({ status: 'error', message: toApiErrorMessage(err, '板块列表读取失败') });
      });
    setCatalog({ status: 'loading' });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- load once per opening / retry
  }, [open, reloadKey]);

  const chosen = useMemo(() => new Set(value.map((board) => board.id)), [value]);
  const full = value.length >= MAX_PICKED_BOARDS;

  const sections = useMemo(() => {
    if (catalog.status !== 'ready') return [];
    const needle = query.trim().toLowerCase();
    if (needle) {
      return [
        {
          key: 'found',
          title: '搜索结果',
          rows: catalog.all.filter((row) => row.name.toLowerCase().includes(needle)).slice(0, MAX_LISTED),
        },
      ];
    }
    const followRows: Option[] = followed.map((board) => ({
      id: board.id,
      name: board.name,
      boardType: board.boardType,
      state: board.today?.state ?? 'none',
    }));
    const shown = new Set([...catalog.recommended, ...followRows].map((row) => row.id));
    return [
      { key: 'rec', title: catalog.recSource === 'rule' ? '推荐板块（按规则排序）' : '模型推荐', rows: catalog.recommended },
      { key: 'follow', title: '我关注的', rows: followRows },
      { key: 'signal', title: '今日异动', rows: catalog.signalled.filter((row) => !shown.has(row.id)).slice(0, MAX_LISTED) },
    ].filter((section) => section.rows.length > 0);
  }, [catalog, query, followed]);

  const toggle = (row: Option) => {
    if (chosen.has(row.id)) {
      onChange(value.filter((board) => board.id !== row.id));
    } else if (!full) {
      onChange([...value, { id: row.id, name: row.name, boardType: row.boardType }]);
    }
  };

  const addRecommended = () => {
    if (catalog.status !== 'ready') return;
    const next = [...value];
    for (const row of catalog.recommended) {
      if (next.length >= MAX_PICKED_BOARDS) break;
      if (!next.some((board) => board.id === row.id)) next.push({ id: row.id, name: row.name, boardType: row.boardType });
    }
    onChange(next);
  };

  return (
    <div className="board-picker">
      <div className="bp-head">
        <span className="bp-label" id={`${idPrefix}-label`}>
          板块
        </span>
        <ul className="bp-chips" aria-labelledby={`${idPrefix}-label`}>
          {value.length === 0 ? <li className="bp-all">全市场</li> : null}
          {value.map((board) => (
            <li key={board.id} className="bp-chip">
              <span>{board.name}</span>
              <button
                type="button"
                aria-label={`移除 ${board.name}`}
                disabled={disabled}
                onClick={() => onChange(value.filter((item) => item.id !== board.id))}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
        <button
          type="button"
          className="btn quiet sm"
          aria-expanded={open}
          aria-controls={`${idPrefix}-panel`}
          disabled={disabled}
          onClick={() => setOpen((current) => !current)}
        >
          {open ? '收起' : value.length ? '更改板块' : '选择板块'}
        </button>
      </div>

      {open ? (
        <div className="bp-panel" id={`${idPrefix}-panel`}>
          <div className="bp-search">
            <label className="vh" htmlFor={`${idPrefix}-search`}>
              搜索板块
            </label>
            <input
              id={`${idPrefix}-search`}
              type="search"
              placeholder="搜索全部板块，例如 半导体"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <span className="bp-hint">{full ? `最多选 ${MAX_PICKED_BOARDS} 个` : `可选 ${MAX_PICKED_BOARDS} 个`}</span>
            {catalog.status === 'ready' && catalog.recommended.length > 0 && !query.trim() ? (
              <button type="button" className="btn sm bp-add-rec" disabled={full} onClick={addRecommended}>
                选上推荐板块
              </button>
            ) : null}
          </div>
          {catalog.status === 'loading' || catalog.status === 'idle' ? (
            <p className="note" role="status">
              正在读取板块…
            </p>
          ) : null}
          {catalog.status === 'error' ? (
            <p className="note" role="alert">
              {catalog.message}
              <button type="button" className="btn quiet sm" onClick={() => setReloadKey((key) => key + 1)}>
                重试
              </button>
            </p>
          ) : null}
          {catalog.status === 'ready' && sections.length === 0 ? (
            <p className="note">{query.trim() ? '没有匹配的板块。' : '今天没有推荐或异动板块，可以直接搜索。'}</p>
          ) : null}
          {sections.map((section) => (
            <div className="bp-section" key={section.key}>
              <h4 id={`${idPrefix}-${section.key}`}>{section.title}</h4>
              <ul className="bp-list" aria-labelledby={`${idPrefix}-${section.key}`}>
                {section.rows.map((row) => {
                  const on = chosen.has(row.id);
                  return (
                    <li key={row.id}>
                      <button
                        type="button"
                        className={`bp-row${row.why ? ' has-why' : ''}`}
                        aria-pressed={on}
                        disabled={!on && full}
                        onClick={() => toggle(row)}
                      >
                        <span className="bp-name">{row.name}</span>
                        <span className="tag line">{TYPE_LABEL[row.boardType]}</span>
                        {STATE_LABEL[row.state] ? <span className="tag wait">{STATE_LABEL[row.state]}</span> : null}
                        <span className="bp-check" aria-hidden="true">
                          {on ? '已选' : ''}
                        </span>
                        {row.why ? <span className="bp-why">{row.why}</span> : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
};
