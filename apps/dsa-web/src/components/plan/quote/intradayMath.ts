import type { StockIntradayBar } from '../../../api/stocks';

/** An A-share session has 48 five-minute bars (09:35-11:30, 13:05-15:00). */
export const SESSION_SLOTS = 48;

export interface IntradayPoint {
  /** HH:MM (Beijing time). */
  label: string;
  close: number;
  /** Volume-weighted average price since the open; null until a bar with volume and amount arrives. */
  avg: number | null;
  volume: number;
  /** Close vs the previous day's close, in percent. */
  changePercent: number;
}

export interface IntradaySeries {
  points: IntradayPoint[];
  /** Price axis, symmetric around the previous close so up and down moves read equally. */
  min: number;
  max: number;
  maxVolume: number;
}

/** `2026-11-06T09:35:00+08:00` / `2026-11-06 09:35:00` -> `09:35`. */
export function barLabel(time: string): string {
  return time.slice(11, 16);
}

/** Builds the price / average / volume series; the average is cumulative amount over cumulative volume. */
export function buildIntradaySeries(bars: StockIntradayBar[], prevClose: number): IntradaySeries {
  let totalAmount = 0;
  let totalVolume = 0;
  let lastAvg: number | null = null;
  const points: IntradayPoint[] = bars.map((bar) => {
    const volume = bar.volume ?? 0;
    if (bar.amount != null && volume > 0) {
      totalAmount += bar.amount;
      totalVolume += volume;
      lastAvg = totalAmount / totalVolume;
    }
    return {
      label: barLabel(bar.time),
      close: bar.close,
      avg: lastAvg,
      volume,
      changePercent: prevClose > 0 ? ((bar.close - prevClose) / prevClose) * 100 : 0,
    };
  });

  let reach = 0;
  for (const bar of bars) {
    reach = Math.max(reach, Math.abs(bar.close - prevClose));
    if (bar.high != null) {
      reach = Math.max(reach, Math.abs(bar.high - prevClose));
    }
    if (bar.low != null) {
      reach = Math.max(reach, Math.abs(bar.low - prevClose));
    }
  }
  // Never collapse to a flat line: keep at least 0.5% of the previous close on each side.
  const half = Math.max(reach * 1.08, prevClose * 0.005);
  return {
    points,
    min: prevClose - half,
    max: prevClose + half,
    maxVolume: points.reduce((peak, point) => Math.max(peak, point.volume), 0),
  };
}
