import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { getParsedApiError } from '../../../api/error';
import { stocksApi } from '../../../api/stocks';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../../i18n/uiText';
import { GlassDialog } from '../GlassDialog';
import { CHART_COLORS } from './chartTheme';
import { buildChipGrid, computeChipDistribution } from './chipDistribution';
import { IntradayPanel } from './IntradayPanel';
import { KlineChart } from './KlineChart';
import {
  PRICE_MA_WINDOWS,
  aggregateBars,
  applyTurnover,
  formatVolume,
  movingAverage,
  priceDigits,
} from './klineMath';
import type { DailyBar, KlinePeriod } from './klineMath';
import type { QuoteTarget } from './quoteTarget';
import '../../../styles/plan-quote.css';

/** Daily history to request: enough for monthly candles and a warmed-up chip distribution. */
export const QUOTE_HISTORY_DAYS = 750;

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; bars: DailyBar[]; name: string | null };

/** The quote only supplies float shares; it loads in parallel and never blocks the chart. */
type FloatState = { status: 'loading' } | { status: 'done'; floatShares: number | null };

const PERIODS: Array<{ id: KlinePeriod; key: UiTextKey }> = [
  { id: 'daily', key: 'quote.period.daily' },
  { id: 'weekly', key: 'quote.period.weekly' },
  { id: 'monthly', key: 'quote.period.monthly' },
];

function toDailyBars(data: Awaited<ReturnType<typeof stocksApi.getHistory>>['data']): DailyBar[] {
  return data
    .filter((bar) => Number.isFinite(bar.close) && bar.close > 0 && bar.high > 0)
    .map((bar) => ({
      date: bar.date,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume ?? null,
      amount: bar.amount ?? null,
      changePercent: bar.changePercent ?? null,
      turnoverRate: null,
    }))
    .sort((a, b) => a.date.localeCompare(b.date));
}

function signClass(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) {
    return '';
  }
  return value > 0 ? 'up' : 'down';
}

