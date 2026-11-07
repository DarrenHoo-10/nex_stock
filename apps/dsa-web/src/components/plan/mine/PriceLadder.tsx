import type React from 'react';
import { useId, useMemo } from 'react';
import { motion } from 'motion/react';
import { PLAN_SPRING } from '../motion/springs';
import { usePlanReducedMotion } from '../motion/usePlanMotion';
import { layoutLadder, levelPriceText } from './ladderLayout';
import type { LadderLevel, PlacedLevel } from './ladderLayout';

export interface PriceLadderProps {
  levels: LadderLevel[];
  /** Caption above the drawing, e.g. 「持仓 2,400 股，成本 40.20」. */
  caption: string;
}

const MAX_LABEL_CHARS = 18;

function clipLabel(label: string): string {
  const chars = [...label];
  return chars.length > MAX_LABEL_CHARS ? `${chars.slice(0, MAX_LABEL_CHARS - 1).join('')}…` : label;
}

const HINT_KINDS = new Set(['support', 'cost']);

function Marker({ item, xLine, glowId }: { item: PlacedLevel; xLine: number; glowId: string }) {
  if (item.band && item.y1 !== undefined && item.y2 !== undefined) {
    const height = Math.max(8, item.y2 - item.y1);
    return (
      <>
        <rect className="mk-band-fill" x={xLine - 14} y={item.y1} width={28} height={height} rx={8} />
        <rect className="mk-band" x={xLine - 14} y={item.y1} width={28} height={height} rx={8} />
      </>
    );
  }
  switch (item.kind) {
    case 'close':
      return (
        <>
          <circle className="mk-close-glow" cx={xLine} cy={item.y} r={11} filter={`url(#${glowId})`} />
          <circle className="mk-close" cx={xLine} cy={item.y} r={6.5} />
        </>
      );
    case 'stop':
      return (
        <>
          <line className="mk-stop-glow" x1={xLine - 16} x2={xLine + 16} y1={item.y} y2={item.y} filter={`url(#${glowId})`} />
          <line className="mk-stop" x1={xLine - 16} x2={xLine + 16} y1={item.y} y2={item.y} />
        </>
      );
    case 'worst':
      return <line className="mk-worst" x1={xLine - 12} x2={xLine + 12} y1={item.y} y2={item.y} />;
    case 'limit_down':
      return <line className="mk-limit-down" x1={xLine - 14} x2={xLine + 14} y1={item.y} y2={item.y} />;
    case 'limit_up':
      return <line className="mk-limit-up" x1={xLine - 14} x2={xLine + 14} y1={item.y} y2={item.y} />;
    default:
      return (
        <circle
          className={HINT_KINDS.has(item.kind) ? 'mk-dot mk-hint' : `mk-dot mk-${item.kind.replace('_', '-')}`}
          cx={xLine}
          cy={item.y}
          r={4.5}
        />
      );
  }
}

/**
 * Vertical price ladder (inline SVG). The drawing is `role="img"`; the same levels are also written out as a
 * visually hidden list for screen readers. The axis draws first, then the levels slide in; both are instant
 * under reduced motion.
 */
export const PriceLadder: React.FC<PriceLadderProps> = ({ levels, caption }) => {
  const reduced = usePlanReducedMotion();
  const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  const glowId = `ladder-glow-${uid}`;
  const listId = `ladder-list-${uid}`;
  const geometry = useMemo(() => layoutLadder(levels), [levels]);
  const spoken = useMemo(() => [...geometry.items].sort((a, b) => b.price - a.price), [geometry]);
  const { width, height, xLine, top, bottom, items } = geometry;

  return (
    <aside className="ladder" aria-label="价格梯子">
      <figure>
        <figcaption>{caption}</figcaption>
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="价格梯子" aria-describedby={listId}>
          <defs>
            <filter id={glowId} x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3.2" />
            </filter>
          </defs>
          <motion.line
            className="axis"
            x1={xLine}
            x2={xLine}
            y1={top - 8}
            y2={height - bottom + 8}
            initial={reduced ? false : { pathLength: 0 }}
            animate={{ pathLength: 1 }}
            transition={reduced ? { duration: 0 } : { duration: 0.76, delay: 0.12, ease: [0.3, 0.7, 0.2, 1] }}
          />
          {items.map((item, index) => (
            <motion.g
              key={`${item.kind}-${item.price}-${index}`}
              className={`lv lv-${item.kind.replace('_', '-')}`}
              initial={reduced ? false : { opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={reduced ? { duration: 0 } : { ...PLAN_SPRING, delay: 0.22 + index * 0.06 }}
            >
              <Marker item={item} xLine={xLine} glowId={glowId} />
              <path
                className="lv-link"
                d={`M${xLine + 18} ${item.y} C${xLine + 28} ${item.y} ${xLine + 26} ${item.ly} ${xLine + 38} ${item.ly}`}
              />
              <text className="p" x={xLine + 44} y={item.ly - 2}>
                {levelPriceText(item)}
              </text>
              <text x={xLine + 44} y={item.ly + 14}>
                {clipLabel(item.label)}
              </text>
            </motion.g>
          ))}
        </svg>
        <ul id={listId} className="vh">
          {spoken.map((item, index) => (
            <li key={`${item.kind}-${index}`}>{`${item.label} ${levelPriceText(item)}`}</li>
          ))}
        </ul>
      </figure>
    </aside>
  );
};
