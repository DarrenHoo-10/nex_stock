import type React from 'react';
import { CountUp } from '../motion';
import type { MarketSummary } from '../../../types/boards';
import { formatPct, signClass } from './boardMeta';

function formatTurnover(turnoverYi: number | null): string | null {
  if (typeof turnoverYi !== 'number' || !Number.isFinite(turnoverYi)) return null;
  return turnoverYi >= 10000 ? `${(turnoverYi / 10000).toFixed(2)} 万亿` : `${Math.round(turnoverYi)} 亿`;
}

const Stat: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <span>
    {label}
    <b>{children}</b>
  </span>
);

/** Market summary pill: benchmark change, breadth, limit up/down and the broad-rally flag. */
export const MarketStrip: React.FC<{ market: MarketSummary }> = ({ market }) => {
  const turnover = formatTurnover(market.turnoverYi);
  return (
    <>
      <section className="market glass" aria-label="市场概况" data-testid="market-strip">
        <span>
          {market.indexName}
          <b className={`num ${signClass(market.indexChangePct)}`}>{formatPct(market.indexChangePct)}</b>
        </span>
        <Stat label="上涨家数">
          <CountUp value={market.upCount} />
        </Stat>
        <Stat label="下跌家数">
          <CountUp value={market.downCount} />
        </Stat>
        <Stat label="涨停">
          <CountUp value={market.limitUpCount} />
        </Stat>
        <Stat label="跌停">
          <CountUp value={market.limitDownCount} />
        </Stat>
        {turnover ? <Stat label="成交额">{turnover}</Stat> : null}
        <Stat label="普涨保护">{market.broadRally ? '已触发' : '未触发'}</Stat>
      </section>
      {market.broadRally ? (
        <p className="notice market-notice" role="note">
          <b>普涨降权</b>
          <span>全市场普涨，涨停与上涨家数信号已降权，板块只按超额收益排名判断。</span>
        </p>
      ) : null}
      {market.note ? (
        <p className="notice market-notice" role="note">
          <b>说明</b>
          <span>{market.note}</span>
        </p>
      ) : null}
    </>
  );
};
