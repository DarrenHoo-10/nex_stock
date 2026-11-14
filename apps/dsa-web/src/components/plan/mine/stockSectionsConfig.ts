export const sections = [
  { value: 'analysis', label: '个股分析' }, { value: 'plan', label: '明日计划' },
  { value: 'research', label: '研报' }, { value: 'events', label: '重大事件' },
  { value: 'news', label: '相关新闻' },
] as const;
export type StockSection = typeof sections[number]['value'];
export function stockSection(value: string | null): StockSection {
  return sections.find(s => s.value === value)?.value ?? 'analysis';
}
