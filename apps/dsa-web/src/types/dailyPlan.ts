/**
 * Next-trading-day plan types (contract: daily-plan-api-contract v1, sections 2-5).
 * Wire format is snake_case; these are the camelCase shapes produced by `toCamelCase`.
 * Amounts are CNY, prices keep two decimals, ratios are percentages (98.7 means 98.7%).
 */

export type Horizon = 'short' | 'swing';

/** Price-limit / lot-size market: main board, ChiNext, STAR, Beijing. */
export type BoardMarket = 'main' | 'chinext' | 'star' | 'bse';

/** Verdict for a stock that is not held. */
export type PlanVerdictNotHeld = 'buy' | 'wait' | 'skip' | 'suspended';
/** Verdict for a held stock. */
export type PlanVerdictHeld = 'hold' | 'add' | 'reduce' | 'clear';
export type PlanVerdict = PlanVerdictNotHeld | PlanVerdictHeld;

/** The 「就……」 action of a conditional plan. */
export type PlanAction = 'buy' | 'sell' | 'reduce' | 'clear' | 'hold' | 'wait' | 'skip';

export type RunStatus = 'pending' | 'running' | 'succeeded' | 'partial' | 'failed' | 'skipped';
export type StepStatus = RunStatus;
export type RunTrigger = 'schedule' | 'manual';
export type RunStepKey = 'boards' | 'picks' | 'plans' | 'switch' | 'push';
export type PushStatus = 'sent' | 'failed' | 'skipped' | 'pending';

export type StopMode = 'fixed' | 'nearer';
export type PlanStatus = 'ready' | 'failed' | 'missing';
export type LevelKind =
  | 'pressure'
  | 'breakout'
  | 'entry'
  | 'support'
  | 'stop'
  | 'take_profit'
  | 'limit_up'
  | 'limit_down'
  | 'cost'
  | 'close';
export type SizingStatus = 'ok' | 'below_lot' | 'max_holdings' | 'no_price';
export type SwitchDecisionKind = 'switch' | 'keep';

// ---------------------------------------------------------------- account

export interface PositionRules {
  stopLossPct: number;
  maxTotalPct: number;
  maxHoldings: number;
  /** Effective single-stock cap in percent. */
  maxSinglePct: number;
  /** true when derived as total position / max holdings. */
  maxSinglePctDerived: boolean;
  stopMode: StopMode;
}

export interface HoldingInput {
  /** 6-digit code. */
  code: string;
  shares: number;
  cost: number;
  buyDate: string | null;
  horizon: Horizon | null;
}

export interface Holding extends HoldingInput {
  name: string;
  boardMarket: BoardMarket;
  isSt: boolean;
  close: number | null;
  changePct: number | null;
  marketValue: number;
  /** Floating P&L. */
  pnl: number;
  pnlPct: number;
  /** T+1 sellable. */
  sellable: boolean;
  sellableNote: string | null;
}

export interface AccountResponse {
  cash: number;
  marketValue: number;
  /** cash + marketValue. */
  totalCapital: number;
  /** Total position, percent. */
  positionPct: number;
  holdings: Holding[];
  rules: PositionRules;
  updatedAt: string | null;
}

export interface HoldingsSnapshotRequest {
  cash: number;
  holdings: HoldingInput[];
}

// ------------------------------------------------------------ stock plans

export interface StockPlanSummary {
  code: string;
  name: string;
  boardMarket: BoardMarket;
  isSt: boolean;
  held: boolean;
  close: number | null;
  changePct: number | null;
  /** Verdict of the default horizon (held: the holding's horizon, otherwise short). */
  verdict: PlanVerdict;
  /** One-line plan. */
  headline: string;
  status: PlanStatus;
  /** Set when status is `failed`. */
  failureReason: string | null;
}

export interface StockPlanListResponse {
  tradeDate: string;
  nextTradeDate: string;
  /** Held first; not-held sorted buy -> wait -> skip -> suspended. */
  items: StockPlanSummary[];
}

export interface Condition {
  ifText: string;
  thenText: string;
  action: PlanAction;
  /** Shares converted by the rules (sell 1/3 -> 800). */
  shares: number | null;
  price: number | null;
  /** Price is outside the next-day limit-up/limit-down range. */
  outsideLimit: boolean;
}

export interface StopInfo {
  price: number;
  mode: StopMode;
  /** e.g. "47.00 × 0.90" or "取较近者：技术支撑". */
  formula: string;
  techSupport: number | null;
  /** e.g. 「止损会在支撑之前触发」「技术形态转弱」. */
  warning: string | null;
}

export interface Sizing {
  status: SizingStatus;
  /** Chinese hint for below-lot / full-position cases. */
  message: string | null;
  price: number;
  /** 100 or 200. */
  lot: number;
  /** The three raw caps converted to shares. */
  byTotal: number;
  bySingle: number;
  byCash: number;
  rawShares: number;
  shares: number;
  amount: number;
  /** Share of total capital, percent. */
  capitalPct: number;
  /** Total position after buying, percent. */
  postPositionPct: number;
  stop: StopInfo;
  /** Loss when the stop price is hit. */
  maxLoss: number;
  limitDownPrice: number;
  limitDownLoss: number;
  /** Step-by-step 「怎么算的」. */
  steps: string[];
}

