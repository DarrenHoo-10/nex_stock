import type { AssistantContext } from '../../../stores/planUiStore';
import type { Horizon } from '../../../types/dailyPlan';

const HORIZON_LABEL: Record<Horizon, string> = { short: '短线', swing: '波段' };

/** Wire shape of `context.daily_plan` (contract section 5, snake_case). */
export interface DailyPlanContextPayload {
  trade_date: string | null;
  kind: 'board' | 'stock' | 'none';
  board_id: string | null;
  stock_code: string | null;
  horizon: Horizon | null;
}

/** Context that actually carries an object: a board without an id or a stock without a code is `none`. */
export function normalizeContext(context: AssistantContext): AssistantContext {
  if (context.kind === 'board' && context.boardId) {
    return context;
  }
  if (context.kind === 'stock' && context.stockCode) {
    return context;
  }
  return { kind: 'none', tradeDate: context.tradeDate ?? null };
}

/** Stable identity of a context; a different key starts a new conversation. */
export function contextKey(context: AssistantContext): string {
  const ctx = normalizeContext(context);
  if (ctx.kind === 'board') {
    return `board:${ctx.boardId}`;
  }
  if (ctx.kind === 'stock') {
    return `stock:${ctx.stockCode}:${ctx.horizon ?? ''}`;
  }
  return 'none';
}

/** Chip text: 「当前：板块 半导体」, 「当前：华工科技 · 短线」 or 「未选中」. */
export function contextLabel(context: AssistantContext): string {
  const ctx = normalizeContext(context);
  if (ctx.kind === 'board') {
    return `当前：板块 ${ctx.boardName || ctx.boardId}`;
  }
  if (ctx.kind === 'stock') {
    const name = ctx.stockName || ctx.stockCode;
    return ctx.horizon ? `当前：${name} · ${HORIZON_LABEL[ctx.horizon]}` : `当前：${name}`;
  }
  return '未选中';
}

/** `context` of POST /agent/chat/stream: the daily-plan block plus the classic stock fields for stock context. */
export function buildStreamContext(
  context: AssistantContext,
  fallbackTradeDate: string | null,
): Record<string, unknown> {
  const ctx = normalizeContext(context);
  const dailyPlan: DailyPlanContextPayload = {
    trade_date: ctx.tradeDate ?? fallbackTradeDate ?? null,
    kind: ctx.kind,
    board_id: ctx.kind === 'board' ? (ctx.boardId ?? null) : null,
    stock_code: ctx.kind === 'stock' ? (ctx.stockCode ?? null) : null,
    horizon: ctx.kind === 'none' ? null : (ctx.horizon ?? null),
  };
  const payload: Record<string, unknown> = { daily_plan: dailyPlan };
  if (ctx.kind === 'stock') {
    payload.stock_code = ctx.stockCode;
    if (ctx.stockName) {
      payload.stock_name = ctx.stockName;
    }
  }
  return payload;
}

/** 3-4 questions per context kind (core-product-spec 6.3). */
export function suggestedQuestions(context: AssistantContext): string[] {
  const ctx = normalizeContext(context);
  if (ctx.kind === 'board') {
    return ['这个板块还能持续吗', '涨的原因是什么', '板块里哪只最稳'];
  }
  if (ctx.kind === 'stock') {
    return ctx.held
      ? ['如果明天低开怎么办', '如果明天高开怎么办', '按我的成本，止损应该放在哪里', '现在加仓风险大吗']
      : ['如果明天低开怎么办', '如果明天高开怎么办', '买多少合适', '为什么不建议买'];
  }
  return ['今天哪个板块最值得关注', '我的持仓明天怎么做', '今天有哪些自选可以买'];
}

interface ProgressLike {
  type: string;
  message?: string;
  display_name?: string;
  tool?: string;
  stage?: string;
}

/** One line for the progress of a running answer (mirrors ChatPage, shortened). */
export function describeProgress(steps: ProgressLike[]): string {
  const last = steps[steps.length - 1];
  if (!last) {
    return '正在连接…';
  }
  const tool = last.display_name || last.tool;
  switch (last.type) {
    case 'thinking':
      return last.message || '正在思考…';
    case 'tool_start':
      return tool ? `${tool}…` : '正在查询数据…';
    case 'tool_done':
      return tool ? `${tool} 完成` : '数据查询完成';
    case 'generating':
      return last.message || '正在生成回答…';
    default:
      return last.message || '处理中…';
  }
}
