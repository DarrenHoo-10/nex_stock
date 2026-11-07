import type { AccountResponse, Holding, HoldingInput, HoldingsSnapshotRequest, Horizon } from '../../../types/dailyPlan';

/** One editable row of the 「持仓与资金」 dialog; numbers stay text until validated. */
export interface HoldingDraftRow {
  /** Stable React key. */
  key: string;
  code: string;
  /** Read-only hint from the account (the backend resolves names; the request has no name field). */
  name: string;
  shares: string;
  cost: string;
  buyDate: string;
  horizon: '' | Horizon;
}

export type HoldingField = 'code' | 'shares' | 'cost' | 'buyDate';

export interface DraftErrors {
  cash?: string;
  /** First error of each row, by row key. */
  rows: Record<string, { field: HoldingField; message: string }>;
  /** Cross-row problem (duplicate codes). */
  form?: string;
}

let rowCounter = 0;

export function newRowKey(): string {
  rowCounter += 1;
  return `holding-row-${rowCounter}`;
}

export function emptyRow(): HoldingDraftRow {
  return { key: newRowKey(), code: '', name: '', shares: '', cost: '', buyDate: '', horizon: '' };
}

export function draftFromAccount(account: AccountResponse | null): { cash: string; rows: HoldingDraftRow[] } {
  if (!account) {
    return { cash: '', rows: [] };
  }
  return {
    cash: String(account.cash),
    rows: account.holdings.map((holding) => ({
      key: newRowKey(),
      code: holding.code,
      name: holding.name,
      shares: String(holding.shares),
      cost: String(holding.cost),
      buyDate: holding.buyDate ?? '',
      horizon: holding.horizon ?? '',
    })),
  };
}

/** "1,184.5" / "1184.5" -> 1184.5; NaN when blank or not a number. */
export function parseNumber(text: string): number {
  const cleaned = text.replace(/[,，\s]/g, '');
  return cleaned === '' ? Number.NaN : Number(cleaned);
}

const CODE_PATTERN = /^\d{6}$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Client-side checks only: 6-digit code, positive whole shares, positive cost, valid date, no duplicate
 * codes. Lot size, max holdings and cash rules are validated by the server (400/422 shown inline).
 */
export function validateDraft(cashText: string, rows: HoldingDraftRow[]): DraftErrors {
  const errors: DraftErrors = { rows: {} };
  const cash = parseNumber(cashText);
  if (!(cash >= 0)) {
    errors.cash = '可用现金要填 0 或正数。';
  }
  rows.forEach((row, index) => {
    const label = `第 ${index + 1} 条持仓`;
    const shares = parseNumber(row.shares);
    const cost = parseNumber(row.cost);
    if (!CODE_PATTERN.test(row.code.trim())) {
      errors.rows[row.key] = { field: 'code', message: `${label}：股票代码要填 6 位数字。` };
    } else if (!(Number.isInteger(shares) && shares > 0)) {
      errors.rows[row.key] = { field: 'shares', message: `${label}：数量要填大于 0 的整数。` };
    } else if (!(cost > 0)) {
      errors.rows[row.key] = { field: 'cost', message: `${label}：成本价要大于 0。` };
    } else if (row.buyDate && !DATE_PATTERN.test(row.buyDate)) {
      errors.rows[row.key] = { field: 'buyDate', message: `${label}：买入日期格式不对。` };
    }
  });
  const codes = rows.map((row) => row.code.trim()).filter((code) => CODE_PATTERN.test(code));
  if (new Set(codes).size !== codes.length) {
    errors.form = '同一只股票只录一条，请合并数量和成本。';
  }
  return errors;
}

export function hasErrors(errors: DraftErrors): boolean {
  return Boolean(errors.cash || errors.form || Object.keys(errors.rows).length > 0);
}

/** Valid drafts only (call after `validateDraft` passed). */
export function snapshotFromDraft(cashText: string, rows: HoldingDraftRow[]): HoldingsSnapshotRequest {
  return {
    cash: parseNumber(cashText),
    holdings: rows.map((row) => ({
      code: row.code.trim(),
      shares: parseNumber(row.shares),
      cost: parseNumber(row.cost),
      buyDate: row.buyDate || null,
      horizon: row.horizon || null,
    })),
  };
}

function toInput(holding: Holding): HoldingInput {
  return {
    code: holding.code,
    shares: holding.shares,
    cost: holding.cost,
    buyDate: holding.buyDate ?? null,
    horizon: holding.horizon ?? null,
  };
}

export interface RecordedBuy {
  code: string;
  shares: number;
  price: number;
  buyDate: string | null;
  horizon: Horizon | null;
}

const round = (value: number, decimals: number) => Number(value.toFixed(decimals));

/**
 * Snapshot for 「记录买入」: keeps every current holding, appends the buy (or merges into an existing row with a
 * weighted average cost) and takes the paid amount out of cash. Returns `null` when cash is not enough.
 */
export function mergeRecordedBuy(
  account: Pick<AccountResponse, 'cash' | 'holdings'>,
  buy: RecordedBuy,
): { snapshot: HoldingsSnapshotRequest | null; amount: number; shortfall: number } {
  const amount = round(buy.shares * buy.price, 2);
  const cashLeft = round(account.cash - amount, 2);
  if (cashLeft < 0) {
    return { snapshot: null, amount, shortfall: round(-cashLeft, 2) };
  }
  const existing = account.holdings.map(toInput);
  const index = existing.findIndex((holding) => holding.code === buy.code);
  let holdings: HoldingInput[];
  if (index >= 0) {
    const current = existing[index];
    const totalShares = current.shares + buy.shares;
    const merged: HoldingInput = {
      ...current,
      shares: totalShares,
      cost: round((current.shares * current.cost + buy.shares * buy.price) / totalShares, 4),
      // The most recent buy decides T+1.
      buyDate: buy.buyDate ?? current.buyDate,
      horizon: buy.horizon ?? current.horizon,
    };
    holdings = existing.map((holding, i) => (i === index ? merged : holding));
  } else {
    holdings = [
      ...existing,
      { code: buy.code, shares: buy.shares, cost: buy.price, buyDate: buy.buyDate, horizon: buy.horizon },
    ];
  }
  return { snapshot: { cash: cashLeft, holdings }, amount, shortfall: 0 };
}
