import type React from 'react';
import { useId, useMemo, useRef, useState } from 'react';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import { useStockIndex } from '../../../hooks/useStockIndex';
import type { UiTextKey } from '../../../i18n/uiText';
import { searchStocks } from '../../../utils/searchStocks';
import { PlanIcon } from '../PlanIcon';
import { targetFromRawCode, targetFromSuggestion } from './quoteTarget';
import type { QuoteTarget } from './quoteTarget';

const RESULT_LIMIT = 10;

export interface StockSearchBoxProps {
  onOpen: (target: QuoteTarget) => void;
  purpose?: 'quote' | 'watchlist';
  busy?: boolean;
}

/**
 * Stock and ETF search for the header and inline watchlist picker. The symbol index (`stocks.index.json`) is loaded on first focus;
 * matching reuses the autocomplete scoring (code, name, pinyin, alias).
 */
export const StockSearchBox: React.FC<StockSearchBoxProps> = ({ onOpen, purpose = 'quote', busy = false }) => {
  const adding = purpose === 'watchlist';
  const { t } = useUiLanguage();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [activated, setActivated] = useState(false);
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [composing, setComposing] = useState(false);
  const { index, loading, error } = useStockIndex(activated);

  const trimmed = query.trim();
  const suggestions = useMemo(
    () => (trimmed ? searchStocks(trimmed, index, { limit: RESULT_LIMIT }) : []),
    [index, trimmed],
  );
  const rawTarget = !suggestions.length && (!adding || /^\d{6}$/.test(trimmed))
    ? targetFromRawCode(trimmed)
    : null;
  const showList = open && trimmed.length > 0;
  const safeHighlight = Math.min(highlight, Math.max(0, suggestions.length - 1));

  const choose = (target: QuoteTarget) => {
    if (busy) return;
    onOpen(target);
    if (adding) return;
    setQuery('');
    setOpen(false);
    setHighlight(0);
    inputRef.current?.blur();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (busy || composing || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) {
      return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setOpen(true);
      if (suggestions.length) {
        const delta = event.key === 'ArrowDown' ? 1 : -1;
        setHighlight((safeHighlight + delta + suggestions.length) % suggestions.length);
      }
      return;
    }
    if (event.key === 'Enter' && (open || !adding) && !event.repeat) {
      event.preventDefault();
      const selected = suggestions[safeHighlight];
      if (selected) {
        choose(targetFromSuggestion(selected));
      } else if (rawTarget) {
        choose(rawTarget);
      }
      return;
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      setOpen(false);
    }
  };

  let status: string | null = null;
  if (showList && !suggestions.length) {
    if (loading) {
      status = t('quote.search.loading');
    } else if (error) {
      status = t('quote.search.loadFailed');
    } else if (!rawTarget) {
      status = t('quote.search.empty');
    }
  }

  return (
    <div className="qs" role="search">
      <label className="qs-field">
        <PlanIcon name="picks" size={16} />
        <input
          ref={inputRef}
          type="search"
          value={query}
          autoFocus={adding}
          readOnly={busy}
          placeholder={t(adding ? 'watchlist.search.placeholder' : 'quote.search.placeholder')}
          aria-label={t(adding ? 'watchlist.search.placeholder' : 'quote.search.label')}
          role="combobox"
          aria-expanded={showList}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={showList && (suggestions.length > 0 || rawTarget) ? `${listId}-${safeHighlight}` : undefined}
          autoComplete="off"
          spellCheck={false}
          onFocus={() => {
            setActivated(true);
            setOpen(true);
          }}
          onBlur={() => setOpen(false)}
          onChange={(event) => {
            setActivated(true);
            setQuery(event.target.value);
            setHighlight(0);
            setOpen(true);
          }}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          onKeyDown={handleKeyDown}
        />
      </label>
      {showList ? (
        <ul className="qs-list glass" id={listId} role="listbox" aria-label={t('quote.search.label')}>
          {suggestions.map((suggestion, position) => (
            <li
              key={suggestion.canonicalCode}
              id={`${listId}-${position}`}
              role="option"
              aria-selected={position === safeHighlight}
              className={position === safeHighlight ? 'on' : undefined}
              // mousedown keeps focus on the input until the choice is made
              onMouseDown={(event) => {
                event.preventDefault();
                choose(targetFromSuggestion(suggestion));
              }}
              onMouseEnter={() => setHighlight(position)}
            >
              <span className="qs-name">{suggestion.nameZh}</span>
              <span className="qs-code">{suggestion.displayCode}</span>
              <span className={`qs-tag${suggestion.assetType === 'etf' ? ' accent' : ''}`}>
                {suggestion.assetType === 'etf' || suggestion.assetType === 'index'
                  ? t(`quote.asset.${suggestion.assetType}` as UiTextKey)
                  : t(`quote.market.${suggestion.market}` as UiTextKey)}
              </span>
            </li>
          ))}
          {rawTarget ? (
            <li
              id={`${listId}-0`}
              role="option"
              aria-selected
              className="on"
              onMouseDown={(event) => {
                event.preventDefault();
                choose(rawTarget);
              }}
            >
              <span className="qs-name">{t(adding ? 'watchlist.search.addCode' : 'quote.search.openCode', { code: rawTarget.displayCode })}</span>
            </li>
          ) : null}
          {status ? (
            <li className="qs-status" role="presentation">
              {status}
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
};
