import { describe, expect, it } from 'vitest';
import { barLabel, buildIntradaySeries } from '../intradayMath';

const bar = (time: string, close: number, volume: number | null, amount: number | null, high = close, low = close) => ({
  time,
  open: close,
  high,
  low,
  close,
  volume,
  amount,
});

describe('barLabel', () => {
  it('keeps HH:MM of the provider timestamp', () => {
    expect(barLabel('2026-11-06T09:35:00+08:00')).toBe('09:35');
    expect(barLabel('2026-11-06 15:00:00')).toBe('15:00');
  });
});

describe('buildIntradaySeries', () => {
  it('keeps missing minutes in place and excludes the lunch break', () => {
    const timestamps = ['09:31', '09:33', '11:30', '12:15', '13:00', '15:00'];
    const series = buildIntradaySeries(timestamps.map((time) => bar(`2026-11-06T${time}:00+08:00`, 10, 100, 1000)), 10);
    expect(series.points.map((point) => point.slot)).toEqual([1, 3, 120, 121, 241]);
  });

  it('averages cumulative amount over cumulative volume', () => {
    const series = buildIntradaySeries(
      [
        bar('2026-11-06T09:35:00+08:00', 10, 100, 1000),
        bar('2026-11-06T09:40:00+08:00', 12, 300, 3600),
      ],
      10,
    );
    expect(series.points[0].avg).toBe(10);
    expect(series.points[1].avg).toBeCloseTo(4600 / 400, 6);
    expect(series.maxVolume).toBe(300);
  });

  it('carries the last average over a bar without volume and stays null before the first one', () => {
    const series = buildIntradaySeries(
      [
        bar('2026-11-06T09:35:00+08:00', 10, 0, 0),
        bar('2026-11-06T09:40:00+08:00', 11, 100, 1100),
        bar('2026-11-06T09:45:00+08:00', 11, null, null),
      ],
      10,
    );
    expect(series.points.map((point) => point.avg)).toEqual([null, 11, 11]);
  });

  it('measures change against the previous close', () => {
    const series = buildIntradaySeries([bar('2026-11-06T09:35:00+08:00', 8.9, 1, 8.9)], 8.86);
    expect(series.points[0].changePercent).toBeCloseTo(0.4515, 3);
  });

  it('centres the price axis on the previous close and covers highs and lows', () => {
    const series = buildIntradaySeries(
      [bar('2026-11-06T09:35:00+08:00', 10.2, 1, 1, 10.5, 9.9), bar('2026-11-06T09:40:00+08:00', 9.8, 1, 1, 10.1, 9.4)],
      10,
    );
    expect((series.min + series.max) / 2).toBeCloseTo(10, 9);
    expect(series.max).toBeGreaterThanOrEqual(10.5);
    expect(series.min).toBeLessThanOrEqual(9.4);
  });

  it('keeps a visible range for a flat day', () => {
    const series = buildIntradaySeries([bar('2026-11-06T09:35:00+08:00', 10, 1, 10)], 10);
    expect(series.max - series.min).toBeGreaterThan(0);
  });

  it('handles an empty day', () => {
    const series = buildIntradaySeries([], 10);
    expect(series.points).toEqual([]);
    expect(series.maxVolume).toBe(0);
  });
});
