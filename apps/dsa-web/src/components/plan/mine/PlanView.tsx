import type React from 'react';
import { Fragment, useMemo, useState } from 'react';
import { CountUp as Count } from '../motion/CountUp';
import { SegmentedControl } from '../motion/SegmentedControl';
import { Stagger } from '../motion/Stagger';
import { PlanIcon } from '../PlanIcon';
import type { Condition, Holding, Horizon, HorizonPlan, PositionRules, StockPlan } from '../../../types/dailyPlan';
import {
  actionClass,
  changeClass,
  fmtNum,
  fmtPrice,
  HORIZON_HINT,
  HORIZON_LABEL,
  marketLabel,
  textMentionsNumber,
  VERDICT_BIG_CLASS,
  VERDICT_LABEL,
} from './format';
import { buildLadderLevels } from './ladderLayout';
import { PriceLadder } from './PriceLadder';
import { SizingBlock } from './SizingBlock';
import { SwitchCard } from './SwitchCard';

export interface RecordBuyPrefill {
  code: string;
  name: string;
  shares: number | null;
  price: number | null;
  horizon: Horizon;
}

export interface PlanViewProps {
  plan: StockPlan;
  /** The matching holding from the account (held stocks only; null when the account is not loaded). */
  holding: Holding | null;
  /** First holding, used to point at the switch decision when the max-holdings rule blocks a buy. */
  firstHolding: Pick<Holding, 'code' | 'name'> | null;
  rules: PositionRules | null;
  onSelectStock: (code: string) => void;
  onAsk: (horizon?: Horizon) => void;
  onRecordBuy: (prefill: RecordBuyPrefill) => void;
  onRemoveWatch: (code: string) => void;
  removing: boolean;
}

const HORIZON_OPTIONS: Array<{ value: Horizon; label: string }> = [
  { value: 'short', label: HORIZON_HINT.short },
  { value: 'swing', label: HORIZON_HINT.swing },
];

const Fig: React.FC<{ label: string; className?: string; children: React.ReactNode }> = ({ label, className, children }) => (
  <div>
    <dt>{label}</dt>
    <dd className={className}>{children}</dd>
  </div>
);

function ConditionRow({ condition, plan }: { condition: Condition; plan: StockPlan }) {
  const showShares =
    condition.shares != null && !textMentionsNumber(condition.thenText, condition.shares, 0);
  const showPrice = condition.price != null && !textMentionsNumber(`${condition.ifText}${condition.thenText}`, condition.price, 2);
  const range =
    plan.nextLimitDown != null && plan.nextLimitUp != null
      ? `（明天涨跌停范围 ${fmtPrice(plan.nextLimitDown)}–${fmtPrice(plan.nextLimitUp)}）`
      : '';
  return (
    <Fragment>
      <span className="if">{condition.ifText}</span>
      <span className="arr" aria-hidden="true">
        →
      </span>
      <span className="then-cell">
        <span className={`then ${actionClass(condition.action)}`}>
          <span className="vh">就</span>
          {condition.thenText}
        </span>
        {showShares || showPrice || condition.outsideLimit ? (
          <span className="cond-meta">
            {showShares ? <span className="num">{fmtNum(condition.shares)} 股</span> : null}
            {showPrice ? <span className="num">价位 {fmtPrice(condition.price)}</span> : null}
            {condition.outsideLimit ? (
              <span className="tag warn" role="note">
                超出涨跌停范围，可能成交不了{range ? <span className="vh">{range}</span> : null}
              </span>
            ) : null}
          </span>
        ) : null}
      </span>
    </Fragment>
  );
}

