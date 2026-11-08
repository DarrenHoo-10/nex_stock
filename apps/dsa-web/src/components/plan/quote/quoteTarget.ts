import type { AssetType, Market, StockSuggestion } from '../../../types/stockIndex';

/** A symbol opened in the chart dialog. */
export interface QuoteTarget {
  /** Code sent to the history API (index canonical form, e.g. 600519.SH / 510300.SH / 00700.HK). */
  code: string;
  /** Short code shown to the user (600519 / 510300 / 00700 / AAPL). */
  displayCode: string;
  /** Code used for the watchlist: bare 6 digits for A-share stocks/ETFs, canonical otherwise. */
  watchCode: string;
  name: string | null;
  market: Market;
  assetType: AssetType;
}

const A_SHARE_CODE_RE = /^\d{6}$/;

export function targetFromSuggestion(suggestion: StockSuggestion): QuoteTarget {
  const assetType = suggestion.assetType ?? 'stock';
  const bareAShare =
    (suggestion.market === 'CN' || suggestion.market === 'BSE') &&
    assetType !== 'index' &&
    A_SHARE_CODE_RE.test(suggestion.displayCode);
  return {
    code: suggestion.canonicalCode,
    displayCode: suggestion.displayCode,
    watchCode: bareAShare ? suggestion.displayCode : suggestion.canonicalCode,
    name: suggestion.nameZh || null,
    market: suggestion.market,
    assetType,
  };
}

/** Fallback when the user presses Enter on a raw code that is not in the symbol index. */
export function targetFromRawCode(raw: string): QuoteTarget | null {
  const code = raw.trim().toUpperCase();
  if (!code || code.length > 16 || !/^[0-9A-Z.]+$/.test(code)) {
    return null;
  }
  const isAShare = A_SHARE_CODE_RE.test(code);
  const isEtf = isAShare && /^(1[568]|5[1268])/.test(code);
  return {
    code,
    displayCode: code,
    watchCode: code,
    name: null,
    market: isAShare ? 'CN' : /^\d{4,5}$/.test(code) ? 'HK' : 'US',
    assetType: isEtf ? 'etf' : 'stock',
  };
}
