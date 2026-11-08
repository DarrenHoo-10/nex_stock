import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { IntradayPanel } from '../IntradayPanel';

const getIntraday = vi.hoisted(() => vi.fn());
vi.mock('../../../../api/stocks', () => ({ stocksApi: { getIntraday } }));

function bar(time: string, close: number, volume: number, amount: number) {
  return { time: `2026-11-06T${time}:00+08:00`, open: close, high: close, low: close, close, volume, amount };
}

const BARS = [bar('09:35', 8.51, 123074, 1046327), bar('10:30', 8.7, 5000, 43500), bar('15:00', 8.9, 34333, 305562)];

function renderPanel(onClose = vi.fn()) {
  render(
    <UiLanguageProvider>
      <IntradayPanel code="002029" name="七匹狼" date="2026-11-06" prevClose={8.86} digits={2} onClose={onClose} />
    </UiLanguageProvider>,
  );
  return onClose;
}

describe('IntradayPanel', () => {
  beforeEach(() => {
    getIntraday.mockReset();
  });

  it('loads the chosen day and reads out the last bar against the previous close', async () => {
    getIntraday.mockResolvedValue({ data: BARS, isStale: false });
    renderPanel();

    expect(await screen.findByRole('img', { name: /七匹狼 2026-11-06/ })).toBeInTheDocument();
    expect(getIntraday).toHaveBeenCalledWith('002029', '2026-11-06', expect.any(Object));
    const readout = document.querySelector('.qi-readout');
    expect(readout).toHaveTextContent('15:00');
    expect(readout).toHaveTextContent('+0.45%');
    expect(readout).toHaveTextContent('昨收8.86');
  });

  it('follows the pointer to another bar', async () => {
    getIntraday.mockResolvedValue({ data: BARS, isStale: false });
    renderPanel();
    const chart = await screen.findByRole('img', { name: /七匹狼/ });
    // jsdom has no layout: give the svg a 960-wide box so client x equals viewBox x.
    chart.getBoundingClientRect = () => ({ left: 0, top: 0, width: 960, height: 400, right: 960, bottom: 400, x: 0, y: 0, toJSON: () => ({}) });

    // x = 58 + (0.5 / 48) * 844 ≈ 67 is the first slot.
    fireEvent.pointerMove(chart, { clientX: 67, clientY: 100 });

    const readout = document.querySelector('.qi-readout');
    expect(readout).toHaveTextContent('09:35');
    expect(readout).toHaveTextContent('价格8.51');
    expect(readout).toHaveTextContent('-3.95%');
  });

  it('shows a stale notice when the provider served old data', async () => {
    getIntraday.mockResolvedValue({ data: BARS, isStale: true });
    renderPanel();
    expect(await screen.findByText('数据可能不是最新')).toBeInTheDocument();
  });

  it('closes from the back button and from Escape without bubbling the key', async () => {
    getIntraday.mockResolvedValue({ data: BARS, isStale: false });
    const onClose = renderPanel();
    await screen.findByRole('img', { name: /七匹狼/ });

    fireEvent.click(screen.getByRole('button', { name: '返回 K 线' }));
    expect(onClose).toHaveBeenCalledTimes(1);

    const outer = vi.fn();
    document.addEventListener('keydown', outer);
    fireEvent.keyDown(screen.getByRole('group'), { key: 'Escape' });
    document.removeEventListener('keydown', outer);
    expect(onClose).toHaveBeenCalledTimes(2);
    expect(outer).not.toHaveBeenCalled();
  });

  it('shows the error and retries', async () => {
    getIntraday.mockRejectedValueOnce(new Error('boom'));
    getIntraday.mockResolvedValueOnce({ data: BARS, isStale: false });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: '重试' }));

    await waitFor(() => expect(getIntraday).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('img', { name: /七匹狼/ })).toBeInTheDocument();
  });

  it('shows the empty state for a day without bars', async () => {
    getIntraday.mockResolvedValue({ data: [], isStale: false });
    renderPanel();
    expect(await screen.findByText('这一天没有分时数据')).toBeInTheDocument();
  });
});
