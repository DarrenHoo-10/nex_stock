import { describe, expect, it } from 'vitest';
import { formatScreenMessage, formatScreenMessages } from '../screeningMessages';

describe('screening engine notes', () => {
  it('hides internal notes and translates known ones', () => {
    expect(formatScreenMessage('DSA provider context applied 3 of 3 candidates')).toBe('');
    expect(formatScreenMessage('LLM ranking skipped: no LLM config')).toBe('未配置智能重排模型，当前使用确定性因子排序。');
    expect(formatScreenMessage('Snapshot source fallback: tushare: Read timed out')).toBe('数据源降级：tushare（请求超时）');
  });

  it('formats a list, dropping empty, internal and duplicate notes', () => {
    expect(formatScreenMessages([
      'DSA provider context applied 3 of 3 candidates',
      '  ',
      'LLM ranking skipped: no LLM config',
      'LLM ranking skipped: no LLM config',
    ])).toEqual(['未配置智能重排模型，当前使用确定性因子排序。']);
    expect(formatScreenMessages(undefined)).toEqual([]);
  });
});
