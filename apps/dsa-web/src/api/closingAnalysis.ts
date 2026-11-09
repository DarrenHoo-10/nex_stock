import apiClient from './index';

interface ClosingAnalysis {
  daily_plan: string | null;
  market_review: string | null;
  stock_summary: string | null;
}

const endpoint = '/api/v1/closing-analysis';

export const closingAnalysisApi = {
  async dates(signal: AbortSignal) {
    return (await apiClient.get<string[]>(`${endpoint}/dates`, { signal })).data;
  },
  async get(day: string, signal: AbortSignal) {
    return (await apiClient.get<ClosingAnalysis>(`${endpoint}/${day}`, { signal })).data;
  },
};
