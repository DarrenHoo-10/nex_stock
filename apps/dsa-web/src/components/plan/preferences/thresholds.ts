import { getBoardThresholds, putBoardThresholds } from '../../../api/boards';
import { getParsedApiError } from '../../../api/error';
import type { BoardThresholdOverrides, Evidence, ThresholdItem, ThresholdValue } from '../../../types/boards';
import { parseNumber } from './format';

/** Evidence grade (core-product-spec 3.2) with the plain explanation shown next to it. */
export const EVIDENCE_INFO: Record<Evidence, { label: string; explain: string }> = {
  medium: { label: '中', explain: '有多个来源支持的通行口径' },
  low: { label: '低', explain: '单一来源或经验说法' },
  inferred: { label: '推断', explain: '没有直接来源，由相近口径推导' },
};

const EVIDENCE_SET = new Set<string>(['medium', 'low', 'inferred']);

/** GET /boards/thresholds, with missing or unexpected fields normalised for display. */
export async function loadThresholds(): Promise<ThresholdItem[]> {
  const rows = (await getBoardThresholds()) as unknown as Array<Record<string, unknown>>;
  return rows.map((raw) => ({
    key: String(raw.key ?? ''),
    label: String(raw.label ?? raw.key ?? ''),
    value: raw.value as ThresholdValue,
    default: raw.default as ThresholdValue,
    display: String(raw.display ?? ''),
    unit: raw.unit == null || raw.unit === '' ? null : String(raw.unit),
    evidence: (EVIDENCE_SET.has(String(raw.evidence)) ? raw.evidence : 'inferred') as Evidence,
    note: raw.note == null || raw.note === '' ? null : String(raw.note),
    overridden: Boolean(raw.overridden),
  }));
}

export const saveThresholdOverrides = putBoardThresholds;

export interface GroupDef {
  id: string;
  title: string;
  test: RegExp;
}

/** The contract has no group field; keys are grouped by prefix, unknown keys fall into 「其他」. */
export const THRESHOLD_GROUPS: GroupDef[] = [
  { id: 'volume', title: '放量与超额排名', test: /^(amount_ratio|strong_rank|weak_rank|excess_floor|benchmark)/ },
  { id: 'breadth', title: '上涨家数与涨停', test: /^(breadth|limit_up|broken_rate|mainline)/ },
  { id: 'persist', title: '持续与退潮', test: /^(persist|ebb|divergence)/ },
  { id: 'market', title: '市场环境', test: /^(broad_rally)/ },
  { id: 'concept', title: '板块筛选与合并', test: /^(cooling|merge|min_|max_|single)/ },
];

export function groupThresholds(items: ThresholdItem[]): Array<{ id: string; title: string; items: ThresholdItem[] }> {
  const groups = THRESHOLD_GROUPS.map((group) => ({
    id: group.id,
    title: group.title,
    items: items.filter((item) => group.test.test(item.key)),
  }));
  const rest = items.filter((item) => !THRESHOLD_GROUPS.some((group) => group.test.test(item.key)));
  return [...groups, { id: 'other', title: '其他', items: rest }].filter((group) => group.items.length > 0);
}

export function isNumericThreshold(item: ThresholdItem): boolean {
  return typeof item.value === 'number' && typeof item.default === 'number';
}

/** Current form text of a numeric item (`edits` wins over the saved value). */
export function currentText(item: ThresholdItem, edits: Record<string, string>): string {
  return edits[item.key] ?? String(item.value);
}

export function sameValue(left: ThresholdValue, right: ThresholdValue): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

/**
 * Overrides to PUT: every item whose effective value differs from its default after the edits. Numeric items
 * edited back to the default drop out (that is how a single reset is saved); overrides of items this form
 * cannot edit are carried over unchanged, because PUT replaces the whole override object.
 */
export function buildOverrides(items: ThresholdItem[], edits: Record<string, string>): BoardThresholdOverrides {
  const overrides: BoardThresholdOverrides = {};
  for (const item of items) {
    if (isNumericThreshold(item)) {
      const parsed = parseNumber(currentText(item, edits));
      const value = parsed ?? (item.value as number);
      if (!sameValue(value, item.default)) {
        overrides[item.key] = value;
      }
    } else if (!sameValue(item.value, item.default)) {
      overrides[item.key] = item.value;
    }
  }
  return overrides;
}

/** Keys with an unsaved numeric edit. */
export function editedKeys(items: ThresholdItem[], edits: Record<string, string>): string[] {
  return items
    .filter((item) => isNumericThreshold(item) && item.key in edits && parseNumber(edits[item.key]) !== item.value)
    .map((item) => item.key);
}

/** Texts that failed client-side validation (empty / not a number) by key. */
export function invalidNumbers(items: ThresholdItem[], edits: Record<string, string>): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const item of items) {
    if (isNumericThreshold(item) && item.key in edits && parseNumber(edits[item.key]) === null) {
      errors[item.key] = '请填数字';
    }
  }
  return errors;
}

/** User-facing lines of a failed PUT: the 400 `detail` list, else the message; 409 and 503 are classified. */
export function describeThresholdError(error: unknown): {
  kind: 'validation' | 'conflict' | 'unavailable' | 'error';
  lines: string[];
} {
  const parsed = getParsedApiError(error);
  const response = (error as { response?: { status?: number; data?: Record<string, unknown> } } | null)?.response;
  const data = response?.data;
  const status = response?.status ?? parsed.status;
  if (status === 409) {
    return { kind: 'conflict', lines: [parsed.message] };
  }
  if (status === 503) {
    return { kind: 'unavailable', lines: ['次日计划服务未就绪，暂时不能保存阈值。'] };
  }
  const detail = data && typeof data === 'object' ? (data.detail as unknown) : undefined;
  const nested =
    detail && typeof detail === 'object' && !Array.isArray(detail)
      ? ((detail as Record<string, unknown>).detail as unknown)
      : undefined;
  const list = Array.isArray(detail) ? detail : Array.isArray(nested) ? nested : null;
  if (status === 400 && list && list.length > 0) {
    return {
      kind: 'validation',
      lines: list.map((entry) =>
        typeof entry === 'string' ? entry : String((entry as { message?: unknown }).message ?? JSON.stringify(entry)),
      ),
    };
  }
  return { kind: status === 400 ? 'validation' : 'error', lines: [parsed.message] };
}
