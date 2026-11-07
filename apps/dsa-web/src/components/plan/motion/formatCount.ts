const formatters = new Map<number, Intl.NumberFormat>();

/** Fixed-decimals formatter with thousands separators ("en-US", like the prototype). */
export function formatCount(value: number, decimals = 0, sign = false): string {
  let formatter = formatters.get(decimals);
  if (!formatter) {
    formatter = new Intl.NumberFormat('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    formatters.set(decimals, formatter);
  }
  const text = formatter.format(value);
  return sign && value > 0 ? `+${text}` : text;
}
