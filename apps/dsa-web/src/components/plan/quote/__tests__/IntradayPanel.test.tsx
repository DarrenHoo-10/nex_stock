import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import { IntradayPanel } from '../IntradayPanel';

const getOpeningAuction = vi.hoisted(() => vi.fn().mockResolvedValue({ source: null, points: [] }));
const getIntraday = vi.hoisted(() => vi.fn());
vi.mock('../../../../api/stocks', () => ({ stocksApi: { getOpeningAuction, getIntraday } }));

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
    getIntraday.mockResolvedValue({ interval: '1min', data: BARS, isStale: false });
    renderPanel();

    expect(await screen.findByRole('img', { name: /七匹狼 2026-11-06/ })).toBeInTheDocument();
    expect(getIntraday).toHaveBeenCalledWith('002029', '2026-11-06', expect.any(Object));
    const readout = document.querySelector('.qi-readout');
    expect(readout).toHaveTextContent('15:00');
    expect(readout).toHaveTextContent('+0.45%');
    expect(readout).toHaveTextContent('昨收8.86');
  });

  it('shows the auction curve even before there is a regular bar', async () => {
    getIntraday.mockResolvedValue({ interval: '1min', data: [], isStale: false });
    getOpeningAuction.mockResolvedValueOnce({ source: 'ashareapi', points: [
      { time: '09:15:00', price: 8.3 }, { time: '09:25:00', price: 8.52 },
    ] });
    renderPanel();
    expect(await screen.findByRole('img', { name: '开盘集合竞价' })).toBeInTheDocument();
    expect(screen.getByText('09:25:00 · 价格 8.52')).toBeInTheDocument();
  });

  it('displays a match as a single result without adding it to the minute bars', async () => {
    getIntraday.mockResolvedValue({ interval: '1min', data: BARS, isStale: false });
    getOpeningAuction.mockResolvedValueOnce({ source: 'biying', points: [
      { time: '09:25:00', price: 8.48, volume: 500 },
    ] });
    renderPanel();
    expect(await screen.findByText('09:25:00 · 撮合价 8.48')).toBeInTheDocument();
    expect(screen.getByText('成交量 500')).toBeInTheDocument();
    expect(document.querySelector('.qi-readout')).toHaveTextContent('15:00');
  });

  it('keeps the regular chart when the auction request fails', async () => {
    getOpeningAuction.mockRejectedValueOnce(new Error('unavailable'));
    getIntraday.mockResolvedValue({ interval: '1min', data: BARS, isStale: false });
    renderPanel();
    expect(await screen.findByText('暂无竞价数据')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /七匹狼/ })).toBeInTheDocument();
  });

  it('does not display legacy five-minute data as a minute trend', async () => {
    getIntraday.mockResolvedValue({ interval: '5min', data: BARS, isStale: false });
    renderPanel();
    expect(await screen.findByRole('alert')).toHaveTextContent('服务端未返回 1 分钟行情');
    expect(document.querySelector('.qi-svg')).toBeNull();
  });

  it('shows a stale notice when the provider served old data', async () => {
    getIntraday.mockResolvedValue({ interval: '1min', data: BARS, isStale: true });
    renderPanel();
    expect(await screen.findByText('数据可能不是最新')).toBeInTheDocument();
  });

  it('closes from the back button and from Escape without bubbling the key', async () => {
    getIntraday.mockResolvedValue({ interval: '1min', data: BARS, isStale: false });
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
    getIntraday.mockResolvedValueOnce({ interval: '1min', data: BARS, isStale: false });
    renderPanel();

    fireEvent.click(await screen.findByRole('button', { name: '重试' }));

    await waitFor(() => expect(getIntraday).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('img', { name: /七匹狼/ })).toBeInTheDocument();
  });

  it('shows the backend reason for a 503 instead of the generic network text', async () => {
    const message = '历史交易日的分时来自必盈数据源，还没有配置必盈 licence：请在设置的数据源里填写 BIYING_LICENCE 后重试。';
    getIntraday.mockRejectedValue({
      response: { status: 503, data: { error: 'intraday_unavailable', message, reason: 'biying_not_configured' } },
    });
    renderPanel();

    expect(await screen.findByText(new RegExp(message.slice(0, 20)))).toBeInTheDocument();
    expect(screen.queryByText(/代理、DNS/)).not.toBeInTheDocument();
  });

  it('shows the empty state for a day without bars', async () => {
    getIntraday.mockResolvedValue({ interval: '1min', data: [], isStale: false });
    renderPanel();
    expect(await screen.findByText('这一天没有分时数据')).toBeInTheDocument();
  });
});
