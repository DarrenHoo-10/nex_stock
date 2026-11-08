import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import type { Sizing } from '../../../../types/dailyPlan';
import { layoutLadder } from '../ladderLayout';
import { PriceLadder } from '../PriceLadder';
import { SizingBlock } from '../SizingBlock';

const levels = [
  { kind: 'take_profit' as const, label: '止盈', price: 52 },
  { kind: 'close' as const, label: '收盘', price: 48.35 },
  { kind: 'entry' as const, label: '买入区', price: 47, band: [46.8, 47.2] as [number, number] },
  { kind: 'support' as const, label: '平台', price: 46.9 },
  { kind: 'stop' as const, label: '止损', price: 42.3 },
  { kind: 'limit_down' as const, label: '明天跌停价', price: 43.52 },
];

describe('layoutLadder', () => {
  it('orders top-down and keeps label spacing', () => {
    const { items, height } = layoutLadder(levels);
    for (let i = 1; i < items.length; i += 1) {
      expect(items[i].y).toBeGreaterThanOrEqual(items[i - 1].y);
      expect(items[i].ly - items[i - 1].ly).toBeGreaterThanOrEqual(35.99);
    }
    expect(items[0].price).toBe(52);
    expect(items[items.length - 1].ly).toBeLessThanOrEqual(height);
    const band = items.find((item) => item.band);
    expect(band?.y2).toBeGreaterThan(band?.y1 ?? 0);
  });

  it('copes with a single level and with none', () => {
    expect(layoutLadder([{ kind: 'close', label: '收盘', price: 10 }]).items).toHaveLength(1);
    expect(layoutLadder([]).items).toHaveLength(0);
  });
});

describe('PriceLadder', () => {
  it('is an image with a text list of every level', () => {
    render(<PriceLadder levels={levels} caption="按 47.00 买入时的价位" />);
    expect(screen.getByRole('img', { name: '价格梯子' })).toBeInTheDocument();
    const list = screen.getByRole('list', { hidden: true });
    const texts = within(list)
      .getAllByRole('listitem', { hidden: true })
      .map((item) => item.textContent);
    expect(texts).toEqual([
      '止盈 52.00',
      '收盘 48.35',
      '买入区 46.80–47.20',
      '平台 46.90',
      '明天跌停价 43.52',
      '止损 42.30',
    ]);
  });
});

const baseSizing: Sizing = {
  status: 'ok',
  message: null,
  price: 47,
  lot: 100,
  byTotal: 2127.66,
  bySingle: 2127.66,
  byCash: 2127.66,
  rawShares: 2127.66,
  shares: 2100,
  amount: 98700,
  capitalPct: 98.7,
  postPositionPct: 98.7,
  stop: { price: 42.3, mode: 'fixed', formula: '47.00 × 0.90', techSupport: 46.9, warning: null },
  maxLoss: 9870,
  limitDownPrice: 38.07,
  limitDownLoss: 18753,
  steps: ['取最小值 2,127，按每手 100 股向下取整 = 2,100 股'],
};

function renderSizing(sizing: Sizing) {
  return render(
    <UiLanguageProvider>
      <SizingBlock
        sizing={sizing}
        marketText="主板"
        limitPct={10}
        heldBlocker={{ code: '002475', name: '立讯精密' }}
        onSelectStock={() => {}}
      />
    </UiLanguageProvider>,
  );
}

describe('SizingBlock', () => {
  it('shows the backend numbers and the steps', () => {
    renderSizing(baseSizing);
    const group = screen.getByRole('group', { name: '买入股数' });
    expect(group).toHaveTextContent('98,700 元');
    expect(group).toHaveTextContent('9,870 元');
    expect(group).toHaveTextContent('18,753 元');
    expect(screen.getByText('取最小值 2,127，按每手 100 股向下取整 = 2,100 股')).toBeInTheDocument();
  });

  it('explains max_holdings, below_lot and no_price', () => {
    const first = renderSizing({ ...baseSizing, status: 'max_holdings', message: '已持有 1/1 只', shares: 0, steps: [] });
    expect(screen.getByText('不新开仓')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '看换股判断' })).toBeInTheDocument();
    first.unmount();
    const second = renderSizing({ ...baseSizing, status: 'below_lot', message: null, shares: 0 });
    expect(screen.getByText('不建议买入')).toBeInTheDocument();
    expect(screen.getByText(/不够一手/)).toBeInTheDocument();
    second.unmount();
    renderSizing({ ...baseSizing, status: 'no_price', message: null, shares: 0, steps: [] });
    expect(screen.getByText('没有买入价')).toBeInTheDocument();
  });
});
