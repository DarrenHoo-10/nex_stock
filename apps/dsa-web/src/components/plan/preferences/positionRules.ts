import type { PositionRules } from '../../../types/dailyPlan';
import { parseNumber } from './format';
import type { ConfigView } from './usePreferencesConfig';

export type RuleKey =
  | 'POSITION_STOP_LOSS_PCT'
  | 'POSITION_MAX_TOTAL_PCT'
  | 'POSITION_MAX_HOLDINGS'
  | 'POSITION_MAX_SINGLE_PCT'
  | 'POSITION_STOP_MODE';

export const RULE_KEYS: RuleKey[] = [
  'POSITION_STOP_LOSS_PCT',
  'POSITION_MAX_TOTAL_PCT',
  'POSITION_MAX_HOLDINGS',
  'POSITION_MAX_SINGLE_PCT',
  'POSITION_STOP_MODE',
];

/**
 * Fallback for the reset button when the backend schema does not register a default yet
 * (core-product-spec 5.1: 10% stop, 100% total, 1 holding, single cap derived, fixed stop).
 */
const FALLBACK_DEFAULTS: Record<RuleKey, string> = {
  POSITION_STOP_LOSS_PCT: '10',
  POSITION_MAX_TOTAL_PCT: '100',
  POSITION_MAX_HOLDINGS: '1',
  POSITION_MAX_SINGLE_PCT: '',
  POSITION_STOP_MODE: 'fixed',
};

export type RuleValues = Record<RuleKey, string>;

/** Client-side checks before the request; the backend validates again. */
export function validateRules(values: RuleValues): Partial<Record<RuleKey, string>> {
  const errors: Partial<Record<RuleKey, string>> = {};
  const stop = parseNumber(values.POSITION_STOP_LOSS_PCT);
  if (stop === null || stop <= 0 || stop >= 100) {
    errors.POSITION_STOP_LOSS_PCT = '填 0 到 100 之间的数，例如 10';
  }
  const total = parseNumber(values.POSITION_MAX_TOTAL_PCT);
  if (total === null || total <= 0 || total > 100) {
    errors.POSITION_MAX_TOTAL_PCT = '填大于 0、不超过 100 的数，例如 100';
  }
  const holdings = parseNumber(values.POSITION_MAX_HOLDINGS);
  if (holdings === null || !Number.isInteger(holdings) || holdings < 1) {
    errors.POSITION_MAX_HOLDINGS = '填不小于 1 的整数，例如 1';
  }
  if (values.POSITION_MAX_SINGLE_PCT.trim() !== '') {
    const single = parseNumber(values.POSITION_MAX_SINGLE_PCT);
    if (single === null || single <= 0 || single > 100) {
      errors.POSITION_MAX_SINGLE_PCT = '留空，或填大于 0、不超过 100 的数';
    }
  }
  if (values.POSITION_STOP_MODE !== 'fixed' && values.POSITION_STOP_MODE !== 'nearer') {
    errors.POSITION_STOP_MODE = '请选择止损方式';
  }
  return errors;
}

export function serverValues(config: ConfigView, rules: PositionRules | null): RuleValues {
  const pick = (key: RuleKey, fromRules: string | undefined): string => {
    const item = config.get(key);
    if (item && item.value.trim() !== '') {
      return item.value.trim();
    }
    return fromRules ?? config.defaultOf(key) ?? FALLBACK_DEFAULTS[key];
  };
  return {
    POSITION_STOP_LOSS_PCT: pick('POSITION_STOP_LOSS_PCT', rules ? String(rules.stopLossPct) : undefined),
    POSITION_MAX_TOTAL_PCT: pick('POSITION_MAX_TOTAL_PCT', rules ? String(rules.maxTotalPct) : undefined),
    POSITION_MAX_HOLDINGS: pick('POSITION_MAX_HOLDINGS', rules ? String(rules.maxHoldings) : undefined),
    // Empty is meaningful here: derive from total / holdings.
    POSITION_MAX_SINGLE_PCT: (() => {
      const item = config.get('POSITION_MAX_SINGLE_PCT');
      if (item && item.value.trim() !== '') {
        return item.value.trim();
      }
      return rules && !rules.maxSinglePctDerived ? String(rules.maxSinglePct) : '';
    })(),
    POSITION_STOP_MODE: pick('POSITION_STOP_MODE', rules?.stopMode),
  };
}

export function resetValues(config: ConfigView): RuleValues {
  const values = { ...FALLBACK_DEFAULTS };
  for (const key of RULE_KEYS) {
    const schemaDefault = config.defaultOf(key);
    if (schemaDefault !== undefined) {
      values[key] = schemaDefault;
    }
  }
  return values;
}
