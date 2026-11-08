import type {
  BoardDriverKind,
  BoardRepresentativeRole,
  BoardState,
  BoardTag,
  BoardType,
} from '../../../types/boards';

export const BOARD_STATE_LABEL: Record<BoardState, string> = {
  first_launch: '首次启动',
  strengthening: '持续走强',
  divergence: '高位分歧',
  ebbing: '退潮',
  abnormal_drop: '异常下跌',
  insufficient_history: '历史数据不足',
  none: '未异动',
};

/** States offered as filter chips, in the order of the prototype. */
export const BOARD_FILTER_STATES: BoardState[] = [
  'first_launch',
  'strengthening',
  'divergence',
  'ebbing',
  'abnormal_drop',
  'insufficient_history',
];

export const BOARD_TAG_LABEL: Record<BoardTag, string> = {
  mainline: '主线',
  supported: '有承接',
  single_stock: '单股驱动',
  cooling: '冷却',
  merged: '已合并',
};

/** Tags that explain why a board is not a normal opportunity are drawn in the warning tone. */
export const BOARD_WARN_TAGS: ReadonlySet<BoardTag> = new Set<BoardTag>(['single_stock', 'cooling']);

export const BOARD_TYPE_LABEL: Record<BoardType, string> = { industry: '行业', concept: '概念' };

export const DRIVER_KIND_LABEL: Record<BoardDriverKind, string> = {
  news: '消息驱动',
  capital: '资金驱动',
  earnings: '业绩驱动',
  unknown: '原因未明',
};

export const REPRESENTATIVE_ROLE_LABEL: Record<BoardRepresentativeRole, string> = {
  leader: '龙头',
  turnover: '成交额',
  follower: '跟风',
};

export function isNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** Red up, green down (A-share convention). */
export function signClass(value: number | null | undefined): string {
  if (!isNumber(value) || value === 0) return 'flat';
  return value > 0 ? 'up' : 'down';
}

export function formatPct(value: number | null | undefined, digits = 2, sign = true): string {
  if (!isNumber(value)) return '—';
  const text = value.toFixed(digits);
  return sign && value > 0 ? `+${text}%` : `${text}%`;
}

export function formatSigned(value: number | null | undefined, digits = 2): string {
  if (!isNumber(value)) return '—';
  const text = value.toFixed(digits);
  return value > 0 ? `+${text}` : text;
}

export function formatRatio(value: number | null | undefined): string {
  return isNumber(value) ? `${value.toFixed(1)}×` : '无基准';
}

/** Tone of a preformatted metric such as "+3.86%" or "-1.2%"; other strings stay neutral. */
export function metricTone(value: string): string {
  if (/^\+\d/.test(value)) return 'up';
  if (/^[-−]\d/.test(value)) return 'down';
  return '';
}

/** "2026-11-06" -> "11-06". */
export function shortDate(tradeDate: string): string {
  return /^\d{4}-\d{2}-\d{2}/.test(tradeDate) ? tradeDate.slice(5, 10) : tradeDate;
}

/** Position of a turnover ratio on the 0-3x meter used by the list rows. */
export function meterPercent(ratio: number | null | undefined): number {
  if (!isNumber(ratio)) return 0;
  return (Math.min(Math.max(ratio, 0), 3) / 3) * 100;
}
