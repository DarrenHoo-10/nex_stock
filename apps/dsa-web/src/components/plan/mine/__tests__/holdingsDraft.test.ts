import { describe, expect, it } from 'vitest';
import type { AccountResponse } from '../../../../types/dailyPlan';
import {
  draftFromAccount,
  emptyRow,
  hasErrors,
  mergeRecordedBuy,
  snapshotFromDraft,
  validateDraft,
} from '../holdingsDraft';

const account = {
  cash: 1184,
  holdings: [
    { code: '002475', shares: 2400, cost: 40.2, buyDate: '2026-10-28', horizon: 'swing', name: '立讯精密' },
  ],
} as unknown as AccountResponse;

describe('validateDraft', () => {
  it('accepts the account as is', () => {
    const draft = draftFromAccount(account);
    expect(hasErrors(validateDraft(draft.cash, draft.rows))).toBe(false);
  });

  it('flags a bad cash value, code, shares, cost and duplicates', () => {
    expect(validateDraft('-1', []).cash).toBeTruthy();
    expect(validateDraft('', []).cash).toBeTruthy();
    const row = { ...emptyRow(), code: '12', shares: '1', cost: '1' };
    expect(validateDraft('0', [row]).rows[row.key].field).toBe('code');
    const zero = { ...emptyRow(), code: '000988', shares: '0', cost: '1' };
    expect(validateDraft('0', [zero]).rows[zero.key].field).toBe('shares');
    const frac = { ...emptyRow(), code: '000988', shares: '1.5', cost: '1' };
    expect(validateDraft('0', [frac]).rows[frac.key].field).toBe('shares');
    const noCost = { ...emptyRow(), code: '000988', shares: '100', cost: '0' };
    expect(validateDraft('0', [noCost]).rows[noCost.key].field).toBe('cost');
    const a = { ...emptyRow(), code: '000988', shares: '100', cost: '1' };
    const b = { ...emptyRow(), code: '000988', shares: '200', cost: '2' };
    expect(validateDraft('0', [a, b]).form).toContain('同一只股票');
  });

  it('builds the snapshot with numbers and nulls', () => {
    const row = { ...emptyRow(), code: ' 000988', shares: '2,100', cost: '47', buyDate: '', horizon: '' as const };
    expect(snapshotFromDraft('1,300.5', [row])).toEqual({
      cash: 1300.5,
      holdings: [{ code: '000988', shares: 2100, cost: 47, buyDate: null, horizon: null }],
    });
  });
});

describe('mergeRecordedBuy', () => {
  it('appends the buy, keeps the others and takes the amount out of cash', () => {
    const result = mergeRecordedBuy(account, { code: '000988', shares: 20, price: 47, buyDate: '2026-11-09', horizon: 'short' });
    expect(result.snapshot).toEqual({
      cash: 244,
      holdings: [
        { code: '002475', shares: 2400, cost: 40.2, buyDate: '2026-10-28', horizon: 'swing' },
        { code: '000988', shares: 20, cost: 47, buyDate: '2026-11-09', horizon: 'short' },
      ],
    });
    expect(result.amount).toBe(940);
  });

  it('merges into an existing row with a weighted cost', () => {
    const result = mergeRecordedBuy(
      { ...account, cash: 100000 },
      { code: '002475', shares: 100, price: 42.2, buyDate: null, horizon: null },
    );
    expect(result.snapshot?.holdings).toEqual([
      { code: '002475', shares: 2500, cost: 40.28, buyDate: '2026-10-28', horizon: 'swing' },
    ]);
  });

  it('refuses when the cash is not enough', () => {
    const result = mergeRecordedBuy(account, { code: '000988', shares: 100, price: 47, buyDate: null, horizon: null });
    expect(result.snapshot).toBeNull();
    expect(result.shortfall).toBe(3516);
  });
});
