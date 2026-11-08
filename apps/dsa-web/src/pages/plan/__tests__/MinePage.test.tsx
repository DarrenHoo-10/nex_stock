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

describe('MinePage while the plan service is not ready', () => {
  it('lists the watchlist with names and removes a stock', async () => {
    api.removeFromWatchlist.mockResolvedValue([]);
    renderPage();
    const list = await screen.findByRole('list', { name: '自选列表' });
    expect(await within(list).findByText('贵州茅台')).toBeInTheDocument();
    expect(within(list).getByText('600519')).toBeInTheDocument();

    api.getWatchlist.mockResolvedValue([]);
    fireEvent.click(within(list).getByRole('button', { name: '移出' }));
    await waitFor(() => expect(api.removeFromWatchlist).toHaveBeenCalledWith('600519'));
    expect(await screen.findByText(/还没有自选股/)).toBeInTheDocument();
  });

  it('keeps the header quote search available', async () => {
    renderPage();
    await screen.findByRole('list', { name: '自选列表' });
    expect(screen.getAllByRole('combobox').length).toBeGreaterThan(0);
  });
});