function formatPercent(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '--';
  }
  return `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

export interface StockChartDialogProps {
  target: QuoteTarget | null;
  onClose: () => void;
  /** Adds the symbol to the watchlist; omitted hides the button. */
  onAddWatch?: (target: QuoteTarget) => void;
}

export interface StockChartBodyProps {
  target: QuoteTarget;
  onAddWatch?: (target: QuoteTarget) => void;
  /** Drop the name / code / last-price block (the host page already shows the stock). */
  hideTitle?: boolean;
}

/** K-line, stats, MAs, volume and chip distribution of one symbol; shared by the dialog and inline hosts. */
export const StockChartBody: React.FC<StockChartBodyProps> = ({ target, onAddWatch, hideTitle = false }) => {
  const { t } = useUiLanguage();
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [floatState, setFloatState] = useState<FloatState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);
  const [period, setPeriod] = useState<KlinePeriod>('daily');
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  /** The trading day whose one-minute chart is open over the candles (double-click on a daily candle). */
  const [intradayDay, setIntradayDay] = useState<{ date: string; prevClose: number } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    stocksApi
      .getHistory(target.code, { days: QUOTE_HISTORY_DAYS, signal: controller.signal })
      .then((response) => {
        setState({ status: 'ready', bars: toDailyBars(response.data ?? []), name: response.stockName ?? null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setState({ status: 'error', message: getParsedApiError(error).message || t('quote.error') });
      });
    return () => controller.abort();
  }, [target.code, reloadKey, t]);

  useEffect(() => {
    const controller = new AbortController();
    stocksApi
      .getQuote(target.code, { signal: controller.signal })
      .then((quote) => setFloatState({ status: 'done', floatShares: quote.floatShares ?? null }))
      .catch(() => {
        // A failed quote only downgrades the chip turnover to the volume-based estimate.
        if (!controller.signal.aborted) {
          setFloatState({ status: 'done', floatShares: null });
        }
      });
    return () => controller.abort();
  }, [target.code, reloadKey]);

  const rawDaily = useMemo(() => (state.status === 'ready' ? state.bars : []), [state]);
  const turnover = useMemo(
    () => (floatState.status === 'done' ? applyTurnover(rawDaily, floatState.floatShares) : null),
    [floatState, rawDaily],
  );
  const daily = turnover ? turnover.bars : rawDaily;
  const bars = useMemo(() => aggregateBars(daily, period), [daily, period]);
  const priceMa = useMemo(
    () => PRICE_MA_WINDOWS.map((window) => movingAverage(bars.map((bar) => bar.close), window)),
    [bars],
  );
  const grid = useMemo(() => buildChipGrid(daily), [daily]);
  const hasTurnover = turnover !== null && turnover.basis !== 'none';

  const activeIndex = hoverIndex !== null && hoverIndex < bars.length ? hoverIndex : bars.length - 1;
  const active = activeIndex >= 0 ? bars[activeIndex] : null;
  const chip = useMemo(
    () => (active && grid && hasTurnover ? computeChipDistribution(daily, active.dailyIndex, grid) : null),
    [active, daily, grid, hasTurnover],
  );
  const handleHover = useCallback((index: number | null) => setHoverIndex(index), []);

  // The header always shows the latest trading day, whatever the candle period.
  const last = daily.length ? daily[daily.length - 1] : null;
  const digits = priceDigits(last?.close ?? 10);
  const name = target.name || (state.status === 'ready' ? state.name : null) || target.code;

  const badges = (
    <span className="qc-badges">
      <span className="qc-tag">{t(`quote.market.${target.market}` as UiTextKey)}</span>
      {target.assetType === 'etf' || target.assetType === 'index' ? (
        <span className="qc-tag accent">{t(`quote.asset.${target.assetType}` as UiTextKey)}</span>
      ) : null}
    </span>
  );

  if (state.status === 'loading') {
    return (
      <div className="qc-state" aria-busy="true">
        <p>{t('quote.loading')}</p>
      </div>
    );
  }
  if (state.status === 'error') {
    return (
      <div className="qc-state" role="alert">
        <p>
          {t('quote.error')}：{state.message}
        </p>
        <button type="button" className="btn sm" onClick={() => {
            setState({ status: 'loading' });
            setFloatState({ status: 'loading' });
            setReloadKey((key) => key + 1);
          }}>
          {t('common.retry')}
        </button>
      </div>
    );
  }
  if (!bars.length || !active || !last) {
    return (
      <div className="qc-state" role="status">
        <p>{t('quote.empty')}</p>
      </div>
    );
  }

  const stats: Array<{ label: string; value: string; tone?: string }> = [
    { label: t('quote.open'), value: active.open.toFixed(digits), tone: signClass(active.open - (bars[activeIndex - 1]?.close ?? active.open)) },
    { label: t('quote.high'), value: active.high.toFixed(digits), tone: signClass(active.high - (bars[activeIndex - 1]?.close ?? active.high)) },
    { label: t('quote.low'), value: active.low.toFixed(digits), tone: signClass(active.low - (bars[activeIndex - 1]?.close ?? active.low)) },
    { label: t('quote.close'), value: active.close.toFixed(digits), tone: signClass(active.changePercent) },
    { label: t('quote.change'), value: formatPercent(active.changePercent), tone: signClass(active.changePercent) },
    { label: t('quote.volume'), value: formatVolume(active.volume) },
    { label: t('quote.amount'), value: formatVolume(active.amount) },
    {
      label: t('quote.turnover'),
      value: active.turnoverRate !== null ? `${active.turnoverRate.toFixed(2)}%` : '--',
    },
  ];

  // Minute bars exist for A-share stocks / ETFs only, and a candle maps to one session only on the daily period.
  const intradaySupported = (target.market === 'CN' || target.market === 'BSE') && target.assetType !== 'index';
  const openIntraday = (index: number) => {
    const bar = bars[index];
    if (!intradaySupported || period !== 'daily' || !bar) {
      return;
    }
    setIntradayDay({ date: bar.date.slice(0, 10), prevClose: index > 0 ? bars[index - 1].close : bar.open });
  };

  return (
    <div className="qc">
      <div className={hideTitle ? 'qc-top compact' : 'qc-top'}>
        {hideTitle ? null : (
          <div className="qc-id">
            <div className="qc-name">
              {!target.name && name !== target.code ? <b>{name}</b> : null}
              <span className="qc-code">{target.displayCode}</span>
              {badges}
            </div>
            <div className="qc-price">
              <span className={`qc-last ${signClass(last.changePercent)}`}>{last.close.toFixed(digits)}</span>
              <span className={signClass(last.changePercent)}>{formatPercent(last.changePercent)}</span>
              <span className="qc-date">{last.date.slice(0, 10)}</span>
            </div>
          </div>
        )}
        <div className="qc-actions">
          <div className="seg" role="radiogroup" aria-label={t('quote.periodGroup')}>
            {PERIODS.map((item) => (
              <button
                key={item.id}
                type="button"
                role="radio"
                aria-checked={period === item.id}
                className={period === item.id ? 'on' : undefined}
                onClick={() => {
                  setPeriod(item.id);
                  setHoverIndex(null);
                  setIntradayDay(null);
                }}
              >
                {t(item.key)}
              </button>
            ))}
          </div>
          {onAddWatch ? (
            <button type="button" className="btn sm" onClick={() => onAddWatch(target)}>
              {t('quote.addWatch')}
            </button>
          ) : null}
        </div>
      </div>

      <dl className="qc-stats">
        <div className="qc-stat date">
          <dd>{active.date.slice(0, 10)}</dd>
        </div>
        {stats.map((item) => (
          <div key={item.label} className="qc-stat">
            <dt>{item.label}</dt>
            <dd className={item.tone}>{item.value}</dd>
          </div>
        ))}
      </dl>

      <div className="qc-ma" aria-hidden="true">
        {PRICE_MA_WINDOWS.map((window, k) => {
          const value = priceMa[k][activeIndex];
          return (
            <span key={window} style={{ color: CHART_COLORS.ma[k] }}>
              MA{window} {value === null ? '--' : value.toFixed(digits)}
            </span>
          );
        })}
      </div>

      <div className="qc-chart">
        <KlineChart
          key={period}
          bars={bars}
          period={period}
          digits={digits}
          chip={chip}
          onHoverChange={handleHover}
          onBarDoubleClick={intradaySupported ? openIntraday : undefined}
          ariaLabel={t('quote.chartLabel', { name })}
        />
        {intradayDay ? (
          <IntradayPanel
            key={intradayDay.date}
            code={target.code}
            name={name}
            date={intradayDay.date}
            prevClose={intradayDay.prevClose}
            digits={digits}
            onClose={() => setIntradayDay(null)}
          />
        ) : null}
        <section className="qc-chip" aria-label={t('quote.chip.title')}>
          <h3>
            {t('quote.chip.title')}
            <span>{daily[active.dailyIndex]?.date.slice(0, 10)}</span>
          </h3>
          {chip ? (
            <dl>
              <div>
                <dt>{t('quote.chip.profit')}</dt>
                <dd className="up">{(chip.profitRatio * 100).toFixed(1)}%</dd>
              </div>
              <div>
                <dt>{t('quote.chip.avgCost')}</dt>
                <dd style={{ color: CHART_COLORS.avgCost }}>{chip.avgCost.toFixed(digits)}</dd>
              </div>
              <div>
                <dt>{t('quote.chip.range90')}</dt>
                <dd>
                  {chip.range90.low.toFixed(digits)}–{chip.range90.high.toFixed(digits)}
                  <small>
                    {t('quote.chip.concentration')} {(chip.range90.concentration * 100).toFixed(1)}%
                  </small>
                </dd>
              </div>
              <div>
                <dt>{t('quote.chip.range70')}</dt>
                <dd>
                  {chip.range70.low.toFixed(digits)}–{chip.range70.high.toFixed(digits)}
                  <small>
                    {t('quote.chip.concentration')} {(chip.range70.concentration * 100).toFixed(1)}%
                  </small>
                </dd>
              </div>
            </dl>
          ) : (
            <p className="qc-note">{t(turnover ? 'quote.chip.unavailable' : 'quote.chip.loading')}</p>
          )}
        </section>
      </div>

      <div className="qc-foot">
        <span>{t('quote.controls')}</span>
        {intradaySupported && period === 'daily' ? <span>{t('quote.intraday.hint')}</span> : null}
        {chip ? <span>{t('quote.chip.legend')}</span> : null}
        {turnover?.basis === 'estimated' ? <span className="warn">{t('quote.chip.estimated')}</span> : null}
        <span>{t('quote.source')}</span>
      </div>
    </div>
  );
};

/** Glass dialog with the K-line, volume and chip distribution of one stock / ETF. */
export const StockChartDialog: React.FC<StockChartDialogProps> = ({ target, onClose, onAddWatch }) => {
  const { t } = useUiLanguage();
  return (
    <GlassDialog
      open={target !== null}
      onClose={onClose}
      width={1240}
      title={target ? `${target.name || target.displayCode}` : t('quote.search.label')}
    >
      {target ? <StockChartBody key={target.code} target={target} onAddWatch={onAddWatch} /> : null}
    </GlassDialog>
  );
};
