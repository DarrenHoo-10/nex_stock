import apiClient from './index';
import { toCamelCase } from './utils';
import type {
  BoardAnomalyListResponse,
  BoardAnomalyQuery,
  BoardDetail,
  BoardThresholdOverrides,
  FollowListResponse,
  RecommendationResponse,
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

/** GET /api/v1/boards/follows — followed boards with the latest run's row. */
export async function getBoardFollows(): Promise<FollowListResponse> {
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/boards/follows');
  return toCamelCase<FollowListResponse>(response.data);
}

/** PUT /api/v1/boards/follows/{id} — follow (idempotent); returns the updated list. */
export async function followBoard(boardId: string): Promise<FollowListResponse> {
  const response = await apiClient.put<Record<string, unknown>>(`/api/v1/boards/follows/${encodeURIComponent(boardId)}`);
  return toCamelCase<FollowListResponse>(response.data);
}

/** DELETE /api/v1/boards/follows/{id} — unfollow (idempotent); returns the updated list. */
export async function unfollowBoard(boardId: string): Promise<FollowListResponse> {
  const response = await apiClient.delete<Record<string, unknown>>(`/api/v1/boards/follows/${encodeURIComponent(boardId)}`);
  return toCamelCase<FollowListResponse>(response.data);
}

/** GET /api/v1/boards/recommendations — boards picked for the next session from the latest run. */
export async function getBoardRecommendations(): Promise<RecommendationResponse> {
  const response = await apiClient.get<Record<string, unknown>>('/api/v1/boards/recommendations');
  return toCamelCase<RecommendationResponse>(response.data);
}

/** POST /api/v1/boards/recommendations/refresh — regenerate in the background; poll the GET. */
export async function refreshBoardRecommendations(): Promise<RecommendationResponse> {
  const response = await apiClient.post<Record<string, unknown>>('/api/v1/boards/recommendations/refresh');
  return toCamelCase<RecommendationResponse>(response.data);
}
