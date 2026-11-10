import apiClient from './index';
import { toCamelCase } from './utils';

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

export type StockHistoryBar = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
  amount?: number | null;
  changePercent?: number | null;
};

export type StockHistoryResponse = {
  stockCode: string;
  stockName?: string | null;
  period: string;
  data: StockHistoryBar[];
};

/** One 5-minute bar of a trading day; `time` is Beijing time with offset (2026-11-06T09:35:00+08:00). */
export type StockIntradayBar = {
  time: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close: number;
  /** Shares. */
  volume?: number | null;
  /** Yuan. */
  amount?: number | null;
};

export type OpeningAuction = {
  source: 'ashareapi' | 'tushare' | 'biying' | null;
  points: { time: string; price: number; volume?: number | null }[];
};

export type StockIntradayResponse = {
  stockCode: string;
  tradeDate: string;
  interval: string;
  source: string;
  asOf: string;
  isStale: boolean;
  staleReason?: string | null;
  data: StockIntradayBar[];
};

export type StockQuoteResponse = {
  stockCode: string;
  stockName?: string | null;
  currentPrice: number;
  changePercent?: number | null;
  volume?: number | null;
  turnoverRate?: number | null;
  /** Float shares (same unit as daily volume: shares); null when the source lacks the fields. */
  floatShares?: number | null;
  updateTime?: string | null;
};

export const stocksApi = {
  /** Daily K-line history (up to 1000 days). */
  async getHistory(
    stockCode: string,
    options: { days?: number; signal?: AbortSignal } = {},
  ): Promise<StockHistoryResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/history`,
      { params: { period: 'daily', days: options.days ?? 250 }, signal: options.signal, timeout: 60000 },
    );
    return toCamelCase<StockHistoryResponse>(response.data);
  },

  async getOpeningAuction(code: string, date: string, signal: AbortSignal): Promise<OpeningAuction> {
    const response = await apiClient.get<OpeningAuction>(
      `/api/v1/stocks/${encodeURIComponent(code)}/opening-auction`, { params: { date }, signal },
    );
    return response.data;
  },

  /** 5-minute bars of one trading day (`YYYY-MM-DD`); A-share only, 503 when no minute source is configured. */
  async getIntraday(
    stockCode: string,
    date: string,
    options: { signal?: AbortSignal } = {},
  ): Promise<StockIntradayResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/intraday`,
      { params: { date }, signal: options.signal, timeout: 30000 },
    );
    return toCamelCase<StockIntradayResponse>(response.data);
  },

  /** Realtime quote; `floatShares` feeds the chip distribution's daily turnover. */
  async getQuote(stockCode: string, options: { signal?: AbortSignal } = {}): Promise<StockQuoteResponse> {
    const response = await apiClient.get<Record<string, unknown>>(
      `/api/v1/stocks/${encodeURIComponent(stockCode)}/quote`,
      { signal: options.signal, timeout: 60000 },
    );
    return toCamelCase<StockQuoteResponse>(response.data);
  },

  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },
};
