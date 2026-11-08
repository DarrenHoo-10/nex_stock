import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../../contexts/UiLanguageContext';
import type { StockIndexItem } from '../../../../types/stockIndex';
import { StockSearchBox } from '../StockSearchBox';

const INDEX: StockIndexItem[] = [
  {
    canonicalCode: '600519.SH',
    displayCode: '600519',
    nameZh: '贵州茅台',
    pinyinFull: 'guizhoumaotai',
    pinyinAbbr: 'gzmt',
    aliases: [],
    market: 'CN',
    assetType: 'stock',
    active: true,
    popularity: 100,
  },
  {
    canonicalCode: '510300.SH',
    displayCode: '510300',
    nameZh: '华泰柏瑞沪深300ETF',
    pinyinFull: 'huataiboruihushen300ETF',
    pinyinAbbr: 'htbrhs300ETF',
    aliases: ['沪深300ETF'],
    market: 'CN',
    assetType: 'etf',
    active: true,
    popularity: 100,
  },
];

vi.mock('../../../../hooks/useStockIndex', () => ({
  useStockIndex: (enabled: boolean) => ({
    index: enabled ? INDEX : [],
    loading: false,
    error: null,
    fallback: false,
    loaded: enabled,
  }),
}));

function setup() {
  const onOpen = vi.fn();
  render(
    <UiLanguageProvider>
      <StockSearchBox onOpen={onOpen} />
    </UiLanguageProvider>,
  );
  const input = screen.getByRole('combobox');
  fireEvent.focus(input);
  return { input, onOpen };
}

describe('StockSearchBox', () => {
  it('finds ETFs by tracked-index name and opens the highlighted one with Enter', () => {
    const { input, onOpen } = setup();
    fireEvent.change(input, { target: { value: '沪深300' } });
    const option = screen.getByRole('option', { name: /华泰柏瑞沪深300ETF/ });
    expect(option).toHaveTextContent('ETF');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledWith(
      expect.objectContaining({ code: '510300.SH', watchCode: '510300', assetType: 'etf' }),
    );
    expect(input).toHaveValue('');
  });

  it('finds stocks by pinyin and opens on click', () => {
    const { input, onOpen } = setup();
    fireEvent.change(input, { target: { value: 'gzmt' } });
    fireEvent.mouseDown(screen.getByRole('option', { name: /贵州茅台/ }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ code: '600519.SH', name: '贵州茅台' }));
  });

  it('offers a raw code that is not in the index', () => {
    const { input, onOpen } = setup();
    fireEvent.change(input, { target: { value: '159999' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ code: '159999', assetType: 'etf' }));
  });
});
