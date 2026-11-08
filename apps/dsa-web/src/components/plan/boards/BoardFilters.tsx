import type React from 'react';
import { SegmentedControl } from '../motion';
import type { BoardState, BoardType } from '../../../types/boards';
import { BOARD_FILTER_STATES, BOARD_STATE_LABEL } from './boardMeta';

export type BoardTypeFilter = 'all' | BoardType;
export type BoardStateFilter = 'all' | BoardState;

const TYPE_OPTIONS = [
  { value: 'all' as const, label: '全部' },
  { value: 'industry' as const, label: '行业' },
  { value: 'concept' as const, label: '概念' },
];

export interface BoardFiltersProps {
  type: BoardTypeFilter;
  state: BoardStateFilter;
  onTypeChange: (value: BoardTypeFilter) => void;
  onStateChange: (value: BoardStateFilter) => void;
}

/** Industry/concept segmented control and the state chips of the board list window. */
export const BoardFilters: React.FC<BoardFiltersProps> = ({ type, state, onTypeChange, onStateChange }) => (
  <div className="filters">
    <SegmentedControl options={TYPE_OPTIONS} value={type} onChange={onTypeChange} ariaLabel="板块类型" />
    <div className="chips" role="group" aria-label="状态">
      <button type="button" className="chip" aria-pressed={state === 'all'} onClick={() => onStateChange('all')}>
        全部
      </button>
      {BOARD_FILTER_STATES.map((value) => (
        <button
          key={value}
          type="button"
          className="chip"
          aria-pressed={state === value}
          onClick={() => onStateChange(state === value ? 'all' : value)}
        >
          {BOARD_STATE_LABEL[value]}
        </button>
      ))}
    </div>
  </div>
);
