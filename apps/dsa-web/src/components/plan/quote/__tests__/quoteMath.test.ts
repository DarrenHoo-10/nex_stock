import { describe, expect, it } from 'vitest';
import { buildChipGrid, bucketPrice, computeChipDistribution } from '../chipDistribution';
import type { ChipInputBar } from '../chipDistribution';
import { aggregateBars, applyTurnover, formatVolume, movingAverage, priceDigits } from '../klineMath';
import type { DailyBar } from '../klineMath';
import { targetFromRawCode, targetFromSuggestion } from '../quoteTarget';

function bar(partial: Partial<DailyBar> & Pick<DailyBar, 'date' | 'close'>): DailyBar {
  return {
    open: partial.close,
    high: partial.close,
    low: partial.close,
    volume: 1000,
    amount: null,
    changePercent: null,
    turnoverRate: 1,
    ...partial,
  };
}

describe('movingAverage', () => {
  it('fills once the window is complete', () => {
    expect(movingAverage([1, 2, 3, 4], 2)).toEqual([null, 1.5, 2.5, 3.5]);
    expect(movingAverage([1, 2], 5)).toEqual([null, null]);
  });
});

describe('aggregateBars', () => {
  const daily = [
    bar({ date: '2026-10-29', open: 10, high: 11, low: 9.5, close: 10.5, volume: 100, turnoverRate: 1 }),
    bar({ date: '2026-10-30', open: 10.5, high: 12, low: 10, close: 11.8, volume: 200, turnoverRate: 2 }),
    bar({ date: '2026-11-02', open: 11.8, high: 12.2, low: 11, close: 11.2, volume: 50, turnoverRate: 0.5 }),
  ];

  it('keeps daily bars and their indices', () => {
    expect(aggregateBars(daily, 'daily').map((item) => item.dailyIndex)).toEqual([0, 1, 2]);
  });

  it('merges ISO weeks and months', () => {
    const weekly = aggregateBars(daily, 'weekly');
    expect(weekly).toHaveLength(2);
    expect(weekly[0]).toMatchObject({
      open: 10,
      high: 12,
      low: 9.5,
      close: 11.8,
      volume: 300,
      turnoverRate: 3,
      dailyIndex: 1,
    });
    expect(weekly[1].changePercent).toBeCloseTo(((11.2 - 11.8) / 11.8) * 100);

    const monthly = aggregateBars(daily, 'monthly');
    expect(monthly.map((item) => item.date)).toEqual(['2026-10-30', '2026-11-02']);
  });
});

describe('applyTurnover', () => {
  const bars = [
    bar({ date: '2026-11-04', close: 10, volume: 1_000_000 }),
    bar({ date: '2026-11-05', close: 10, volume: 3_000_000 }),
    bar({ date: '2026-11-06', close: 10, volume: null }),
  ];

  it('uses float shares from the quote', () => {
    const result = applyTurnover(bars, 100_000_000);
    expect(result.basis).toBe('quote');
    expect(result.bars.map((item) => item.turnoverRate)).toEqual([1, 3, null]);
  });

  it('estimates from the median volume and caps at 100%', () => {
    const result = applyTurnover(bars, null);
    expect(result.basis).toBe('estimated');
    expect(result.bars[1].turnoverRate).toBeCloseTo(2);
    expect(applyTurnover([bar({ date: '2026-11-04', close: 1, volume: 5e9 })], 1e9).bars[0].turnoverRate).toBe(100);
  });

  it('reports none without volume', () => {
    const result = applyTurnover([bar({ date: '2026-11-04', close: 1, volume: 0 })], 1e9);
    expect(result.basis).toBe('none');
    expect(result.bars[0].turnoverRate).toBeNull();
  });
});

describe('formatting', () => {
  it('formats volumes and digits', () => {
    expect(formatVolume(123456789)).toBe('1.23亿');
    expect(formatVolume(56789)).toBe('5.68万');
    expect(formatVolume(null)).toBe('--');
    expect(priceDigits(3.456)).toBe(3);
    expect(priceDigits(1680)).toBe(2);
  });
});

describe('computeChipDistribution', () => {
  const flat = (price: number, turnoverRate: number | null = 10): ChipInputBar => ({
    open: price,
    high: price + 0.5,
    low: price - 0.5,
    close: price,
    volume: 1000,
    amount: null,
    turnoverRate,
  });

  it('normalizes to 1 and moves cost toward recent prices', () => {
    const bars = [...Array.from({ length: 30 }, () => flat(10)), ...Array.from({ length: 30 }, () => flat(20))];
    const grid = buildChipGrid(bars);
    expect(grid).not.toBeNull();
    const early = computeChipDistribution(bars, 29, grid!);
    const late = computeChipDistribution(bars, 59, grid!);
    expect(early!.chips.reduce((sum, value) => sum + value, 0)).toBeCloseTo(1);
    expect(early!.avgCost).toBeCloseTo(10, 0);
    // 30 days at 10% turnover leave ~4% of the old chips: cost moves near 20; with the close on the
    // peak roughly half of the new chips (plus all old ones) sit at or below it.
    expect(late!.avgCost).toBeGreaterThan(19);
    expect(late!.profitRatio).toBeGreaterThan(0.4);
    expect(late!.profitRatio).toBeLessThan(0.6);
    expect(late!.range90.low).toBeLessThan(late!.range90.high);
    expect(late!.range70.concentration).toBeLessThanOrEqual(late!.range90.concentration);
  });

  it('reports trapped chips after a drop', () => {
    const bars = [...Array.from({ length: 40 }, () => flat(20, 3)), flat(12, 3)];
    const grid = buildChipGrid(bars)!;
    const result = computeChipDistribution(bars, bars.length - 1, grid)!;
    expect(result.profitRatio).toBeLessThan(0.1);
    expect(bucketPrice(grid, 0)).toBeCloseTo(11.5);
  });

  it('returns null without turnover or for an out-of-range target', () => {
    const bars = [flat(10, null), flat(11, null)];
    const grid = buildChipGrid(bars)!;
    expect(computeChipDistribution(bars, 1, grid)).toBeNull();
    expect(computeChipDistribution(bars, 5, grid)).toBeNull();
  });
});

describe('quote targets', () => {
  it('maps index suggestions to API and watchlist codes', () => {
    const etf = targetFromSuggestion({
      canonicalCode: '510300.SH',
      displayCode: '510300',
      nameZh: '华泰柏瑞沪深300ETF',
      market: 'CN',
      assetType: 'etf',
      matchType: 'exact',
      matchField: 'code',
      score: 99,
    });
    expect(etf).toMatchObject({ code: '510300.SH', watchCode: '510300', assetType: 'etf' });

    const hk = targetFromSuggestion({
      canonicalCode: '00700.HK',
      displayCode: '00700',
      nameZh: '腾讯控股',
      market: 'HK',
      matchType: 'exact',
      matchField: 'code',
      score: 99,
    });
    expect(hk).toMatchObject({ code: '00700.HK', watchCode: '00700.HK', assetType: 'stock' });
  });

  it('accepts raw codes and rejects junk', () => {
    expect(targetFromRawCode(' 159915 ')).toMatchObject({ code: '159915', market: 'CN', assetType: 'etf' });
    expect(targetFromRawCode('aapl')).toMatchObject({ code: 'AAPL', market: 'US' });
    expect(targetFromRawCode('沪深300')).toBeNull();
    expect(targetFromRawCode('')).toBeNull();
  });
});
