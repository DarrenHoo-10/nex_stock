import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { KlineBarTip } from '../KlineBarTip';

describe('KlineBarTip', () => {
  it('shows the candle values colored against the previous close', () => {
    const { container } = render(
      <UiLanguageProvider>
        <KlineBarTip
          bar={{
            date: '2026-09-30',
            open: 31.75,
            high: 31.85,
            low: 30.91,
            close: 31.02,
            volume: 71_960_000,
            amount: 2_246_000_000,
            changePercent: -1.62,
            turnoverRate: 2.52,
            dailyIndex: 10,
          }}
          prevClose={31.53}
          digits={2}
          period="daily"
          style={{ left: 8, top: 8 }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByText('2026-09-30 周三')).toBeInTheDocument();
    expect(screen.getByText('31.75')).toHaveClass('up');
    expect(screen.getByText('31.02')).toHaveClass('down');
    expect(screen.getByText('-0.51')).toHaveClass('down');
    expect(screen.getByText('-1.62%')).toHaveClass('down');
    expect(screen.getByText('2.98%')).toBeInTheDocument(); // amplitude (31.85-30.91)/31.53
    expect(screen.getByText('7196.00万')).toBeInTheDocument();
    expect(screen.getByText('22.46亿')).toBeInTheDocument();
    expect(screen.getByText('2.52%')).toBeInTheDocument();
    expect(container.querySelector('.kc-tip')).toHaveStyle({ left: '8px' });
  });
});
