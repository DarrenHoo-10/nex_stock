import type React from 'react';
import { useId, useState } from 'react';
import { isPlanServiceUnavailable, previewSizing } from '../../../api/dailyPlan';
import { getParsedApiError } from '../../../api/error';
import type { Horizon, Sizing } from '../../../types/dailyPlan';
import { formatNumber, parseNumber } from './format';

type TryState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'done'; sizing: Sizing; code: string }
  | { status: 'error'; message: string };

interface SizingTryProps {
  /** Unsaved rule edits: the preview uses the saved rules, so it waits for the save. */
  dirty: boolean;
  /** Code prefilled from the first holding, when the account has one. */
  initialCode?: string;
}

/**
 * Optional try-out: asks POST /plan/sizing how many shares the *saved* rules and the current account give for a
 * code and a buy price. The formula lives in the backend; this only shows its answer (or says it is unavailable).
 */
export const SizingTry: React.FC<SizingTryProps> = ({ dirty, initialCode = '' }) => {
  const [codeEdit, setCodeEdit] = useState<string | null>(null);
  const [price, setPrice] = useState('');
  const [horizon, setHorizon] = useState<Horizon>('short');
  const [state, setState] = useState<TryState>({ status: 'idle' });
  const idPrefix = useId();

  const code = codeEdit ?? initialCode;

  const priceValue = parseNumber(price);
  const codeValid = /^\d{6}$/.test(code.trim());
  const canRun = codeValid && priceValue !== null && priceValue > 0 && !dirty && state.status !== 'loading';

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canRun || priceValue === null) {
      return;
    }
    setState({ status: 'loading' });
    try {
      const sizing = await previewSizing({ code: code.trim(), price: priceValue, horizon });
      setState({ status: 'done', sizing, code: code.trim() });
    } catch (error) {
      setState({
        status: 'error',
        message: isPlanServiceUnavailable(error) ? '次日计划服务未就绪，暂时不能试算。' : getParsedApiError(error).message,
      });
    }
  };

  return (
    <section className="pref-try" aria-labelledby={`${idPrefix}-title`}>
      <h3 id={`${idPrefix}-title`}>试算股数</h3>
      <p className="note">按已保存的规则和当前账户，看看某个价格能买多少。结果来自后端计算，这里不重新算。</p>
      <form className="form-grid pref-try-form" onSubmit={run} noValidate>
        <label className="f" htmlFor={`${idPrefix}-code`}>
          股票代码
          <input
            id={`${idPrefix}-code`}
            inputMode="numeric"
            maxLength={6}
            value={code}
            placeholder="例如 000988"
            onChange={(event) => setCodeEdit(event.target.value)}
          />
        </label>
        <label className="f" htmlFor={`${idPrefix}-price`}>
          买入价（元）
          <input
            id={`${idPrefix}-price`}
            inputMode="decimal"
            value={price}
            placeholder="例如 47.00"
            onChange={(event) => setPrice(event.target.value)}
          />
        </label>
        <div className="f-cell">
          <label className="f-label" htmlFor={`${idPrefix}-horizon`}>
            周期
          </label>
          <select id={`${idPrefix}-horizon`} value={horizon} onChange={(event) => setHorizon(event.target.value as Horizon)}>
            <option value="short">短线</option>
            <option value="swing">波段</option>
          </select>
        </div>
        <button type="submit" className="btn" disabled={!canRun}>
          {state.status === 'loading' ? '正在试算…' : '试算'}
        </button>
      </form>
      {dirty ? <p className="note">规则有未保存的修改，先保存再试算。</p> : null}
      <div aria-live="polite" className="pref-try-result">
        {state.status === 'done' ? (
          state.sizing.status === 'ok' ? (
            <div className="inset" data-testid="sizing-result">
              <p>
                {formatNumber(state.sizing.price, 2)} 元买入 {state.code} → 约{' '}
                <b className="num">{formatNumber(state.sizing.shares)}</b> 股（
                {formatNumber(state.sizing.amount)} 元，占总资金 {formatNumber(state.sizing.capitalPct, 1)}%）。止损价{' '}
                {formatNumber(state.sizing.stop.price, 2)} 元，按止损价最多亏 {formatNumber(state.sizing.maxLoss)} 元。
              </p>
              {state.sizing.steps.length > 0 ? (
                <details className="calc">
                  <summary>怎么算的</summary>
                  <ol>
                    {state.sizing.steps.map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                </details>
              ) : null}
            </div>
          ) : (
            <p className="notice pref-notice" data-testid="sizing-result">
              {state.sizing.message || '按当前规则不建议买入。'}
            </p>
          )
        ) : null}
        {state.status === 'error' ? (
          <p className="err" role="alert">
            {state.message}
          </p>
        ) : null}
      </div>
    </section>
  );
};
