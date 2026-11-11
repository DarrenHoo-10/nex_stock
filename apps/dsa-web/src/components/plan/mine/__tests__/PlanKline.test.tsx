import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { PlanKline } from '../PlanKline';

const getHistory = vi.hoisted(() => vi.fn());
const getQuote = vi.hoisted(() => vi.fn());
const getOpeningAuction = vi.hoisted(() => vi.fn().mockResolvedValue({ source: null, points: [] }));
const getIntraday = vi.hoisted(() => vi.fn());
vi.mock('../../../../api/stocks', () => ({ stocksApi: { getOpeningAuction, getHistory, getQuote, getIntraday } }));

function bar(day: number) {
  const close = 8 + day * 0.01;
  return {
    date: `2026-10-${String(day).padStart(2, '0')}`,
    open: close - 0.05,
    high: close + 0.1,
    low: close - 0.1,
    close,
    volume: 1000 + day,
    amount: 9000 + day,
    changePercent: 0.5,
  };
}

function renderChart(code = '002029') {
  return render(
    <UiLanguageProvider>
      <PlanKline code={code} name="七匹狼" />
    </UiLanguageProvider>,
  );
}

describe('PlanKline', () => {
  beforeEach(() => {
    getHistory.mockReset();
    getQuote.mockReset();
    getIntraday.mockReset();
    getIntraday.mockResolvedValue({ interval: '1min', data: [], isStale: false });
    getQuote.mockResolvedValue({ floatShares: 1e9 });
  });

  it('requests the daily history of the planned stock and draws the chart', async () => {
    getHistory.mockResolvedValue({
      stockName: '七匹狼',
      data: Array.from({ length: 25 }, (_, index) => bar(index + 1)),
    });
    renderChart();

    expect(await screen.findByRole('img', { name: /七匹狼/ })).toBeInTheDocument();
    // The title block (name / code / last price) is dropped, the rest of the quote chart stays.
    expect(screen.queryByText('002029')).not.toBeInTheDocument();
    expect(screen.getByRole('radiogroup')).toBeInTheDocument();
    expect(screen.getByText('筹码分布')).toBeInTheDocument();
    expect(getHistory).toHaveBeenCalledWith('002029', expect.objectContaining({ days: 750 }));
  });

  it('shows the error with a retry that reloads the history', async () => {
    getHistory.mockRejectedValueOnce(new Error('boom'));
    getHistory.mockResolvedValueOnce({ stockName: '七匹狼', data: [bar(1), bar(2), bar(3)] });
    renderChart();

    const retry = await screen.findByRole('button', { name: /重试|Retry/i });
    fireEvent.click(retry);

    await waitFor(() => expect(getHistory).toHaveBeenCalledTimes(2));
    expect(await screen.findByTestId('plan-kline')).toBeInTheDocument();
  });

  it('shows the empty state when the stock has no usable bars', async () => {
    getHistory.mockResolvedValue({ stockName: null, data: [] });
    renderChart();

    expect(await screen.findByRole('status')).toHaveTextContent('没有取到');
  });

  describe('double-click for the intraday of a day', () => {
    const history = { stockName: '七匹狼', data: Array.from({ length: 25 }, (_, index) => bar(index + 1)) };
    // jsdom has no layout, so the plot is 80px wide: 25 visible candles of 3.2px, x=10 is the 4th candle.
    const FOURTH_DAY = '2026-10-04';

    it('opens the one-minute chart of the double-clicked day over the candles', async () => {
      getHistory.mockResolvedValue(history);
      renderChart();
      const canvas = await screen.findByRole('img', { name: /K 线图/ });

      fireEvent.doubleClick(canvas, { clientX: 10, clientY: 50 });

      await waitFor(() => expect(getIntraday).toHaveBeenCalledWith('002029', FOURTH_DAY, expect.any(Object)));
      expect(await screen.findByText(`${FOURTH_DAY} 分时走势（1 分钟）`)).toBeInTheDocument();
      expect(await screen.findByText('这一天没有分时数据')).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: '返回 K 线' }));
      expect(screen.queryByText(`${FOURTH_DAY} 分时走势（1 分钟）`)).not.toBeInTheDocument();
    });

    it('does nothing on the weekly period, where a candle is not one session', async () => {
      getHistory.mockResolvedValue(history);
      renderChart();
      const canvas = await screen.findByRole('img', { name: /K 线图/ });
      fireEvent.click(screen.getByRole('radio', { name: '周K' }));

      fireEvent.doubleClick(await screen.findByRole('img', { name: /K 线图/ }), { clientX: 10, clientY: 50 });

      expect(canvas).toBeDefined();
      expect(getIntraday).not.toHaveBeenCalled();
    });

    it('is off for symbols without minute data (Hong Kong)', async () => {
      getHistory.mockResolvedValue(history);
      renderChart('00700');
      const canvas = await screen.findByRole('img', { name: /K 线图/ });

      fireEvent.doubleClick(canvas, { clientX: 10, clientY: 50 });

      expect(getIntraday).not.toHaveBeenCalled();
      expect(screen.queryByText('双击日 K 查看当天分时')).not.toBeInTheDocument();
    });
  });
});
