import type React from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { getParsedApiError } from '../../../api/error';
import { stocksApi } from '../../../api/stocks';
import type { OpeningAuction, StockIntradayBar } from '../../../api/stocks';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import { svgPointer } from './svgPointer';
import { CHART_COLORS } from './chartTheme';
import { SESSION_SLOTS, buildIntradaySeries, minuteSlot } from './intradayMath';
import { formatVolume } from './klineMath';

const H = 400;
const LEFT = 58;
const RIGHT = 58;
const TOP = 10;
const PRICE_H = 244;
const VOL_TOP = TOP + PRICE_H + 16;
const VOL_H = 96;
const AXIS_Y = VOL_TOP + VOL_H + 16;
const NO_AUCTION: OpeningAuction['points'] = [];
const auctionFraction = (time: string) => {
  const [, minute, second] = time.split(':').map(Number);
  return ((minute - 15) * 60 + (second || 0)) / (15 * 60);
};
const AXIS_TIMES = ['09:30', '10:30', '11:30', '14:00', '15:00'];

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; bars: StockIntradayBar[]; stale: boolean };

function signClass(value: number): string {
  if (value === 0) {
    return '';
  }
  return value > 0 ? 'up' : 'down';
}

export interface IntradayPanelProps {
  code: string;
  name: string;
  /** Trading day, YYYY-MM-DD. */
  date: string;
  /** Previous trading day's close: the baseline of the price axis and of the change percentages. */
  prevClose: number;
  digits: number;
  onClose: () => void;
}

