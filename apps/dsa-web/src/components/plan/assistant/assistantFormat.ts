/** 12345 -> "12.3k". */
export function formatTokens(value: number | null | undefined): string {
  if (value == null) {
    return '—';
  }
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}M`;
  }
  if (value >= 1000) {
    return `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  }
  return String(value);
}

/** "刚刚" / "5 分钟前" / "3 小时前" / "11-06". */
export function relativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) {
    return '';
  }
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) {
    return '';
  }
  const minutes = Math.floor((now - time) / 60_000);
  if (minutes < 1) {
    return '刚刚';
  }
  if (minutes < 60) {
    return `${minutes} 分钟前`;
  }
  if (minutes < 24 * 60) {
    return `${Math.floor(minutes / 60)} 小时前`;
  }
  return iso.slice(5, 10);
}
