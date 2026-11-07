import { describe, expect, it } from 'vitest';
import {
  buildStreamContext,
  contextKey,
  contextLabel,
  describeProgress,
  normalizeContext,
  suggestedQuestions,
} from '../assistantContext';

describe('assistant context helpers', () => {
  it('labels the three context kinds for the chip', () => {
    expect(contextLabel({ kind: 'board', boardId: 'industry:BK1', boardName: '半导体' })).toBe('当前：板块 半导体');
    expect(contextLabel({ kind: 'stock', stockCode: '000988', stockName: '华工科技', horizon: 'short' })).toBe(
      '当前：华工科技 · 短线',
    );
    expect(contextLabel({ kind: 'stock', stockCode: '000988', horizon: 'swing' })).toBe('当前：000988 · 波段');
    expect(contextLabel({ kind: 'none' })).toBe('未选中');
  });

  it('treats a context without its object as none', () => {
    expect(normalizeContext({ kind: 'board' }).kind).toBe('none');
    expect(normalizeContext({ kind: 'stock', stockCode: '' }).kind).toBe('none');
    expect(contextKey({ kind: 'board', boardId: null })).toBe('none');
  });

  it('builds the snake_case daily_plan block plus the classic stock fields', () => {
    expect(
      buildStreamContext(
        { kind: 'stock', stockCode: '000988', stockName: '华工科技', horizon: 'short', tradeDate: '2026-11-06' },
        '2026-11-05',
      ),
    ).toEqual({
      daily_plan: {
        trade_date: '2026-11-06',
        kind: 'stock',
        board_id: null,
        stock_code: '000988',
        horizon: 'short',
      },
      stock_code: '000988',
      stock_name: '华工科技',
    });
    expect(buildStreamContext({ kind: 'board', boardId: 'industry:BK1', boardName: '半导体' }, '2026-11-05')).toEqual({
      daily_plan: { trade_date: '2026-11-05', kind: 'board', board_id: 'industry:BK1', stock_code: null, horizon: null },
    });
    expect(buildStreamContext({ kind: 'none' }, null)).toEqual({
      daily_plan: { trade_date: null, kind: 'none', board_id: null, stock_code: null, horizon: null },
    });
  });

  it('offers 3-4 suggestions per kind and differs for holdings', () => {
    for (const context of [
      { kind: 'none' as const },
      { kind: 'board' as const, boardId: 'b' },
      { kind: 'stock' as const, stockCode: '000988' },
      { kind: 'stock' as const, stockCode: '000988', held: true },
    ]) {
      const count = suggestedQuestions(context).length;
      expect(count).toBeGreaterThanOrEqual(3);
      expect(count).toBeLessThanOrEqual(4);
    }
    expect(suggestedQuestions({ kind: 'stock', stockCode: '1', held: true })).toContain('现在加仓风险大吗');
    expect(suggestedQuestions({ kind: 'stock', stockCode: '1' })).not.toContain('现在加仓风险大吗');
  });

  it('describes progress for the pending bubble', () => {
    expect(describeProgress([])).toBe('正在连接…');
    expect(describeProgress([{ type: 'tool_start', display_name: '获取行情' }])).toBe('获取行情…');
    expect(describeProgress([{ type: 'generating' }])).toBe('正在生成回答…');
  });
});
