import type React from 'react';
import { CountUp as Count } from '../motion/CountUp';
import { PlanIcon } from '../PlanIcon';
import type { AccountResponse, Horizon, StockPlan, StockPlanSummary } from '../../../types/dailyPlan';
import { changeClass, marketLabel } from './format';
import { ModelFootnote, PlanView } from './PlanView';
import type { RecordBuyPrefill } from './PlanView';
import type { Loadable } from './useMineData';

export interface StockDetailProps {
  code: string;
  /** Row of the list for this code; null when the code is not in the watchlist or holdings. */
  summary: StockPlanSummary | null;
  detail: Loadable<StockPlan>;
  account: Loadable<AccountResponse>;
  onRetry: () => void;
  onBack: () => void;
  onSelectStock: (code: string) => void;
  onAsk: (horizon?: Horizon) => void;
  onRecordBuy: (prefill: RecordBuyPrefill) => void;
  onAddWatch: (code: string) => void;
  onRemoveWatch: (code: string) => void;
  busy: boolean;
}

const BackButton: React.FC<{ onBack: () => void }> = ({ onBack }) => (
  <button type="button" className="btn quiet back" onClick={onBack}>
    ‹ 返回列表
  </button>
);

const DetailHeader: React.FC<{ summary: StockPlanSummary; plan: StockPlan | null }> = ({ summary, plan }) => {
  const close = plan?.close ?? summary.close;
  const change = plan?.changePct ?? summary.changePct;
  const suspended = summary.verdict === 'suspended';
  return (
    <header className="d-head">
      <div className="id">
        <h2 id="plan-page-title">{summary.name}</h2>
        <span className="cd">{summary.code}</span>
        <span className="tag line">{marketLabel(summary.boardMarket, summary.isSt)}</span>
        {summary.held ? <span className="tag hold">持仓</span> : null}
      </div>
      {!suspended && close != null ? (
        <div className="d-price">
          <span>
            <Count value={close} decimals={2} animateOnMount />
          </span>
          <span className={changeClass(change)}>
            <Count value={change} decimals={2} sign suffix="%" />
          </span>
        </div>
      ) : null}
    </header>
  );
};

const DetailSkeleton: React.FC = () => (
  <div className="detail-sk" aria-busy="true" aria-label="计划载入中">
    <span className="sk sk-line sk-hero" />
    <span className="sk sk-line sk-wide" />
    <span className="sk sk-line sk-mid" />
    <div className="sk-grid">
      <span className="sk sk-block" />
      <span className="sk sk-block" />
    </div>
  </div>
);

/** Verdict-sized state (停牌 / 分析失败 / 暂无计划) with an explanation and the actions that still make sense. */
const StateHero: React.FC<{
  title: string;
  text: string;
  extra?: string;
  summary: StockPlanSummary;
  onAsk: (horizon?: Horizon) => void;
  onRemoveWatch: (code: string) => void;
  busy: boolean;
}> = ({ title, text, extra, summary, onAsk, onRemoveWatch, busy }) => (
  <>
    <div className="hero">
      <div>
        <div className="verdict v-skip v-state">{title}</div>
        <p className="summary">{text}</p>
        {extra ? <p className="meta">{extra}</p> : null}
      </div>
    </div>
    <div className="actions">
      <button type="button" className="btn primary" onClick={() => onAsk()}>
        <PlanIcon name="assist" size={16} />
        问助手
      </button>
      {!summary.held ? (
        <button type="button" className="btn quiet" disabled={busy} onClick={() => onRemoveWatch(summary.code)}>
          移出自选
        </button>
      ) : null}
    </div>
  </>
);

/** Right window of the page: one stock's plan, or the loading / error / missing variants of it. */
export const StockDetail: React.FC<StockDetailProps> = ({
  code,
  summary,
  detail,
  account,
  onRetry,
  onBack,
  onSelectStock,
  onAsk,
  onRecordBuy,
  onAddWatch,
  onRemoveWatch,
  busy,
}) => {
  if (!summary) {
    return (
      <>
        <BackButton onBack={onBack} />
        <section className="empty sm" aria-labelledby="plan-page-title">
          <h3 id="plan-page-title">{code} 不在自选和持仓里</h3>
          <p>加入自选后，下一次运行会生成它的次日计划。</p>
          {/^\d{6}$/.test(code) ? (
            <div className="actions">
              <button type="button" className="btn primary" disabled={busy} onClick={() => onAddWatch(code)}>
                加入自选
              </button>
            </div>
          ) : null}
        </section>
      </>
    );
  }

  const plan = detail.status === 'ready' ? detail.data : null;
  const suspended = summary.verdict === 'suspended';
  const accountData = account.status === 'ready' ? account.data : null;
  const holding = accountData?.holdings.find((item) => item.code === summary.code) ?? null;

  let body: React.ReactNode;
  if (suspended) {
    body = (
      <StateHero
        title="停牌"
        text={plan?.situation || summary.failureReason || '这只股票停牌中。'}
        extra="复牌前不给计划，复牌后的第一个运行日会重新生成。"
        summary={summary}
        onAsk={onAsk}
        onRemoveWatch={onRemoveWatch}
        busy={busy}
      />
    );
  } else if (detail.status === 'loading') {
    body = <DetailSkeleton />;
  } else if (detail.status === 'error' || detail.status === 'unavailable') {
    body = (
      <section className="empty sm" role="alert">
        <h3>{detail.status === 'unavailable' ? '次日计划服务未就绪' : '这只股票的计划读取失败'}</h3>
        {detail.status === 'error' ? <p>{detail.message}</p> : null}
        <div className="actions">
          <button type="button" className="btn" onClick={onRetry}>
            重试
          </button>
        </div>
      </section>
    );
  } else if (!plan || summary.status !== 'ready' || plan.status !== 'ready') {
    const failed = summary.status === 'failed' || plan?.status === 'failed';
    body = (
      <StateHero
        title={failed ? '分析失败' : '暂无计划'}
        text={
          failed
            ? summary.failureReason || plan?.failureReason || '当日分析没有完成。'
            : '这只股票还没有次日计划。'
        }
        extra={failed ? '下一次运行会重新分析；点页头的运行状态可以看每一步的结果。' : '新加入的股票会在下一次运行后生成计划。'}
        summary={summary}
        onAsk={onAsk}
        onRemoveWatch={onRemoveWatch}
        busy={busy}
      />
    );
  } else {
    body = (
      <PlanView
        plan={plan}
        holding={holding}
        firstHolding={accountData?.holdings[0] ?? null}
        rules={accountData?.rules ?? null}
        onSelectStock={onSelectStock}
        onAsk={onAsk}
        onRecordBuy={onRecordBuy}
        onRemoveWatch={onRemoveWatch}
        removing={busy}
      />
    );
  }

  return (
    <>
      <BackButton onBack={onBack} />
      <DetailHeader summary={summary} plan={plan} />
      {body}
      {suspended && plan ? <ModelFootnote plan={plan} /> : null}
    </>
  );
};
