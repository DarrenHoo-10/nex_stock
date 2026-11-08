/**
 * Board anomaly types (contract: daily-plan-api-contract v1, sections 2-3).
 * Wire format is snake_case; these are the camelCase shapes produced by `toCamelCase`.
 * Ratios are percentages (98.7 means 98.7%).
 */

export type BoardType = 'industry' | 'concept';

export type BoardState =
  | 'first_launch'
  | 'strengthening'
  | 'divergence'
  | 'ebbing'
  | 'abnormal_drop'
  | 'insufficient_history'
  | 'none';

export type BoardTag = 'mainline' | 'supported' | 'single_stock' | 'cooling' | 'merged';

/** Evidence grade of a threshold: medium / low / inferred. */
export type Evidence = 'medium' | 'low' | 'inferred';

export type BoardDriverKind = 'news' | 'capital' | 'earnings' | 'unknown';

export type BoardRepresentativeRole = 'leader' | 'turnover' | 'follower';

export interface MarketSummary {
  indexName: string;
  indexChangePct: number | null;
  upCount: number | null;
  downCount: number | null;
  limitUpCount: number | null;
  limitDownCount: number | null;
  /** Two-market turnover in 100 million CNY. */
  turnoverYi: number | null;
  /** Whether the broad-rally protection is active. */
  broadRally: boolean;
  note: string | null;
}

export interface BoardRow {
  /** Stable id, e.g. "industry:BK0448". */
  id: string;
  name: string;
  boardType: BoardType;
  state: BoardState;
  tags: BoardTag[];
  changePct: number | null;
  change5dPct: number | null;
  excessPct: number | null;
  amountRatio: number | null;
  breadthPct: number | null;
  /** null for industry boards. */
  limitUpCount: number | null;
  /** null when the sample is too small. */
  brokenRatePct: number | null;
  reason: string;
}

export interface BoardMetric {
  label: string;
  value: string;
  hint: string | null;
}

export interface BoardSeriesPoint {
  tradeDate: string;
  excessPct: number | null;
}

export interface BoardRepresentative {
  code: string;
  name: string;
  role: BoardRepresentativeRole;
  close: number | null;
  changePct: number | null;
}

export interface BoardDetail extends BoardRow {
  verdictText: string;
  /** Starts with 「原因未明」 when the driver cannot be confirmed. */
  drivers: string;
  driverKind: BoardDriverKind;
  /** Six items. */
  metrics: BoardMetric[];
  /** Last 8 trading days. */
  excessSeries: BoardSeriesPoint[];
  representatives: BoardRepresentative[];
  evidence: string[];
  /** false hides 「在此板块中选股」 (single-stock driven, insufficient data). */
  canPick: boolean;
  dataGaps: string[];
}

export interface BoardAnomalyListResponse {
  tradeDate: string;
  market: MarketSummary;
  boards: BoardRow[];
  historyNote: string | null;
}

export type ThresholdValue = number | string | Record<string, unknown>;

export interface ThresholdItem {
  key: string;
  label: string;
  /** Currently effective value. */
  value: ThresholdValue;
  default: ThresholdValue;
  display: string;
  unit: string | null;
  evidence: Evidence;
  note: string | null;
  overridden: boolean;
}

export interface BoardAnomalyQuery {
  tradeDate?: string;
  boardType?: BoardType;
  state?: BoardState;
}

/** Request body for PUT /boards/thresholds; empty object restores defaults. */
export type BoardThresholdOverrides = Record<string, ThresholdValue>;

/** A followed board: name / type recorded when followed, plus the latest run's row (null without one). */
export interface FollowedBoard {
  id: string;
  name: string;
  boardType: BoardType;
  followedAt: string | null;
  today: BoardRow | null;
}

export interface FollowListResponse {
  tradeDate: string | null;
  boards: FollowedBoard[];
}

export interface RecommendationItem {
  rank: number;
  reason: string;
  board: BoardRow;
}

/** GET /boards/recommendations. `source`: model = picked by the analysis model; rule = fallback ranking. */
export interface RecommendationResponse {
  tradeDate: string | null;
  status: 'ready' | 'generating' | 'none' | 'no_run';
  source: 'model' | 'rule' | null;
  model: string | null;
  note: string | null;
  generatedAt: string | null;
  items: RecommendationItem[];
}