function figuresFor(
  plan: StockPlan,
  horizonPlan: HorizonPlan | null,
  holding: Holding | null,
): React.ReactNode {
  if (plan.held) {
    const stopPrice = horizonPlan?.stop?.price ?? null;
    return (
      <>
        <Fig label="持仓">
          {holding ? <Count value={holding.shares} suffix=" 股" animateOnMount /> : '—'}
        </Fig>
        <Fig label="浮动盈亏" className={changeClass(holding?.pnl)}>
          {holding ? <Count value={holding.pnl} sign suffix=" 元" animateOnMount /> : '—'}
        </Fig>
        <Fig label="止损价">
          <Count value={stopPrice} decimals={2} animateOnMount />
        </Fig>
        <Fig label="明天跌停价">
          <Count value={plan.nextLimitDown} decimals={2} animateOnMount />
        </Fig>
      </>
    );
  }
  const stopPrice = horizonPlan?.stop?.price ?? horizonPlan?.sizing?.stop.price ?? null;
  return (
    <>
      <Fig label="收盘">
        <Count value={plan.close} decimals={2} animateOnMount />
      </Fig>
      <Fig label="计划买入价">
        <Count value={horizonPlan?.entryPrice ?? null} decimals={2} animateOnMount />
      </Fig>
      <Fig label="止损价">
        <Count value={stopPrice} decimals={2} animateOnMount />
      </Fig>
      <Fig label="跌破就放弃">
        <Count value={horizonPlan?.invalidPrice ?? null} decimals={2} animateOnMount />
      </Fig>
    </>
  );
}

