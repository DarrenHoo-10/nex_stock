import type React from 'react';
import { Link } from 'react-router-dom';
import { CountUp as Count } from '../motion/CountUp';
import type { AccountResponse } from '../../../types/dailyPlan';
import { fmtNum } from './format';
import type { Loadable } from './useMineData';

export interface AccountStripProps {
  account: Loadable<AccountResponse>;
  onEdit: () => void;
  onRetry: () => void;
}

/** Account block at the top of the list window: capital, cash, market value, position and the rules in force. */
export const AccountStrip: React.FC<AccountStripProps> = ({ account, onEdit, onRetry }) => {
  if (account.status === 'loading') {
    return (
      <div className="acct" aria-busy="true" aria-label="账户载入中">
        <div className="stats mine-stats">
          {[0, 1, 2, 3].map((index) => (
            <div key={index}>
              <span className="sk sk-line sk-xs" />
              <span className="sk sk-line sk-lg" />
            </div>
          ))}
        </div>
        <span className="sk sk-line sk-wide" />
      </div>
    );
  }

  if (account.status !== 'ready') {
    return (
      <div className="acct">
        <p className="note" role="alert">
          {account.status === 'unavailable' ? '次日计划服务未就绪，账户暂时读不到。' : '账户读取失败。'}
        </p>
        <div className="acct-acts">
          <button type="button" className="btn sm" onClick={onRetry}>
            重试
          </button>
        </div>
      </div>
    );
  }

  const { data } = account;
  const { rules } = data;
  return (
    <div className="acct">
      <dl className="stats mine-stats" aria-label="账户">
        <div>
          <dt className="k">总资金</dt>
          <dd className="v">
            <Count value={data.totalCapital} animateOnMount />
          </dd>
        </div>
        <div>
          <dt className="k">可用现金</dt>
          <dd className="v">
            <Count value={data.cash} animateOnMount />
          </dd>
        </div>
        <div>
          <dt className="k">持仓市值</dt>
          <dd className="v">
            <Count value={data.marketValue} animateOnMount />
          </dd>
        </div>
        <div>
          <dt className="k">仓位</dt>
          <dd className="v">
            <Count value={data.positionPct} decimals={1} suffix="%" animateOnMount />
          </dd>
        </div>
      </dl>
      <p className="rules-line">
        规则：止损 {fmtNum(rules.stopLossPct, rules.stopLossPct % 1 ? 1 : 0)}%，最多 {rules.maxHoldings} 只，单只上限{' '}
        {fmtNum(rules.maxSinglePct, rules.maxSinglePct % 1 ? 1 : 0)}%
        {rules.maxSinglePctDerived ? '（按总仓位推出）' : ''}
        {rules.maxTotalPct < 100 ? `，总仓位上限 ${fmtNum(rules.maxTotalPct, 0)}%` : ''}
      </p>
      <div className="acct-acts">
        <button type="button" className="btn sm" aria-haspopup="dialog" onClick={onEdit}>
          持仓与资金
        </button>
        <Link className="btn quiet sm" to="/preferences">
          改规则
        </Link>
      </div>
    </div>
  );
};
