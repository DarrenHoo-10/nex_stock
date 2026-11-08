/** Pure K-line helpers: bar normalization, weekly/monthly aggregation and moving averages. */

export interface DailyBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  amount: number | null;
  changePercent: number | null;
  /** Daily turnover in percent; null when unknown. */
  turnoverRate: number | null;
}

export type KlinePeriod = 'daily' | 'weekly' | 'monthly';

/** quote: float shares from the realtime quote; estimated: median volume ≈ 2% turnover; none: no volume. */
export type TurnoverBasis = 'quote' | 'estimated' | 'none';

/** Without float shares, the median daily volume is assumed to be this share of the float. */
export const ESTIMATED_MEDIAN_TURNOVER = 0.02;

/**
 * Daily turnover (%) = volume / float shares. Float shares are constant over the window, so share
 * changes (placements, ETF creations/redemptions) make older turnover approximate.
 */
export function applyTurnover(
  bars: DailyBar[],
  floatShares: number | null | undefined,
): { bars: DailyBar[]; basis: TurnoverBasis } {
  const volumes = bars
    .map((bar) => bar.volume)
    .filter((volume): volume is number => volume !== null && volume > 0)
    .sort((a, b) => a - b);
  if (!volumes.length) {
    return { bars: bars.map((bar) => ({ ...bar, turnoverRate: null })), basis: 'none' };
  }
  let float = floatShares && floatShares > 0 ? floatShares : null;
  const basis: TurnoverBasis = float ? 'quote' : 'estimated';
  if (!float) {
    float = volumes[Math.floor(volumes.length / 2)] / ESTIMATED_MEDIAN_TURNOVER;
  }
  return {
    basis,
    bars: bars.map((bar) => ({
      ...bar,
      turnoverRate: bar.volume && bar.volume > 0 ? Math.min(bar.volume / float, 1) * 100 : null,
    })),
  };
}

export interface ChartBar extends DailyBar {
  /** Index of the last daily bar inside this bar (chip distribution is computed up to that day). */
  dailyIndex: number;
}

function isoWeekKey(date: string): string {
  const [y, m, d] = date.slice(0, 10).split('-').map(Number);
  const utc = new Date(Date.UTC(y, m - 1, d));
  const day = utc.getUTCDay() || 7;
  utc.setUTCDate(utc.getUTCDate() + 4 - day);
  const yearStart = Date.UTC(utc.getUTCFullYear(), 0, 1);
  const week = Math.ceil(((utc.getTime() - yearStart) / 86400000 + 1) / 7);
  return `${utc.getUTCFullYear()}-W${week}`;
}

function periodKey(date: string, period: KlinePeriod): string {
  if (period === 'monthly') {
    return date.slice(0, 7);
  }
  if (period === 'weekly') {
    return isoWeekKey(date);
  }
  return date;
}

/** Aggregate daily bars into weekly/monthly bars; daily returns the bars unchanged (with indices). */
export function aggregateBars(daily: DailyBar[], period: KlinePeriod): ChartBar[] {
  if (period === 'daily') {
    return daily.map((bar, index) => ({ ...bar, dailyIndex: index }));
  }
  const result: ChartBar[] = [];
  let currentKey = '';
  daily.forEach((bar, index) => {
    const key = periodKey(bar.date, period);
    const last = result[result.length - 1];
    if (!last || key !== currentKey) {
      currentKey = key;
      result.push({ ...bar, dailyIndex: index });
      return;
    }
    last.high = Math.max(last.high, bar.high);
    last.low = Math.min(last.low, bar.low);
    last.close = bar.close;
    last.date = bar.date;
    last.volume = sumNullable(last.volume, bar.volume);
    last.amount = sumNullable(last.amount, bar.amount);
    last.turnoverRate = sumNullable(last.turnoverRate, bar.turnoverRate);
    last.dailyIndex = index;
  });
  // Change percent is recomputed against the previous aggregated close.
  result.forEach((bar, index) => {
    const prev = index > 0 ? result[index - 1].close : null;
    bar.changePercent = prev ? ((bar.close - prev) / prev) * 100 : bar.changePercent;
  });
  return result;
}

function sumNullable(a: number | null, b: number | null): number | null {
  if (a === null && b === null) {
    return null;
  }
  return (a ?? 0) + (b ?? 0);
}

/** Simple moving average; entries before the window fills are null. */
export function movingAverage(values: Array<number | null>, window: number): Array<number | null> {
  const result: Array<number | null> = new Array(values.length).fill(null);
  let sum = 0;
  let count = 0;
  for (let i = 0; i < values.length; i += 1) {
    const value = values[i] ?? 0;
    sum += value;
    count += 1;
    if (count > window) {
      sum -= values[i - window] ?? 0;
      count = window;
    }
    if (count === window) {
      result[i] = sum / window;
    }
  }
  return result;
}

export const PRICE_MA_WINDOWS = [5, 10, 20, 60] as const;
export const VOLUME_MA_WINDOWS = [5, 10] as const;

/** Format a share volume the way Chinese terminals do (万 / 亿). */
export function formatVolume(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '--';
  }
  const abs = Math.abs(value);
  if (abs >= 1e8) {
    return `${(value / 1e8).toFixed(2)}亿`;
  }
  if (abs >= 1e4) {
    return `${(value / 1e4).toFixed(2)}万`;
  }
  return value.toFixed(0);
}

/** Decimal places for a price: 3 for sub-10 prices (ETF style), otherwise 2. */
export function priceDigits(sample: number): number {
  return Math.abs(sample) < 10 ? 3 : 2;
}
