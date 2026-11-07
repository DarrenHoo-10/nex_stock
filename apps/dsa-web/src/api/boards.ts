import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  BoardAnomalyListResponse,
  BoardAnomalyQuery,
  BoardDetail,
  BoardThresholdOverrides,
  ThresholdItem,
} from '../types/boards';

export { isPlanServiceUnavailable } from './planErrors';

/** GET /api/v1/boards/anomalies — market summary plus board list. */
export async function getBoardAnomalies(query: BoardAnomalyQuery = {}): Promise<BoardAnomalyListResponse> {
  const params: Record<string, string> = {};
  if (query.tradeDate) params.trade_date = query.tradeDate;
  if (query.boardType) params.board_type = query.boardType;
  if (query.state) params.state = query.state;
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/boards/anomalies', { params });
  return toCamelCase<BoardAnomalyListResponse>(response.data);
}

/** GET /api/v1/boards/{board_id} — board detail (404 when the board is unknown). */
export async function getBoardDetail(boardId: string, tradeDate?: string): Promise<BoardDetail> {
  const response = await apiClient.get<Record<string, unknown>>(
    `/api/v1/boards/${encodeURIComponent(boardId)}`,
    { params: tradeDate ? { trade_date: tradeDate } : undefined },
  );
  return toCamelCase<BoardDetail>(response.data);
}

/**
 * The thresholds body is `{ items: [...] }`. Item fields are single words, so the rows are returned without
 * key conversion: dict-valued thresholds keep their snake_case keys, which the PUT sends back unchanged.
 */
function thresholdItems(data: unknown): ThresholdItem[] {
  const items = (data as { items?: unknown } | null)?.items;
  return Array.isArray(items) ? (items as ThresholdItem[]) : [];
}

/** GET /api/v1/boards/thresholds — definitions with default and effective values. */
export async function getBoardThresholds(): Promise<ThresholdItem[]> {
  const response = await apiClient.get<unknown>('/api/v1/boards/thresholds');
  return thresholdItems(response.data);
}

/**
 * PUT /api/v1/boards/thresholds — save overrides; an empty object restores defaults. Keys are the threshold
 * `key` values (snake_case) and are sent as-is. Returns the refreshed definitions. A 400 carries the Chinese
 * validation message in `message` and the list in `detail`.
 */
export async function putBoardThresholds(overrides: BoardThresholdOverrides): Promise<ThresholdItem[]> {
  const response = await apiClient.put<unknown>('/api/v1/boards/thresholds', { overrides });
  return thresholdItems(response.data);
}
