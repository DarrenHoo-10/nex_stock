import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import MinePage from '../MinePage';

const api = vi.hoisted(() => ({
  getPlanAccount: vi.fn(),
  getStockPlan: vi.fn(),
  listStockPlans: vi.fn(),
  getWatchlist: vi.fn(),
  addToWatchlist: vi.fn(),
  removeFromWatchlist: vi.fn(),
  loadStockIndex: vi.fn(),
}));

vi.mock('../../../api/dailyPlan', () => ({
  getPlanAccount: api.getPlanAccount,
  getStockPlan: api.getStockPlan,
  listStockPlans: api.listStockPlans,
  getLatestRun: vi.fn().mockResolvedValue(null),
  isPlanServiceUnavailable: (error: unknown) => (error as { unavailable?: boolean })?.unavailable === true,
}));
vi.mock('../../../api/systemConfig', () => ({
  systemConfigApi: {
    getWatchlist: api.getWatchlist,
    addToWatchlist: api.addToWatchlist,
    removeFromWatchlist: api.removeFromWatchlist,
  },
}));
vi.mock('../../../utils/stockIndexLoader', () => ({ loadStockIndex: api.loadStockIndex }));

const INDEX = [
  { canonicalCode: '600519.SH', displayCode: '600519', nameZh: '贵州茅台', pinyinFull: 'guizhoumaotai', pinyinAbbr: 'gzmt', aliases: ['茅台'], market: 'CN', assetType: 'stock', active: true, popularity: 100 },
  { canonicalCode: '000988.SZ', displayCode: '000988', nameZh: '华工科技', pinyinFull: 'huagongkeji', pinyinAbbr: 'hgkj', aliases: [], market: 'CN', assetType: 'stock', active: true, popularity: 90 },
  { canonicalCode: '00700.HK', displayCode: '00700', nameZh: '腾讯控股', pinyinFull: 'tengxunkonggu', pinyinAbbr: 'txkg', aliases: ['腾讯'], market: 'HK', assetType: 'stock', active: true, popularity: 100 },
];

function renderPage() {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={['/mine']}>
        <MinePage />
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  Object.values(api).forEach((fn) => fn.mockReset());
  localStorage.setItem('dsa.uiLanguage', 'zh');
  const unavailable = Object.assign(new Error('unavailable'), { unavailable: true });
  api.getPlanAccount.mockRejectedValue(unavailable);
  api.listStockPlans.mockRejectedValue(unavailable);
  api.getWatchlist.mockResolvedValue(['600519']);
  api.loadStockIndex.mockResolvedValue({ data: INDEX, fallback: false });
});

describe('MinePage stock search', () => {
  it('searches by name or pinyin and adds a stock to the watchlist', async () => {
    api.addToWatchlist.mockResolvedValue(['600519', '000988']);
    renderPage();
    // While the plan service is not ready, the watchlist itself is listed.
    const list = await screen.findByRole('list', { name: '自选列表' });
    expect(await within(list).findByText('贵州茅台')).toBeInTheDocument();

    const input = screen.getByRole('combobox', { name: '搜索股票加入自选' });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: 'hgkj' } });
    const option = await screen.findByRole('option', { name: /华工科技/ });
    expect(option).toHaveTextContent('+ 自选');

    api.getWatchlist.mockResolvedValue(['600519', '000988']);
    fireEvent.keyDown(input, { key: 'Enter' });
    await waitFor(() => expect(api.addToWatchlist).toHaveBeenCalledWith('000988.SZ'));
    expect(await within(list).findByText('华工科技')).toBeInTheDocument();
    expect(input).toHaveValue('');
  });

  it('marks stocks already in the watchlist and does not add them again', async () => {
    renderPage();
    const input = await screen.findByRole('combobox', { name: '搜索股票加入自选' });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '茅台' } });
    const option = await screen.findByRole('option', { name: /贵州茅台/ });
    await waitFor(() => expect(option).toHaveTextContent('已在自选'));
    fireEvent.click(option);
    expect(api.addToWatchlist).not.toHaveBeenCalled();
  });

  it('shows the backend error when adding fails and removes a stock from the fallback list', async () => {
    api.addToWatchlist.mockRejectedValue(new Error('自选已满'));
    api.removeFromWatchlist.mockResolvedValue([]);
    renderPage();
    const input = await screen.findByRole('combobox', { name: '搜索股票加入自选' });
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: '腾讯' } });
    fireEvent.click(await screen.findByRole('option', { name: /腾讯控股/ }));
    await waitFor(() => expect(api.addToWatchlist).toHaveBeenCalledWith('00700.HK'));
    expect(await screen.findByRole('alert')).toBeInTheDocument();

    const list = screen.getByRole('list', { name: '自选列表' });
    api.getWatchlist.mockResolvedValue([]);
    fireEvent.click(within(list).getByRole('button', { name: '移出' }));
    await waitFor(() => expect(api.removeFromWatchlist).toHaveBeenCalledWith('600519'));
    expect(await screen.findByText('还没有自选股。用右上角的搜索框加几只。')).toBeInTheDocument();
  });
});