/** The loaded plan of one stock: verdict, key numbers, conditional plan, sizing, switch decision, notes, ladder. */
export const PlanView: React.FC<PlanViewProps> = ({
  plan,
  holding,
  firstHolding,
  rules,
  onSelectStock,
  onAsk,
  onRecordBuy,
  onRemoveWatch,
  removing,
}) => {
  // The horizon choice is remembered per stock; a different stock starts at its own default.
  const [pick, setPick] = useState<{ code: string; horizon: Horizon } | null>(null);
  const horizon: Horizon = pick && pick.code === plan.code ? pick.horizon : plan.defaultHorizon;
  const horizonPlan = plan.horizons[horizon] ?? null;
  const verdict = horizonPlan?.verdict ?? plan.verdict;
  const sizing = horizonPlan?.sizing ?? null;
  const ladderLevels = useMemo(() => buildLadderLevels(plan, horizonPlan), [plan, horizonPlan]);
  const marketText = marketLabel(plan.boardMarket, plan.isSt);
  const summary = horizonPlan ? horizonPlan.headline : '这个周期不给计划。';
  const stop = horizonPlan?.stop ?? null;
  const caption = plan.held
    ? holding
      ? `持仓 ${fmtNum(holding.shares)} 股，成本 ${fmtPrice(holding.cost)}`
      : '关键价位'
    : horizonPlan?.entryPrice != null && verdict !== 'skip'
      ? `按 ${fmtPrice(horizonPlan.entryPrice)} 买入时的价位`
      : '关键价位';

  return (
    <>
      <p className="vh" role="status">
        {plan.name} 的次日计划已载入
      </p>
      <div className="hero">
        <div>
          <div className={`verdict ${VERDICT_BIG_CLASS[verdict]}`} data-testid="mine-verdict">
            {VERDICT_LABEL[verdict]}
          </div>
          <p className="summary">{summary}</p>
          {horizonPlan?.reason ? <p className="meta">原因：{horizonPlan.reason}</p> : null}
          <p className="meta">
            {plan.nextTradeDate} 的计划，基于 {plan.tradeDate} 收盘数据
          </p>
        </div>
        <dl className="figs" aria-label="关键数字">
          {figuresFor(plan, horizonPlan, holding)}
        </dl>
      </div>

      <div className="body">
        <div className="plan">
          <section>
            <div className="sec-head">
              <h3>明天怎么做</h3>
              <SegmentedControl
                ariaLabel="周期"
                options={HORIZON_OPTIONS}
                value={horizon}
                onChange={(next) => setPick({ code: plan.code, horizon: next })}
              />
            </div>
            {horizonPlan && horizonPlan.conditions.length > 0 ? (
              <Stagger key={`${plan.code}-${horizon}`} as="ul" itemAs="li" className="ifthen" gap={0.04} y={8}>
                {horizonPlan.conditions.map((condition, index) => (
                  <ConditionRow key={`${index}-${condition.ifText}`} condition={condition} plan={plan} />
                ))}
              </Stagger>
            ) : (
              <p className="note">没有这个周期的计划。</p>
            )}
            {plan.held ? (
              <p className="note">
                买入日 {holding?.buyDate ?? '未填'}，计划周期 {holding?.horizon ? HORIZON_LABEL[holding.horizon] : '未填'}，
                {holding ? (holding.sellable ? 'T+1 已满足' : (holding.sellableNote ?? 'T+1 尚未满足')) : 'T+1 状态读不到'}
                {holding?.sellable && holding.sellableNote ? `（${holding.sellableNote}）` : ''}。
                {stop ? `止损价 ${fmtPrice(stop.price)}（${stop.formula}），技术支撑只作提示。` : ''}
                {rules && !stop ? `止损幅度 ${fmtNum(rules.stopLossPct, 0)}%。` : ''}
              </p>
            ) : null}
            {plan.held && stop?.warning ? (
              <p className="note warn-t" role="note">
                提示：{stop.warning}
              </p>
            ) : null}
          </section>

          {!plan.held && sizing ? (
            <section>
              <h3>买多少</h3>
              <SizingBlock
                sizing={sizing}
                marketText={marketText}
                limitPct={plan.limitPct}
                heldBlocker={firstHolding}
                onSelectStock={onSelectStock}
              />
            </section>
          ) : null}

          {plan.held && plan.switch ? (
            <section>
              <SwitchCard
                decision={plan.switch}
                heldName={plan.name}
                heldHeadline={summary}
                horizon={horizon}
                onAsk={onAsk}
                onSelectStock={onSelectStock}
              />
            </section>
          ) : null}

          <section>
            <h3>今天的情况</h3>
            <p className="situ">{plan.situation}</p>
          </section>

          {plan.risks.length > 0 ? (
            <section>
              <h3>风险</h3>
              <ul className="risk-list">
                {plan.risks.map((risk) => (
                  <li key={risk}>{risk}</li>
                ))}
              </ul>
            </section>
          ) : null}

          {plan.dataGaps.length > 0 ? (
            <section>
              <div className="notice" role="note">
                <b>数据缺口</b>
                <p>{plan.dataGaps.join('；')}。缺少的数据会降低这条结论的把握。</p>
              </div>
            </section>
          ) : null}

          <div className="actions">
            <button type="button" className="btn primary" onClick={() => onAsk(horizon)}>
              <PlanIcon name="assist" size={16} />
              问助手
            </button>
            {!plan.held && horizonPlan?.verdict === 'buy' ? (
              <button
                type="button"
                className="btn"
                aria-haspopup="dialog"
                onClick={() =>
                  onRecordBuy({
                    code: plan.code,
                    name: plan.name,
                    shares: sizing && sizing.status === 'ok' ? sizing.shares : null,
                    price: sizing?.price ?? horizonPlan.entryPrice,
                    horizon,
                  })
                }
              >
                记录买入
              </button>
            ) : null}
            {!plan.held ? (
              <button type="button" className="btn quiet" disabled={removing} onClick={() => onRemoveWatch(plan.code)}>
                移出自选
              </button>
            ) : null}
          </div>

          <ModelFootnote plan={plan} />
        </div>
        <PriceLadder key={plan.code} levels={ladderLevels} caption={caption} />
      </div>
    </>
  );
};

/** Which model wrote the plan, and the fallback when the primary one failed. */
export const ModelFootnote: React.FC<{ plan: Pick<StockPlan, 'model'> }> = ({ plan }) => {
  const { model } = plan;
  if (!model) {
    return null;
  }
  return (
    <p className="mine-foot">
      分析模型：<span className="num">{model.model}</span>
      {model.fallbackFrom ? (
        <>
          。已回退：原定 <span className="num">{model.fallbackFrom}</span>
          {model.fallbackReason ? `，${model.fallbackReason}` : ''}
        </>
      ) : null}
    </p>
  );
};
