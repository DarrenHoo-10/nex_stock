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
