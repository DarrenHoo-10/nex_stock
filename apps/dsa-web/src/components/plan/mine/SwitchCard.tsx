import type React from 'react';
import type { Horizon, SwitchDecision } from '../../../types/dailyPlan';
import { HORIZON_LABEL, VERDICT_LABEL } from './format';

export interface SwitchCardProps {
  decision: SwitchDecision;
  heldName: string;
  /** What the held stock does in the selected horizon (its headline). */
  heldHeadline: string;
  horizon: Horizon;
  onAsk: (horizon?: Horizon) => void;
  onSelectStock: (code: string) => void;
}

const MET_TEXT = { true: '已满足', false: '未满足', null: '待判断' } as const;

/** 换股判断: default is 「不换」; both conditions must hold to switch. */
export const SwitchCard: React.FC<SwitchCardProps> = ({ decision, heldName, heldHeadline, horizon, onAsk, onSelectStock }) => {
  const { candidate } = decision;
  const keep = decision.decision === 'keep';
  return (
    <div className="inset" id="switch" role="group" aria-label="换股判断">
      <div className="sec-head">
        <h3>换股判断：{decision.summary || (keep ? '不换' : '换股')}</h3>
        <button type="button" className="btn quiet sm" onClick={() => onAsk(horizon)}>
          提问
        </button>
      </div>
      {candidate ? (
        <div className="cmp">
          <div>
            <p className="k">当前持仓（{HORIZON_LABEL[horizon]}）</p>
            <p>
              <b>{heldName}</b> {heldHeadline}
            </p>
          </div>
          <div>
            <p className="k">最好的换入候选</p>
            <p>
              <button type="button" className="link-btn strong" onClick={() => onSelectStock(candidate.code)}>
                {candidate.name}
              </button>{' '}
              <span className="tag skip">{VERDICT_LABEL[candidate.verdict]}</span> {candidate.reason}
            </p>
          </div>
        </div>
      ) : (
        <p className="note">自选里今天没有明显更好的换入候选。</p>
      )}
      {decision.conditions.length > 0 ? (
        <>
          <p className="note switch-lead">两个条件同时满足才换，默认不换：</p>
          <ul className="cond">
            {decision.conditions.map((condition) => (
              <li key={condition.text}>
                <span className={`mark${condition.met ? ' on' : ''}`} aria-hidden="true" />
                <span>
                  {condition.text}
                  <span className="vh">（{MET_TEXT[String(condition.met) as keyof typeof MET_TEXT]}）</span>
                </span>
                <span className={`tag ${condition.met ? 'ok' : 'skip'} met-tag`} aria-hidden="true">
                  {MET_TEXT[String(condition.met) as keyof typeof MET_TEXT]}
                </span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {decision.costs.length > 0 ? (
        <>
          <p className="note switch-lead">换股成本</p>
          <ul className="risk-list switch-costs">
            {decision.costs.map((cost) => (
              <li key={cost}>{cost}</li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
};