export interface HorizonPlan {
  verdict: PlanVerdict;
  headline: string;
  /** Why the plan was dropped or not given. */
  reason: string | null;
  entryPrice: number | null;
  entryZone: [number, number] | null;
  breakoutPrice: number | null;
  /** Invalidation price. */
  invalidPrice: number | null;
  takeProfitPrice: number | null;
  conditions: Condition[];
  /** Not held and a buy price exists. */
  sizing: Sizing | null;
  stop: StopInfo | null;
}

export interface Level {
  kind: LevelKind;
  price: number;
  label: string;
}

export interface SwitchCandidate {
  code: string;
  name: string;
  reason: string;
  verdict: PlanVerdict;
}

export interface SwitchConditionItem {
  text: string;
  /** null = unknown / not evaluated. */
  met: boolean | null;
}

/** Only for held stocks when max holdings is reached. */
export interface SwitchDecision {
  decision: SwitchDecisionKind;
  summary: string;
  candidate: SwitchCandidate | null;
  /** Both conditions must hold to switch. */
  conditions: SwitchConditionItem[];
  /** T+1, stops on both sides, max loss after switching. */
  costs: string[];
}

export interface ModelUsage {
  /** Model actually used (no channel secrets). */
  model: string;
  fallbackFrom: string | null;
  fallbackReason: string | null;
}

export interface StockPlan extends StockPlanSummary {
  tradeDate: string;
  nextTradeDate: string;
  /** 「今天的情况」. */
  situation: string;
  /** null = no plan for that horizon (see the horizon's reason). */
  horizons: Record<Horizon, HorizonPlan | null>;
  defaultHorizon: Horizon;
  /** Price-limit percentage. */
  limitPct: number;
  nextLimitUp: number | null;
  nextLimitDown: number | null;
  /** Price ladder. */
  levels: Level[];
  switch: SwitchDecision | null;
  risks: string[];
  dataGaps: string[];
  model: ModelUsage | null;
}

export interface SizingRequest {
  code: string;
  price: number;
  horizon: Horizon;
}

// ------------------------------------------------------------------- runs

export interface RunStep {
  key: RunStepKey;
  label: string;
  status: StepStatus;
  finishedAt: string | null;
  detail: string | null;
}

export interface RunFailure {
  code: string;
  name: string;
  reason: string;
}

/** Result of one personal channel type in a run's push (`push.channels`). */
export interface RunPushChannelOutcome {
  /** Channel type as the server names it: `feishu`, `dingtalk`, `wechat`, `telegram`, `email`, `custom` ... */
  channel: string;
  success: boolean;
}

export interface RunPush {
  /**
   * `feishu`: the primary user's system (.env) Feishu route (also what runs without the field mean);
   * `personal`: the signed-in user's own push channels.
   */
  channel: 'feishu' | 'personal';
  status: PushStatus;
  sentAt: string | null;
  error: string | null;
  /** Whether a route exists: the Feishu webhook, or at least one usable personal channel (nothing secret is returned). */
  configured: boolean;
  /** Why Stock skipped the push, e.g. `no_model` for a personal schedule or `managed_feishu` for the system route. */
  reason?: string | null;
  /** Personal runs only: what each channel type returned. */
  channels?: RunPushChannelOutcome[];
}

export interface RunModelCall {
  step: string;
  calls: number;
  model: string;
  note: string | null;
}

export interface PlanRun {
  id: string;
  tradeDate: string;
  status: RunStatus;
  trigger: RunTrigger;
  startedAt: string | null;
  finishedAt: string | null;
  durationSec: number | null;
  steps: RunStep[];
  stockTotal: number;
  stockSucceeded: number;
  stockFailed: number;
  failures: RunFailure[];
  push: RunPush;
  /** Feishu message body preview. */
  pushPreviewMarkdown: string | null;
  modelCalls: RunModelCall[];
  dataGaps: string[];
}

/** Response of GET /plan/runs/latest. */
export interface LatestRunResponse {
  run: PlanRun | null;
}

// -------------------------------------------------------------- assistant

export type AssistantContextKind = 'board' | 'stock' | 'none';

/** Sent as `context.daily_plan` to /agent/chat/stream (snake_case on the wire). */
export interface DailyPlanAssistantContext {
  tradeDate: string;
  kind: AssistantContextKind;
  boardId: string | null;
  stockCode: string | null;
  horizon: Horizon | null;
}

// ------------------------------------------------------------------ picks

/** Optional fields appended to a screening `Pick` (contract section 4). */
export interface PickPlanExtras {
  horizons?: Horizon[];
  entryCondition?: string | null;
  invalidCondition?: string | null;
  risks?: string[];
  /** e.g. 「连板股只列出不建议追」. */
  note?: string | null;
}
