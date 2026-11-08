import type React from 'react';
import { CountUp as Count } from '../motion/CountUp';
import type { Holding, Sizing } from '../../../types/dailyPlan';
import { fmtNum, fmtPct, fmtPrice } from './format';

export interface SizingBlockProps {
  sizing: Sizing;
  /** Market label for the limit-down sentence, e.g. 「主板」. */
  marketText: string;
  limitPct: number;
  /** The holding that fills the max-holdings slot (for the 「看换股判断」 link). */
  heldBlocker: Pick<Holding, 'code' | 'name'> | null;
  onSelectStock: (code: string) => void;
}

const floorShares = (value: number) => fmtNum(Math.floor(value));

/** 「买多少」: the backend's sizing result, its status messages and the expandable 「怎么算的」. */
export const SizingBlock: React.FC<SizingBlockProps> = ({ sizing, marketText, limitPct, heldBlocker, onSelectStock }) => {
  const calc = (
    <>
      {sizing.steps.length > 0 ? (
        <details className="calc">
          <summary>怎么算的</summary>
          <ol>
            {sizing.steps.map((step, index) => (
              <li key={`${index}-${step}`}>{step}</li>
            ))}
          </ol>
          <CapBreakdown sizing={sizing} />
        </details>
      ) : null}
    </>
  );

  if (sizing.status === 'max_holdings') {
    return (
      <div className="notice" role="note">
        <b>不新开仓</b>
        <p>
          {sizing.message ?? '已达到最多持股数，按规则不再新开仓，买入改为看换股判断。'}
          {heldBlocker ? (
            <>
              {' '}
              <button type="button" className="link-btn" onClick={() => onSelectStock(heldBlocker.code)}>
                看换股判断
              </button>
            </>
          ) : null}
        </p>
      </div>
    );
  }

  if (sizing.status === 'below_lot') {
    return (
      <>
        <div className="notice" role="note">
          <b>不建议买入</b>
          <p>
            {sizing.message ??
              `按当前规则可买金额不够一手（${fmtNum(sizing.lot)} 股 × ${fmtPrice(sizing.price)} = ${fmtNum(sizing.lot * sizing.price)} 元）。`}
          </p>
        </div>
        {calc}
      </>
    );
  }

  if (sizing.status === 'no_price') {
    return (
      <div className="notice" role="note">
        <b>没有买入价</b>
        <p>{sizing.message ?? '这个周期还没有可用的买入价，暂时算不出股数。'}</p>
      </div>
    );
  }

  return (
    <div className="inset" role="group" aria-label="买入股数">
      <div className="big">
        <Count value={sizing.shares} animateOnMount />
        <small>股，在 {fmtPrice(sizing.price)} 买入</small>
      </div>
      <dl className="facts">
        <Fact label="金额" value={`${fmtNum(sizing.amount)} 元`} />
        <Fact label="占总资金" value={fmtPct(sizing.capitalPct)} />
        <Fact label="止损价" value={fmtPrice(sizing.stop.price)} />
        <Fact label="到止损亏损" value={`${fmtNum(sizing.maxLoss)} 元`} />
        <Fact label="买后总仓位" value={fmtPct(sizing.postPositionPct)} />
      </dl>
      <p className="note">
        收盘跌破 {fmtPrice(sizing.stop.price)}，下一交易日全部卖出。{marketText}单日最大跌幅 {fmtNum(limitPct, 0)}%：
        如果卖出当天直接跌停，成交价约 {fmtPrice(sizing.limitDownPrice)}，亏损约 {fmtNum(sizing.limitDownLoss)} 元。
      </p>
      {sizing.stop.warning ? (
        <p className="note warn-t" role="note">
          提示：{sizing.stop.warning}
        </p>
      ) : null}
      {calc}
    </div>
  );
};

const Fact: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <dt className="k">{label}</dt>
    <dd className="v num">{value}</dd>
  </div>
);

/** The three caps behind the minimum; the tightest one is marked. */
const CapBreakdown: React.FC<{ sizing: Sizing }> = ({ sizing }) => {
  const caps = [
    { key: 'single', label: '单只上限', value: sizing.bySingle },
    { key: 'total', label: '总仓位上限', value: sizing.byTotal },
    { key: 'cash', label: '可用现金', value: sizing.byCash },
  ];
  const tightest = Math.min(...caps.map((cap) => cap.value));
  return (
    <dl className="caps">
      {caps.map((cap) => (
        <div key={cap.key} className={cap.value === tightest ? 'tight' : undefined}>
          <dt>{cap.label}</dt>
          <dd className="num">
            {floorShares(cap.value)} 股{cap.value === tightest ? <span className="vh">（最紧的一项）</span> : null}
          </dd>
        </div>
      ))}
      <div>
        <dt>每手</dt>
        <dd className="num">{fmtNum(sizing.lot)} 股</dd>
      </div>
    </dl>
  );
};
