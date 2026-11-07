import type { ScreeningCandidate, ScreeningStrategy } from '../../../api/screening';

export type PickHorizon = 'short' | 'swing';
export type HorizonFilter = 'all' | PickHorizon;

export const HORIZON_LABEL: Record<PickHorizon, string> = { short: '短线', swing: '波段' };
export const HORIZON_SPAN: Record<PickHorizon, string> = { short: '1–5 天', swing: '2–4 周' };

/** A candidate prepared for display. Optional contract fields fall back to the existing screening fields. */
export interface PickView {
  code: string;
  name: string;
  price: number | null;
  changePct: number | null;
  /** Empty when the backend did not send `horizons`. */
  horizons: PickHorizon[];
  reasons: string[];
  entryCondition: string | null;
  invalidCondition: string | null;
  risks: string[];
  note: string | null;
}

function asStringList(value: unknown): string[] {
  if (typeof value === 'string') return value.trim() ? [value.trim()] : [];
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim() !== '').map((item) => item.trim());
}

function asText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function asHorizons(value: unknown): PickHorizon[] {
  if (!Array.isArray(value)) return [];
  const result: PickHorizon[] = [];
  value.forEach((item) => {
    if ((item === 'short' || item === 'swing') && !result.includes(item)) result.push(item);
  });
  return result;
}

function finiteOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/**
 * Picks come back as untyped dicts: `horizons`, `entry_condition`, `invalid_condition`, `risks` and `note` are
 * optional additions of the daily-plan contract (section 4). The base fields (reason, watch items,
 * invalidators, risks) are the fallback so the card is useful without them.
 */
export function toPickView(candidate: ScreeningCandidate): PickView {
  const extra = candidate as ScreeningCandidate & Record<string, unknown>;
  const raw = (candidate.raw ?? {}) as Record<string, unknown>;
  const pick = (key: string): unknown => extra[key] ?? raw[key];

  const selected = (candidate.whySelected ?? [])
    .map((item) => item.text)
    .filter((text): text is string => typeof text === 'string' && text.trim() !== '');
  const reasons = selected.length ? selected : asStringList(candidate.reason);

  const watch = asStringList(candidate.llmWatchItems);
  const invalidators = asStringList(candidate.llmInvalidators);
  const riskList = asStringList(pick('risks'));
  const fallbackRisks = asStringList(candidate.llmRisks);
  const risks = riskList.length
    ? riskList
    : fallbackRisks.length
      ? fallbackRisks
      : asStringList(candidate.riskSummary);

  return {
    code: candidate.code,
    name: candidate.name || candidate.code,
    price: finiteOrNull(candidate.price),
    changePct: finiteOrNull(candidate.changePct),
    horizons: asHorizons(pick('horizons')),
    reasons,
    entryCondition: asText(pick('entryCondition')) ?? (watch.length ? watch.join('；') : null),
    invalidCondition: asText(pick('invalidCondition')) ?? (invalidators.length ? invalidators.join('；') : null),
    risks,
    note: asText(pick('note')),
  };
}

/** Candidates without `horizons` stay visible under every horizon filter. */
export function matchesHorizon(pick: PickView, filter: HorizonFilter): boolean {
  return filter === 'all' || pick.horizons.length === 0 || pick.horizons.includes(filter);
}

export function strategyLabel(strategy: ScreeningStrategy): string {
  return strategy.title || strategy.name || strategy.id;
}
