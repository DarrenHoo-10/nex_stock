/** Colors mirror the plan-glass tokens (dark glass only); red is up, teal is down. */
export const CHART_COLORS = {
  up: '#FF7468',
  down: '#3CC3B4',
  text: 'rgba(255,255,255,.72)',
  faint: 'rgba(255,255,255,.45)',
  grid: 'rgba(255,255,255,.08)',
  cross: 'rgba(255,255,255,.55)',
  crossLine: 'rgba(255,255,255,.82)',
  crossLabel: '#3D6BFF',
  labelBg: 'rgba(20,24,56,.92)',
  chipProfit: 'rgba(255,116,104,.78)',
  chipTrapped: 'rgba(127,184,255,.72)',
  avgCost: '#FFCF6E',
  ma: ['#F5F7FF', '#FFCF6E', '#FF8AD8', '#7FB8FF'],
  volMa: ['#F5F7FF', '#FFCF6E'],
} as const;
