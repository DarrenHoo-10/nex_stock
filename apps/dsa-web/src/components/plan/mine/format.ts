import type { BoardMarket, Horizon, PlanAction, PlanVerdict } from '../../../types/dailyPlan';

const numberFormatters = new Map<number, Intl.NumberFormat>();

function formatter(decimals: number): Intl.NumberFormat {
  let found = numberFormatters.get(decimals);
  if (!found) {
    found = new Intl.NumberFormat('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    numberFormatters.set(decimals, found);
  }
  return found;
}

const isNum = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/** Thousands separators, fixed decimals ("98,700"); a dash for missing values. */
export function fmtNum(value: number | null | undefined, decimals = 0): string {
  return isNum(value) ? formatter(decimals).format(value) : '—';
}

/** Price with two decimals ("47.00"). */
export function fmtPrice(value: number | null | undefined): string {
  return fmtNum(value, 2);
}

/** Percent with one decimal and a unit ("98.7%"). */
export function fmtPct(value: number | null | undefined, decimals = 1): string {
  return isNum(value) ? `${formatter(decimals).format(value)}%` : '—';
}

/** Signed percent ("+4.21%"). */
export function fmtSignedPct(value: number | null | undefined, decimals = 2): string {
  if (!isNum(value)) {
    return '—';
  }
  const text = formatter(decimals).format(value);
  return `${value > 0 ? '+' : ''}${text}%`;
}

/** Red up / green down (plan-glass tokens): `up`, `down` or `flat`. */
export function changeClass(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (!isNum(value) || value === 0) {
    return 'flat';
  }
  return value > 0 ? 'up' : 'down';
}

export const VERDICT_LABEL: Record<PlanVerdict, string> = {
  buy: '买入',
  wait: '等待',
  skip: '不买',
  suspended: '停牌',
  hold: '持有',
  add: '加仓',
  reduce: '减仓',
  clear: '清仓',
};

/** Tag modifier from plan-glass.css (`tag buy|wait|skip|hold|warn`). */
export const VERDICT_TAG_CLASS: Record<PlanVerdict, string> = {
  buy: 'buy',
  wait: 'wait',
  skip: 'skip',
  suspended: 'skip',
  hold: 'hold',
  add: 'buy',
  reduce: 'warn',
  clear: 'warn',
};

/** Modifier of the big verdict word (`verdict v-*`). */
export const VERDICT_BIG_CLASS: Record<PlanVerdict, string> = {
  buy: 'v-buy',
  wait: 'v-wait',
  skip: 'v-skip',
  suspended: 'v-skip',
  hold: 'v-hold',
  add: 'v-buy',
  reduce: 'v-sell',
  clear: 'v-sell',
};

export const HORIZON_LABEL: Record<Horizon, string> = { short: '短线', swing: '波段' };
export const HORIZON_HINT: Record<Horizon, string> = { short: '短线 1–5 天', swing: '波段 2–4 周' };

const BOARD_MARKET_LABEL: Record<BoardMarket, string> = {
  main: '主板',
  chinext: '创业板',
  star: '科创板',
  bse: '北交所',
};

/** "创业板", "主板 · ST". */
export function marketLabel(market: BoardMarket, isSt: boolean): string {
  const base = BOARD_MARKET_LABEL[market] ?? '主板';
  return isSt ? `${base} · ST` : base;
}

/** Class of the 「就……」 chip, keyed by the action. */
export function actionClass(action: PlanAction): string {
  if (action === 'sell' || action === 'reduce' || action === 'clear') {
    return 'a-sell';
  }
  if (action === 'buy') {
    return 'a-buy';
  }
  return '';
}

/** True when `text` already mentions the number (so a separate chip would repeat it). */
export function textMentionsNumber(text: string, value: number, decimals: number): boolean {
  const variants = new Set([
    value.toFixed(decimals),
    String(value),
    value.toFixed(1),
    formatter(decimals).format(value),
    formatter(0).format(value),
  ]);
  return [...variants].some((variant) => text.includes(variant));
}

/** Local calendar date as YYYY-MM-DD. */
export function todayLocalDate(now: Date = new Date()): string {
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}-${month}-${day}`;
}

/** "2026-11-06" -> "11-06". */
export function shortDate(date: string): string {
  return /^\d{4}-\d{2}-\d{2}/.test(date) ? date.slice(5, 10) : date;
}