/** One-minute price / average / volume of one trading day, laid over the candle chart it was opened from. */
export const IntradayPanel: React.FC<IntradayPanelProps> = ({ code, name, date, prevClose, digits, onClose }) => {
  const { t } = useUiLanguage();
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);
  const [auction, setAuction] = useState<OpeningAuction | null>(null);
  const [hover, setHover] = useState<{ session: 'auction' | 'regular'; index: number; x: number } | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    stocksApi
      .getIntraday(code, date, { signal: controller.signal })
      .then((response) => {
        if (controller.signal.aborted) return;
        if (response.interval !== '1min') {
          setState({ status: 'error', message: t('quote.intraday.intervalUnavailable') });
          return;
        }
        setState({ status: 'ready', bars: response.data ?? [], stale: response.isStale });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ status: 'error', message: getParsedApiError(error).message || t('quote.intraday.error') });
        }
      });
    return () => controller.abort();
  }, [code, date, reloadKey, t]);

  useEffect(() => {
    const controller = new AbortController();
    stocksApi.getOpeningAuction(code, date, controller.signal)
      .then((result) => { if (!controller.signal.aborted) setAuction(result); })
      .catch(() => { if (!controller.signal.aborted) setAuction({ source: null, points: [] }); });
    return () => controller.abort();
  }, [code, date]);

  useEffect(() => {
    rootRef.current?.focus();
  }, []);

  const [width, setWidth] = useState(960);
  useEffect(() => {
    if (!rootRef.current || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(360, Math.min(960, entry.contentRect.width))));
    observer.observe(rootRef.current);
    return () => observer.disconnect();
  }, []);
  const openX = LEFT + (width - LEFT - RIGHT) * 0.22;
  const regularW = width - RIGHT - openX;
  const auctionX = (time: string) => LEFT + auctionFraction(time) * (openX - LEFT);

  const bars = useMemo(() => (state.status === 'ready' ? state.bars : []), [state]);
  const auctionPoints = auction?.points ?? NO_AUCTION;
  const series = useMemo(() => buildIntradaySeries(bars, prevClose, auctionPoints), [bars, prevClose, auctionPoints]);
  const slots = SESSION_SLOTS;

  const xOf = (index: number) => openX + ((index + 0.5) / slots) * regularW;
  const yOf = (price: number) => TOP + ((series.max - price) / (series.max - series.min)) * PRICE_H;
  const barWidth = Math.max(1.5, (regularW / slots) * 0.62);

  const handleKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') {
      // Only the panel closes, not the dialog it sits in.
      event.stopPropagation();
      onClose();
    }
  };

  const handlePointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const point = svgPointer(event.currentTarget, event.clientX, event.clientY);
    if (!point || point.y < TOP || point.y > VOL_TOP + VOL_H) {
      setHover(null);
      return;
    }
    if (point.x >= LEFT && point.x < openX) {
      const nearest = auctionPoints.reduce((best, p, i) =>
        Math.abs(auctionX(p.time) - point.x) < Math.abs(auctionX(auctionPoints[best].time) - point.x) ? i : best, 0);
      setHover(auctionPoints.length ? { session: 'auction', index: nearest, x: point.x } : null);
      return;
    }
    const slot = Math.floor(((point.x - openX) / regularW) * slots);
    const index = series.points.findIndex((bar) => bar.slot === slot);
    setHover(index >= 0 ? { session: 'regular', index, x: point.x } : null);
  };

  const selectedAuction = hover?.session === 'auction' ? auctionPoints[hover.index]
    : !series.points.length ? auctionPoints[auctionPoints.length - 1] : undefined;
  const active = selectedAuction ? {
    label: selectedAuction.time, close: selectedAuction.price,
    changePercent: prevClose > 0 ? (selectedAuction.price / prevClose - 1) * 100 : 0,
    volume: selectedAuction.volume, avg: null,
  } : series.points[hover?.index ?? series.points.length - 1];
  const match = selectedAuction?.time.startsWith('09:25:') && auction?.source !== 'ashareapi';

  let notice: React.ReactNode;
  if (state.status === 'loading') {
    notice = (
      <div className="qi-state" aria-busy="true">
        <p>{t('quote.intraday.loading')}</p>
      </div>
    );
  } else if (state.status === 'error') {
    notice = (
      <div className="qi-state" role="alert">
        <p>
          {t('quote.intraday.error')}：{state.message}
        </p>
        <button
          type="button"
          className="btn sm"
          onClick={() => {
            setState({ status: 'loading' });
            setReloadKey((key) => key + 1);
          }}
        >
          {t('common.retry')}
        </button>
      </div>
    );
  } else if (!series.points.length) {
    notice = (
      <div className="qi-state" role="status">
        <p>{t('quote.intraday.empty')}</p>
      </div>
    );
  }
  let body: React.ReactNode;
  if (active) {
    const half = (series.max - series.min) / 2;
    const gridPrices = [series.max, prevClose + half / 2, prevClose, prevClose - half / 2, series.min];
    const pathFor = (field: 'close' | 'avg') => series.points.map((point, i) => {
      const price = point[field];
      if (price === null) return '';
      const prev = series.points[i - 1];
      const command = !prev || prev[field] === null || point.slot - prev.slot > 1 ? 'M' : 'L';
      return `${command}${xOf(point.slot).toFixed(1)},${yOf(price).toFixed(1)}`;
    }).join('');
    const volumeScale = series.maxVolume > 0 ? VOL_H / series.maxVolume : 0;

    body = (
      <>
        <dl className="qi-readout" aria-live="off">
          <div>
            <dd>{active.label}</dd>
          </div>
          <div>
            <dt>{t(match ? 'quote.auction.match' : 'quote.intraday.price')}</dt>
            <dd className={signClass(active.changePercent)}>{active.close.toFixed(digits)}</dd>
          </div>
          <div>
            <dt>{t('quote.change')}</dt>
            <dd className={signClass(active.changePercent)}>
              {active.changePercent > 0 ? '+' : ''}
              {active.changePercent.toFixed(2)}%
            </dd>
          </div>
          <div>
            <dt style={{ color: CHART_COLORS.avgCost }}>{t('quote.intraday.avg')}</dt>
            <dd>{active.avg === null ? '--' : active.avg.toFixed(digits)}</dd>
          </div>
          <div>
            <dt>{t('quote.volume')}</dt>
            <dd>{active.volume == null ? '--' : formatVolume(active.volume)}</dd>
          </div>
          <div>
            <dt>{t('quote.intraday.prevClose')}</dt>
            <dd>{prevClose.toFixed(digits)}</dd>
          </div>
        </dl>
        <svg
          className="qi-svg"
          viewBox={`0 0 ${width} ${H}`}
          role="img"
          aria-label={t('quote.intraday.chartLabel', { name, date })}
          onPointerMove={handlePointerMove}
          onPointerLeave={() => setHover(null)}
        >
          <rect className="qi-auction-band" x={LEFT} y={TOP} width={openX - LEFT} height={VOL_TOP + VOL_H - TOP} />
          <line x1={openX} x2={openX} y1={TOP} y2={VOL_TOP + VOL_H} stroke={CHART_COLORS.grid} />
          {gridPrices.map((price, i) => {
            const isBase = i === 2;
            const percent = ((price - prevClose) / prevClose) * 100;
            return (
              <g key={i}>
                <line
                  x1={LEFT}
                  x2={width - RIGHT}
                  y1={yOf(price)}
                  y2={yOf(price)}
                  stroke={isBase ? CHART_COLORS.cross : CHART_COLORS.grid}
                  strokeDasharray={isBase ? '5 4' : undefined}
                />
                <text x={LEFT - 6} y={yOf(price)} textAnchor="end" dominantBaseline="middle" fill={CHART_COLORS.text} fontSize="11">
                  {price.toFixed(digits)}
                </text>
                <text
                  x={width - RIGHT + 6}
                  y={yOf(price)}
                  dominantBaseline="middle"
                  fill={isBase ? CHART_COLORS.text : percent > 0 ? CHART_COLORS.up : CHART_COLORS.down}
                  fontSize="11"
                >
                  {percent > 0 ? '+' : ''}
                  {percent.toFixed(2)}%
                </text>
              </g>
            );
          })}

          <g aria-label={t('quote.auction.title')}>
            <text x={LEFT + 6} y={TOP + 15} fill={CHART_COLORS.text} fontSize="11">{t('quote.auction.title')}</text>
            {!auctionPoints.length && (
              <text x={LEFT + 6} y={TOP + 32} fill={CHART_COLORS.faint} fontSize="11">
                {t(auction ? 'quote.auction.empty' : 'quote.intraday.loading')}
              </text>
            )}
            {auctionPoints.length > 1 && (
              <polyline points={auctionPoints.map(p => [auctionX(p.time), yOf(p.price)].join(',')).join(' ')}
                fill="none" stroke={CHART_COLORS.ma[3]} strokeWidth="1.6" />
            )}
            {auctionPoints.map((p, i) => (
              <g key={p.time}>
                <circle cx={auctionX(p.time)} cy={yOf(p.price)} r={auctionPoints.length === 1 ? 2.8 : 1.4} fill={CHART_COLORS.ma[3]} />
                {p.volume != null && p.volume > 0 && (
                  <rect x={auctionX(p.time) - barWidth / 2} y={VOL_TOP + VOL_H - p.volume * volumeScale}
                    width={barWidth} height={p.volume * volumeScale}
                    fill={p.price >= (auctionPoints[i - 1]?.price ?? prevClose) ? CHART_COLORS.up : CHART_COLORS.down} />
                )}
              </g>
            ))}
          </g>
          <text x={LEFT} y={AXIS_Y} textAnchor="middle" fill={CHART_COLORS.faint} fontSize="11">09:15</text>

          {series.points.map((point, i) => {
            const before = i === 0 ? prevClose : series.points[i - 1].close;
            const height = point.volume * volumeScale;
            return (
              <rect
                className="qi-minute-volume"
                key={point.label}
                x={xOf(point.slot) - barWidth / 2}
                y={VOL_TOP + VOL_H - height}
                width={barWidth}
                height={height}
                fill={point.close >= before ? CHART_COLORS.up : CHART_COLORS.down}
                opacity={0.85}
              />
            );
          })}
          <line x1={LEFT} x2={width - RIGHT} y1={VOL_TOP + VOL_H} y2={VOL_TOP + VOL_H} stroke={CHART_COLORS.grid} />

          {AXIS_TIMES.filter(label => width >= 560 || (label !== '10:30' && label !== '14:00')).map((label) => {
            const index = minuteSlot(label);
            return (
              <text key={label} x={xOf(index)} y={AXIS_Y} textAnchor="middle" fill={CHART_COLORS.faint} fontSize="11">
                {label}
              </text>
            );
          })}

          <path d={pathFor('close')} fill="none" stroke={CHART_COLORS.ma[3]} strokeWidth="1.6" strokeLinejoin="round" />
          <path d={pathFor('avg')} fill="none" stroke={CHART_COLORS.avgCost} strokeWidth="1.3" />
          {series.points.map((point, i) => {
            if (series.points[i - 1]?.slot === point.slot - 1 || series.points[i + 1]?.slot === point.slot + 1) return null;
            return <circle key={point.label} cx={xOf(point.slot)} cy={yOf(point.close)} r="1.6" fill={CHART_COLORS.ma[3]} />;
          })}

          {hover && active ? (
            <g pointerEvents="none">
              <line x1={hover.x} x2={hover.x} y1={TOP} y2={VOL_TOP + VOL_H} stroke={CHART_COLORS.crossLine} strokeDasharray="3 3" />
              <circle cx={hover.x} cy={yOf(active.close)} r="3.5" fill={CHART_COLORS.ma[3]} />
            </g>
          ) : null}
        </svg>
        {state.status === 'ready' && state.stale ? <p className="qi-stale">{t('quote.intraday.stale')}</p> : null}
      </>
    );
  }

  return (
    <div
      ref={rootRef}
      className="qi"
      tabIndex={-1}
      role="group"
      aria-label={t('quote.intraday.title', { date })}
      onKeyDown={handleKeyDown}
    >
      <div className="qi-top">
        <h3>{t('quote.intraday.title', { date })}</h3>
        <button type="button" className="btn sm" onClick={onClose}>
          {t('quote.intraday.back')}
        </button>
      </div>
      {body}
      {notice}
    </div>
  );
};
