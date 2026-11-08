import type { StockSuggestion } from '../types/stockIndex';

/** Watchlist form of a stock-index entry's code, as the backend stores it (600519, HK00700, AAPL). */
export function watchlistCodeOf(item: Pick<StockSuggestion, 'displayCode' | 'market'>): string {
  return item.market === 'HK' ? `HK${item.displayCode}` : item.displayCode.toUpperCase();
}
