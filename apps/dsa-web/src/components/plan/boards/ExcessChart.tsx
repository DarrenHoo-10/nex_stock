import type React from 'react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { motion } from 'motion/react';
import type { BoardSeriesPoint } from '../../../types/boards';
import { usePlanReducedMotion } from '../motion';
import { formatPct, isNumber, shortDate, signClass } from './boardMeta';

const DEFAULT_W = 620;
const MIN_W = 240;
const MAX_W = 760;
const H = 176;
const PAD_X = 28;
const PAD_TOP = 22;
const PAD_BOTTOM = 26;
const UP_MARK = '#FF7468';
const DOWN_MARK = '#3CC3B4';

type Plotted = { index: number; x: number; y: number; point: BoardSeriesPoint; value: number | null };

function buildLayout(series: BoardSeriesPoint[], W: number) {
  const values = series.map((point) => point.excessPct).filter(isNumber);
  const max = Math.max(1, ...values.map((value) => Math.abs(value)));
  const mid = PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2;
  const half = (H - PAD_TOP - PAD_BOTTOM) / 2;
  const step = series.length > 1 ? (W - PAD_X * 2) / (series.length - 1) : 0;
  const plotted: Plotted[] = series.map((point, index) => {
    const value = isNumber(point.excessPct) ? point.excessPct : null;
    return {
      index,
      x: series.length > 1 ? PAD_X + index * step : W / 2,
      y: value === null ? mid : mid - (value / max) * half,
      point,
      value,
    };
  });
  // Consecutive non-null points form one segment; a missing day breaks the line.
  const segments: Plotted[][] = [];
  let current: Plotted[] = [];
  plotted.forEach((item) => {
    if (item.value === null) {
      if (current.length) segments.push(current);
      current = [];
    } else {
      current.push(item);
    }
  });
  if (current.length) segments.push(current);
  return { plotted, segments, mid };
}

function summary(series: BoardSeriesPoint[], benchmark: string): string {
  const parts = series.map((point) => `${shortDate(point.tradeDate)} ${formatPct(point.excessPct)}`);
  return `最近 ${series.length} 个交易日相对${benchmark}的超额收益：${parts.join('，')}`;
}

export interface ExcessChartProps {
  series: BoardSeriesPoint[];
  /** Benchmark name for the accessible description, default 中证全指. */
  benchmark?: string;
}

/**
 * Thin (2px) line of the last trading days' excess return with a zero baseline and a hover/keyboard tooltip.
 * The line draws in once, unless the user prefers reduced motion.
 */
export const ExcessChart: React.FC<ExcessChartProps> = ({ series, benchmark = '中证全指' }) => {
  const reduced = usePlanReducedMotion();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState<number | null>(null);
  // The viewBox follows the real pixel width so labels keep their size on narrow screens.
  const [W, setWidth] = useState(DEFAULT_W);
  useEffect(() => {
    const element = wrapRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver((entries) => {
      const next = Math.round(entries[0]?.contentRect.width ?? DEFAULT_W);
      if (next > 0) setWidth(Math.min(MAX_W, Math.max(MIN_W, next)));
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [series.length]);
  const { plotted, segments, mid } = useMemo(() => buildLayout(series, W), [series, W]);
  const labelEvery = W < 420 ? 2 : 1;

  if (series.length === 0) {
    return <p className="note">没有近期的超额收益序列。</p>;
  }

  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const x = ((event.clientX - rect.left) / rect.width) * W;
    let best = 0;
    let bestDistance = Infinity;
    plotted.forEach((item) => {
      const distance = Math.abs(item.x - x);
      if (distance < bestDistance) {
        best = item.index;
        bestDistance = distance;
      }
    });
    setActive(best);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      event.preventDefault();
      const delta = event.key === 'ArrowRight' ? 1 : -1;
      setActive((current) => {
        const next = (current ?? (delta > 0 ? -1 : series.length)) + delta;
        return Math.min(series.length - 1, Math.max(0, next));
      });
    } else if (event.key === 'Escape') {
      setActive(null);
    }
  };

  const activeItem = active === null ? null : plotted[active];
  const lastIndex = plotted.length - 1;
  const tooltipLeft = activeItem ? Math.min(Math.max((activeItem.x / W) * 100, 12), 88) : 0;

  return (
    <div className="excess-chart">
      <div
        ref={wrapRef}
        className="excess-wrap"
        role="group"
        aria-roledescription="折线图"
        aria-label={summary(series, benchmark)}
        tabIndex={0}
        data-testid="excess-chart"
        onPointerMove={handlePointerMove}
        onPointerLeave={() => setActive(null)}
        onKeyDown={handleKeyDown}
        onBlur={() => setActive(null)}
      >
        <svg viewBox={`0 0 ${W} ${H}`} aria-hidden="true" focusable="false">
          <line x1={PAD_X - 10} x2={W - PAD_X + 10} y1={mid} y2={mid} className="zero-line" />
          <text x={PAD_X - 10} y={mid - 6} textAnchor="start" className="zero-label">
            0
          </text>
          {activeItem ? (
            <line x1={activeItem.x} x2={activeItem.x} y1={PAD_TOP - 8} y2={H - PAD_BOTTOM + 4} className="cursor-line" />
          ) : null}
          {segments.map((segment) =>
            segment.length > 1 ? (
              <motion.polyline
                key={segment[0].index}
                points={segment.map((item) => `${item.x},${item.y}`).join(' ')}
                className="excess-line"
                fill="none"
                initial={reduced ? false : { pathLength: 0, opacity: 0.4 }}
                animate={{ pathLength: 1, opacity: 1 }}
                transition={reduced ? { duration: 0 } : { duration: 0.9, ease: [0.25, 1, 0.5, 1] }}
              />
            ) : null,
          )}
          {plotted.map((item) =>
            item.value === null ? (
              <text key={item.index} x={item.x} y={mid - 8} textAnchor="middle" className="gap-label">
                无
              </text>
            ) : (
              <circle
                key={item.index}
                cx={item.x}
                cy={item.y}
                r={item.index === lastIndex || item.index === active ? 4.5 : 3}
                fill={item.value >= 0 ? UP_MARK : DOWN_MARK}
                className="excess-dot"
              />
            ),
          )}
          {plotted
            .filter((item) => (lastIndex - item.index) % labelEvery === 0)
            .map((item) => (
            <text key={`d${item.index}`} x={item.x} y={H - 6} textAnchor="middle" className={item.index === active ? 'on' : ''}>
              {shortDate(item.point.tradeDate)}
            </text>
          ))}
          {plotted[lastIndex] && plotted[lastIndex].value !== null && active === null ? (
            <text
              x={plotted[lastIndex].x}
              y={plotted[lastIndex].y + ((plotted[lastIndex].value ?? 0) >= 0 ? -10 : 18)}
              textAnchor="end"
              className="last-label"
            >
              {formatPct(plotted[lastIndex].value)}
            </text>
          ) : null}
        </svg>
        {activeItem ? (
          <div className="excess-tip" style={{ left: `${tooltipLeft}%` }} data-testid="excess-tooltip">
            <span>{activeItem.point.tradeDate}</span>
            <b className={`num ${signClass(activeItem.value)}`}>
              {activeItem.value === null ? '无数据' : `超额 ${formatPct(activeItem.value)}`}
            </b>
          </div>
        ) : null}
      </div>
      <div className="legend">
        <span>
          <i style={{ background: UP_MARK }} />
          强于{benchmark}
        </span>
        <span>
          <i style={{ background: DOWN_MARK }} />
          弱于{benchmark}
        </span>
      </div>
    </div>
  );
};
