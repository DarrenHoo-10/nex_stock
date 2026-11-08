import type React from 'react';
import { useEffect, useId, useRef, useState } from 'react';
import { LayoutGroup, motion } from 'motion/react';
import { Link } from 'react-router-dom';
import { PLAN_INSTANT, PLAN_SPRING } from '../motion/springs';
import { usePlanReducedMotion } from '../motion/usePlanMotion';
import type { PlanVerdict, StockPlanSummary } from '../../../types/dailyPlan';
import { changeClass, fmtPrice, fmtSignedPct, VERDICT_LABEL, VERDICT_TAG_CLASS } from './format';

export interface StockListProps {
  items: StockPlanSummary[];
  selectedCode: string | null;
  maxHoldings: number | null;
  onSelect: (code: string) => void;
  /** Resolves with an error sentence, or null when the stock was added. */
  onAddWatch: (code: string) => Promise<string | null>;
}

const CODE_PATTERN = /^\d{6}$/;

function stateTag(item: StockPlanSummary): { label: string; className: string } {
  // The backend may send a null verdict for a failed analysis.
  const verdict = item.verdict as PlanVerdict | null;
  if (verdict && (verdict === 'suspended' || item.status === 'ready')) {
    return { label: VERDICT_LABEL[verdict], className: VERDICT_TAG_CLASS[verdict] };
  }
  return item.status === 'failed'
    ? { label: '分析失败', className: 'warn' }
    : { label: '暂无计划', className: 'line' };
}

function oneLine(item: StockPlanSummary): string {
  if (item.status === 'failed') {
    return item.failureReason || '当日分析没有完成。';
  }
  if (item.status === 'missing') {
    return '还没有生成计划，下一次运行后出现。';
  }
  return item.headline;
}

const Row: React.FC<{
  item: StockPlanSummary;
  selected: boolean;
  reduced: boolean;
  onSelect: (code: string) => void;
}> = ({ item, selected, reduced, onSelect }) => {
  const tag = stateTag(item);
  const suspended = item.verdict === 'suspended';
  const hasPrice = item.close != null && !suspended;
  return (
    <li>
      <button
        type="button"
        className="row"
        data-code={item.code}
        aria-current={selected ? 'true' : undefined}
        onClick={() => onSelect(item.code)}
      >
        {selected ? (
          <motion.span className="ind" layoutId="mine-row-ind" transition={reduced ? PLAN_INSTANT : PLAN_SPRING} />
        ) : null}
        <span className="r-top">
          <span className="nm">{item.name}</span>
          <span className="cd">{item.code}</span>
          <span className={`tag ${tag.className}`}>{tag.label}</span>
        </span>
        <span className="r-px">
          {hasPrice ? (
            <>
              <b className="num">{fmtPrice(item.close)}</b>
              <span className={`num ${changeClass(item.changePct)}`}>{fmtSignedPct(item.changePct)}</span>
            </>
          ) : (
            <b className="flat">{suspended ? '停牌' : '—'}</b>
          )}
        </span>
        <span className="one">{oneLine(item)}</span>
      </button>
    </li>
  );
};

/** Held stocks first, then the watchlist; plus the inline 「加自选」 form. */
export const StockList: React.FC<StockListProps> = ({ items, selectedCode, maxHoldings, onSelect, onAddWatch }) => {
  const reduced = usePlanReducedMotion();
  const held = items.filter((item) => item.held);
  const watch = items.filter((item) => !item.held);
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const errorId = useId();
  const wasAdding = useRef(false);

  // Give focus back to 「加自选」 after the inline form closes.
  useEffect(() => {
    if (wasAdding.current && !adding) {
      triggerRef.current?.focus();
    }
    wasAdding.current = adding;
  }, [adding]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const value = code.trim();
    if (!CODE_PATTERN.test(value)) {
      setError('请输入 6 位股票代码。');
      inputRef.current?.focus();
      return;
    }
    setBusy(true);
    const result = await onAddWatch(value);
    setBusy(false);
    if (result) {
      setError(result);
      inputRef.current?.focus();
      return;
    }
    setError(null);
    setCode('');
    setAdding(false);
  };

  return (
    <LayoutGroup id="mine-list">
      {items.length === 0 ? (
        <div className="list-empty">
          <h3>还没有自选和持仓</h3>
          <p>先加几只自选，或去选股挑一些。</p>
          <Link className="btn sm" to="/picks">
            去选股
          </Link>
        </div>
      ) : null}
      {held.length > 0 ? (
        <>
          <p className="group-label">
            <span>持仓</span>
            <span>{maxHoldings ? `${held.length}/${maxHoldings} 只` : `${held.length} 只`}</span>
          </p>
          <ul className="rows" aria-label="持仓">
            {held.map((item) => (
              <Row key={item.code} item={item} selected={item.code === selectedCode} reduced={reduced} onSelect={onSelect} />
            ))}
          </ul>
        </>
      ) : null}
      <p className="group-label">
        <span>自选</span>
        <span>{watch.length} 只，可买的在前</span>
      </p>
      {watch.length > 0 ? (
        <ul className="rows" aria-label="自选">
          {watch.map((item) => (
            <Row key={item.code} item={item} selected={item.code === selectedCode} reduced={reduced} onSelect={onSelect} />
          ))}
        </ul>
      ) : (
        <p className="empty-note">自选里还没有股票。</p>
      )}
      <div className="add-row">
        {adding ? (
          <form className="add-watch" noValidate onSubmit={submit}>
            <label className="vh" htmlFor={`${errorId}-input`}>
              股票代码
            </label>
            <input
              id={`${errorId}-input`}
              ref={inputRef}
              className="add-input"
              inputMode="numeric"
              maxLength={6}
              autoComplete="off"
              placeholder="6 位代码，如 000988"
              value={code}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? errorId : undefined}
              onChange={(event) => setCode(event.target.value.replace(/\D/g, ''))}
              autoFocus
            />
            <button type="submit" className="btn sm" disabled={busy}>
              {busy ? '添加中…' : '添加'}
            </button>
            <button
              type="button"
              className="btn quiet sm"
              onClick={() => {
                setAdding(false);
                setError(null);
                setCode('');
              }}
            >
              取消
            </button>
            {error ? (
              <p className="err" id={errorId} role="alert">
                {error}
              </p>
            ) : null}
          </form>
        ) : (
          <button ref={triggerRef} type="button" className="btn quiet sm" onClick={() => setAdding(true)}>
            + 加自选
          </button>
        )}
      </div>
    </LayoutGroup>
  );
};
