import type { HorizonPlan, Level, LevelKind, Sizing, StockPlan } from '../../../types/dailyPlan';
import { fmtPrice } from './format';

/** Level kinds the ladder draws, plus `worst` (limit-down on the stop day) which the API does not name. */
export type LadderKind = LevelKind | 'worst';

export interface LadderLevel {
  kind: LadderKind;
  label: string;
  /** Single price, or the centre of a band. */
  price: number;
  /** Buy zone [low, high]. */
  band?: [number, number];
}

export interface PlacedLevel extends LadderLevel {
  /** Marker position. */
  y: number;
  /** Label position (pushed apart so texts never overlap). */
  ly: number;
  /** Band extent (top and bottom edge) when the level is a zone. */
  y1?: number;
  y2?: number;
}

export interface LadderGeometry {
  width: number;
  height: number;
  xLine: number;
  top: number;
  bottom: number;
  items: PlacedLevel[];
}

const WIDTH = 240;
const TOP = 18;
const BOTTOM = 18;
const X_LINE = 30;
const GAP = 36;
const MIN_HEIGHT = 420;

/** Price text of a level: "46.80–47.20" for a band, "47.00" otherwise. */
export function levelPriceText(level: LadderLevel): string {
  return level.band ? `${fmtPrice(level.band[0])}–${fmtPrice(level.band[1])}` : fmtPrice(level.price);
}

/**
 * Port of the prototype's `ladder()` layout: price maps linearly to y, then labels are spread by at least
 * `GAP` px (top-down, then pushed back up when they overflow the bottom). Height grows with the level count.
 */
export function layoutLadder(levels: LadderLevel[]): LadderGeometry {
  const valid = levels.filter((level) => Number.isFinite(level.price));
  const height = Math.max(MIN_HEIGHT, TOP + BOTTOM + (valid.length + 1) * GAP);
  const prices = valid.flatMap((level) => (level.band ? level.band : [level.price]));
  let lo = prices.length ? Math.min(...prices) : 0;
  let hi = prices.length ? Math.max(...prices) : 1;
  const pad = (hi - lo) * 0.06 || 1;
  lo -= pad;
  hi += pad;
  const yOf = (price: number) => TOP + ((hi - price) / (hi - lo)) * (height - TOP - BOTTOM);

  const items: PlacedLevel[] = valid
    .map((level) => ({
      ...level,
      y: yOf(level.price),
      ly: 0,
      ...(level.band ? { y1: yOf(level.band[1]), y2: yOf(level.band[0]) } : {}),
    }))
    .sort((a, b) => a.y - b.y);
  let last = -99;
  for (const item of items) {
    item.ly = Math.max(item.y, last + GAP);
    last = item.ly;
  }
  const overflow = last - (height - BOTTOM);
  if (overflow > 0) {
    for (const item of items) {
      item.ly -= overflow;
    }
    for (let index = items.length - 2; index >= 0; index -= 1) {
      if (items[index].ly > items[index + 1].ly - GAP) {
        items[index].ly = items[index + 1].ly - GAP;
      }
    }
  }
  return { width: WIDTH, height, xLine: X_LINE, top: TOP, bottom: BOTTOM, items };
}

/**
 * Levels for the ladder from the plan: the API's `levels` (entry becomes the buy-zone band when the current
 * horizon has one), the next-day limit-down line when the API did not send one, and the limit-down price on the
 * stop day when the sizing is computed.
 */
export function buildLadderLevels(plan: StockPlan, horizonPlan: HorizonPlan | null): LadderLevel[] {
  const sizing: Sizing | null = horizonPlan?.sizing ?? null;
  const zone = horizonPlan?.entryZone ?? null;
  const levels: LadderLevel[] = plan.levels.map((level: Level) => {
    if (level.kind === 'entry' && zone) {
      return { kind: level.kind, label: level.label, price: (zone[0] + zone[1]) / 2, band: [zone[0], zone[1]] };
    }
    return { kind: level.kind, label: level.label, price: level.price };
  });
  if (plan.nextLimitDown != null && !levels.some((level) => level.kind === 'limit_down')) {
    levels.push({ kind: 'limit_down', label: '明天跌停价', price: plan.nextLimitDown });
  }
  if (sizing && sizing.status === 'ok' && Number.isFinite(sizing.limitDownPrice) && sizing.limitDownPrice > 0) {
    levels.push({ kind: 'worst', label: '止损日再跌停', price: sizing.limitDownPrice });
  }
  return levels;
}
