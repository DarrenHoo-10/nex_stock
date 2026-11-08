import type React from 'react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useStockIndex } from '../../hooks/useStockIndex';
import type { StockSuggestion } from '../../types/stockIndex';
import { searchStocks } from '../../utils/searchStocks';
import { watchlistCodeOf } from '../../utils/watchlistCode';

const MAX_RESULTS = 8;

const MARKET_LABEL: Record<string, string> = {
  CN: 'A股',
  BSE: '北交所',
  HK: '港股',
  US: '美股',
  JP: '日股',
  KR: '韩股',
  INDEX: '指数',
  ETF: 'ETF',
};

interface PlanStockSearchProps {
  /** Codes already in the watchlist (backend form). */
  watchlist: ReadonlySet<string>;
  /** Add one stock; resolves with an error message, or null on success. */
  onAdd: (code: string, name: string) => Promise<string | null>;
}

/**
 * Header search box of 我的股票: search by code, name or pinyin (the bundled stock index, loaded on first focus)
 * and add the stock to the watchlist. Arrow keys move through the results, Enter adds, Escape closes.
 */
export const PlanStockSearch: React.FC<PlanStockSearchProps> = ({ watchlist, onAdd }) => {
  const idPrefix = useId();
  const listId = `${idPrefix}-list`;
  const [touched, setTouched] = useState(false);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const stockIndex = useStockIndex(touched);

  const results = useMemo(
    () => (query.trim() && stockIndex.loaded ? searchStocks(query, stockIndex.index, { limit: MAX_RESULTS }) : []),
    [query, stockIndex.index, stockIndex.loaded],
  );
  const showList = open && query.trim() !== '';
  const activeIndex = Math.min(active, Math.max(results.length - 1, 0));

  // Clicking outside closes the list.
  useEffect(() => {
    if (!open) return undefined;
    const handle = (event: PointerEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', handle);
    return () => document.removeEventListener('pointerdown', handle);
  }, [open]);

  const add = async (suggestion: StockSuggestion) => {
    const code = watchlistCodeOf(suggestion);
    if (watchlist.has(code) || adding) return;
    setAdding(code);
    setError(null);
    const failure = await onAdd(suggestion.canonicalCode, suggestion.nameZh);
    setAdding(null);
    if (failure) {
      setError(failure);
      return;
    }
    setQuery('');
    setOpen(false);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setOpen(true);
      setActive((index) => (results.length ? (index + 1) % results.length : 0));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActive((index) => (results.length ? (index - 1 + results.length) % results.length : 0));
    } else if (event.key === 'Enter') {
      if (showList && results[activeIndex]) {
        event.preventDefault();
        void add(results[activeIndex]);
      }
    } else if (event.key === 'Escape') {
      setOpen(false);
    }
  };

  let status: string | null = null;
  if (showList) {
    if (stockIndex.loading) status = '正在载入股票列表…';
    else if (stockIndex.error && !stockIndex.index.length) status = '股票列表载入失败，刷新页面后再试';
    else if (!results.length) status = '没有找到匹配的股票';
  }

  return (
    <div className="stock-search" ref={boxRef}>
      <label className="vh" htmlFor={`${idPrefix}-input`}>
        搜索股票加入自选
      </label>
      <input
        id={`${idPrefix}-input`}
        type="search"
        role="combobox"
        autoComplete="off"
        placeholder="搜索股票加入自选：代码 / 名称 / 拼音"
        aria-expanded={showList && results.length > 0}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList && results.length ? `${idPrefix}-opt-${activeIndex}` : undefined}
        value={query}
        onFocus={() => {
          setTouched(true);
          setOpen(true);
        }}
        onChange={(event) => {
          setQuery(event.target.value);
          setActive(0);
          setOpen(true);
          setError(null);
        }}
        onKeyDown={handleKeyDown}
      />
      {showList ? (
        <div className="stock-search-pop">
          {status ? (
            <p className="stock-search-status" role="status">
              {status}
            </p>
          ) : null}
          <ul id={listId} role="listbox" aria-label="搜索结果">
            {results.map((suggestion, index) => {
              const code = watchlistCodeOf(suggestion);
              const inList = watchlist.has(code);
              return (
                <li
                  key={suggestion.canonicalCode}
                  id={`${idPrefix}-opt-${index}`}
                  role="option"
                  aria-selected={index === activeIndex}
                  aria-disabled={inList || undefined}
                  className={`stock-option${index === activeIndex ? ' active' : ''}`}
                  onPointerEnter={() => setActive(index)}
                  onClick={() => void add(suggestion)}
                >
                  <span className="so-name">{suggestion.nameZh}</span>
                  <span className="so-code num">{suggestion.displayCode}</span>
                  <span className="tag line">{MARKET_LABEL[suggestion.market] ?? suggestion.market}</span>
                  <span className="so-act">{inList ? '已在自选' : adding === code ? '加入中…' : '+ 自选'}</span>
                </li>
              );
            })}
          </ul>
          {error ? (
            <p className="stock-search-status err" role="alert">
              {error}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};
