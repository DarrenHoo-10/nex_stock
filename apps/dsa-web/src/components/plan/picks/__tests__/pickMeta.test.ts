import { describe, expect, it } from 'vitest';
import type { ScreeningCandidate } from '../../../../api/screening';
import { matchesHorizon, toPickView } from '../pickMeta';

function candidate(extra: Record<string, unknown> = {}): ScreeningCandidate {
  return { rank: 1, code: '000988', name: '华工科技', reason: '板块首次启动', raw: {}, ...extra } as ScreeningCandidate;
}

describe('toPickView', () => {
  it('uses the optional contract fields when the backend sends them', () => {
    const pick = toPickView(
      candidate({
        price: 48.35,
        changePct: 4.21,
        horizons: ['short', 'swing', 'bogus'],
        entryCondition: '回踩 47 元企稳',
        invalidCondition: '收盘跌破 45.5 元',
        risks: ['板块只启动 1 天'],
        note: '只列出不建议追',
        llmWatchItems: ['should not win'],
      }),
    );
    expect(pick.horizons).toEqual(['short', 'swing']);
    expect(pick.entryCondition).toBe('回踩 47 元企稳');
    expect(pick.invalidCondition).toBe('收盘跌破 45.5 元');
    expect(pick.risks).toEqual(['板块只启动 1 天']);
    expect(pick.note).toBe('只列出不建议追');
    expect(pick.price).toBe(48.35);
  });

  it('falls back to reasons, watch items, invalidators and the risk summary', () => {
    const pick = toPickView(
      candidate({
        llmWatchItems: ['开板后能否回封', '量能放大'],
        llmInvalidators: ['放量跌破 5%'],
        riskSummary: '封板不稳',
      }),
    );
    expect(pick.horizons).toEqual([]);
    expect(pick.reasons).toEqual(['板块首次启动']);
    expect(pick.entryCondition).toBe('开板后能否回封；量能放大');
    expect(pick.invalidCondition).toBe('放量跌破 5%');
    expect(pick.risks).toEqual(['封板不稳']);
    expect(pick.note).toBeNull();
  });

  it('prefers the structured selection reasons and tolerates a bare candidate', () => {
    const structured = toPickView(
      candidate({ whySelected: [{ code: 'a', text: '放量 2.6 倍', source: 'x', quality: 'observed' }] }),
    );
    expect(structured.reasons).toEqual(['放量 2.6 倍']);

    const bare = toPickView({ rank: 2, code: '600000', name: '', reason: '', raw: {} } as ScreeningCandidate);
    expect(bare.name).toBe('600000');
    expect(bare.reasons).toEqual([]);
    expect(bare.entryCondition).toBeNull();
    expect(bare.risks).toEqual([]);
  });

  it('reads optional fields from raw when they are not on the candidate', () => {
    const pick = toPickView(candidate({ raw: { horizons: ['swing'], note: '备注' } }));
    expect(pick.horizons).toEqual(['swing']);
    expect(pick.note).toBe('备注');
  });
});

describe('matchesHorizon', () => {
  it('keeps picks without horizons visible under every filter', () => {
    const unknown = toPickView(candidate());
    const short = toPickView(candidate({ horizons: ['short'] }));
    expect(matchesHorizon(unknown, 'swing')).toBe(true);
    expect(matchesHorizon(short, 'swing')).toBe(false);
    expect(matchesHorizon(short, 'short')).toBe(true);
    expect(matchesHorizon(short, 'all')).toBe(true);
  });
});
