import type React from 'react';
import { useMemo } from 'react';
import { StockChartBody } from '../quote/StockChartDialog';
import { targetFromCode } from '../quote/quoteTarget';
import '../../../styles/plan-quote.css';

export interface PlanKlineProps {
  code: string;
  name: string;
}

/** The planned stock's K-line (same chart as the quote dialog, without its title block) under the verdict hero. */
export const PlanKline: React.FC<PlanKlineProps> = ({ code, name }) => {
  const target = useMemo(() => targetFromCode(code, name), [code, name]);
  if (!target) {
    return null;
  }
  return (
    <section className="plan-kline" aria-label={`${name} K 线`} data-testid="plan-kline">
      <StockChartBody key={target.code} target={target} hideTitle />
    </section>
  );
};
