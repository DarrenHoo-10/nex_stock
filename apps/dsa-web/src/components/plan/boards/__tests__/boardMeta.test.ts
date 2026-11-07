import { describe, expect, it } from 'vitest';
import {
  formatPct,
  formatRatio,
  meterPercent,
  metricTone,
  shortDate,
  signClass,
} from '../boardMeta';

describe('boardMeta', () => {
  it('formats percentages with an explicit plus sign and a dash for missing values', () => {
    expect(formatPct(3.864)).toBe('+3.86%');
    expect(formatPct(-1.35)).toBe('-1.35%');
    expect(formatPct(0)).toBe('0.00%');
    expect(formatPct(null)).toBe('—');
    expect(formatPct(82, 0, false)).toBe('82%');
  });

  it('uses red for up and green for down (A-share convention)', () => {
    expect(signClass(1)).toBe('up');
    expect(signClass(-0.1)).toBe('down');
    expect(signClass(0)).toBe('flat');
    expect(signClass(null)).toBe('flat');
  });

  it('shows 无基准 when there is no turnover baseline', () => {
    expect(formatRatio(2.66)).toBe('2.7×');
    expect(formatRatio(null)).toBe('无基准');
  });

  it('keeps the turnover meter inside 0-3x', () => {
    expect(meterPercent(1.5)).toBe(50);
    expect(meterPercent(9)).toBe(100);
    expect(meterPercent(-1)).toBe(0);
    expect(meterPercent(null)).toBe(0);
  });

  it('derives the tone of preformatted metrics', () => {
    expect(metricTone('+3.86%')).toBe('up');
    expect(metricTone('-1.2%')).toBe('down');
    expect(metricTone('2.7×')).toBe('');
    expect(metricTone('行业不看')).toBe('');
  });

  it('shortens trade dates', () => {
    expect(shortDate('2026-11-06')).toBe('11-06');
    expect(shortDate('unknown')).toBe('unknown');
  });
});
