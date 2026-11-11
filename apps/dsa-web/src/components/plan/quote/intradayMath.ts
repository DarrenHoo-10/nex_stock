import type { OpeningAuction, StockIntradayBar } from '../../../api/stocks';

/** Minutes on the trading clock, including both sessions' opening and closing timestamps. */
const SESSION_SLOTS = 241;

export function minuteSlot(label: string, separateLunchBars = false): number {
  const [hour, minute] = label.split(':').map(Number);
  const time = hour * 60 + minute;
  if (time >= 570 && time <= 690) return time - 570;
  if (time >= 780 && time <= 900) return 120 + Number(separateLunchBars) + time - 780;
  return -1;
}

export interface IntradayPoint {
  /** HH:MM (Beijing time). */
  slot: number;
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
  slots: number;
  separateLunchBars: boolean;
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
export function buildIntradaySeries(bars: StockIntradayBar[], prevClose: number, auction: OpeningAuction['points'] = []): IntradaySeries {
  let totalAmount = 0;
  let totalVolume = 0;
  let lastAvg: number | null = null;
  const regularBars = bars.filter((bar) => minuteSlot(barLabel(bar.time)) >= 0);
  // Both boundary quotes remain selectable when the source reports both.
  const labels = new Set(regularBars.map(bar => barLabel(bar.time)));
  const separateLunchBars = labels.has('11:30') && labels.has('13:00');
  const points: IntradayPoint[] = regularBars.map((bar) => {
    const volume = bar.volume ?? 0;
    if (bar.amount != null && volume > 0) {
      totalAmount += bar.amount;
      totalVolume += volume;
      lastAvg = totalAmount / totalVolume;
    }
    return {
      slot: minuteSlot(barLabel(bar.time), separateLunchBars),
      label: barLabel(bar.time),
      close: bar.close,
      avg: lastAvg,
      volume,
      changePercent: prevClose > 0 ? ((bar.close - prevClose) / prevClose) * 100 : 0,
    };
  });

  let reach = auction.reduce((peak, p) => Math.max(peak, Math.abs(p.price - prevClose)), 0);
  for (const bar of regularBars) {
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
    slots: SESSION_SLOTS + Number(separateLunchBars),
    separateLunchBars,
    min: prevClose - half,
    max: prevClose + half,
    maxVolume: points.reduce((peak, point) => Math.max(peak, point.volume),
      auction.reduce((peak, p) => Math.max(peak, p.volume ?? 0), 0)),
  };
}
