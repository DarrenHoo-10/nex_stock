import type React from 'react';
import type { ScreeningStrategy } from '../../../api/screening';
import { strategyLabel } from './pickMeta';

export interface StrategyChipsProps {
  strategies: ScreeningStrategy[];
  value: string | null;
  onChange: (id: string) => void;
  disabled?: boolean;
}

/** Strategy chips from the screening strategies API; the selected strategy's description follows. */
export const StrategyChips: React.FC<StrategyChipsProps> = ({ strategies, value, onChange, disabled = false }) => {
  const current = strategies.find((strategy) => strategy.id === value);
  return (
    <div className="strategy-block">
      <p className="k" id="pick-strategy-label">
        策略
      </p>
      <div className="chips" role="group" aria-labelledby="pick-strategy-label">
        {strategies.map((strategy) => (
          <button
            key={strategy.id}
            type="button"
            className="chip"
            aria-pressed={strategy.id === value}
            disabled={disabled}
            onClick={() => onChange(strategy.id)}
          >
            {strategyLabel(strategy)}
          </button>
        ))}
      </div>
      {current?.description ? <p className="strategy-desc">{current.description}</p> : null}
    </div>
  );
};
