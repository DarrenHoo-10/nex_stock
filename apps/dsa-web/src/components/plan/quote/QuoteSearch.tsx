import type React from 'react';
import { useState } from 'react';
import { StockChartDialog } from './StockChartDialog';
import { StockSearchBox } from './StockSearchBox';
import type { QuoteTarget } from './quoteTarget';

export interface QuoteSearchProps {
  /** Adds a symbol (watchlist code form) to the watchlist; omitted hides the dialog's button. */
  onAddWatch?: (watchCode: string) => void;
}

/** Header search box plus the K-line / volume / chip distribution dialog it opens. */
export const QuoteSearch: React.FC<QuoteSearchProps> = ({ onAddWatch }) => {
  const [target, setTarget] = useState<QuoteTarget | null>(null);
  return (
    <>
      <StockSearchBox onOpen={setTarget} />
      <StockChartDialog
        target={target}
        onClose={() => setTarget(null)}
        onAddWatch={onAddWatch ? (selected) => onAddWatch(selected.watchCode) : undefined}
      />
    </>
  );
};
