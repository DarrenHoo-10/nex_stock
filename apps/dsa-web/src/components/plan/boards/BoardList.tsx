import type React from 'react';
import { LayoutGroup, motion } from 'motion/react';
import type { BoardRow } from '../../../types/boards';
import { PLAN_INSTANT, PLAN_SPRING, StaggerItem, usePlanReducedMotion } from '../motion';
import {
  BOARD_STATE_LABEL,
  BOARD_TAG_LABEL,
  BOARD_TYPE_LABEL,
  BOARD_WARN_TAGS,
  formatPct,
  formatRatio,
  isNumber,
  meterPercent,
  signClass,
} from './boardMeta';

export const BOARD_DETAIL_ID = 'board-detail';

export interface BoardListProps {
  boards: BoardRow[];
  activeId: string | null;
  onSelect: (id: string) => void;
  /** Ids of followed boards (shown with a star). */
  followedIds?: ReadonlySet<string>;
}

const BoardRowItem: React.FC<{
  board: BoardRow;
  index: number;
  active: boolean;
  followed: boolean;
  onSelect: (id: string) => void;
}> = ({ board, index, active, followed, onSelect }) => {
  const reduced = usePlanReducedMotion();
  const warnState = board.state === 'insufficient_history';
  return (
    <StaggerItem as="li" index={index}>
      <button
        type="button"
        className="row board-row"
        aria-expanded={active}
        aria-controls={BOARD_DETAIL_ID}
        aria-current={active ? 'true' : undefined}
        data-board-id={board.id}
        onClick={() => onSelect(board.id)}
      >
        {active ? (
          <motion.span className="ind" layoutId="board-row-ind" transition={reduced ? PLAN_INSTANT : PLAN_SPRING} />
        ) : null}
        <span className="r-top">
          <span className="nm">{board.name}</span>
          {followed ? (
            <span className="follow-star" role="img" aria-label="已关注">
              ★
            </span>
          ) : null}
          <span className="tag line">{BOARD_TYPE_LABEL[board.boardType]}</span>
          {board.state !== 'none' ? (
            <span className={`tag ${warnState ? 'warn' : 'line'}`}>{BOARD_STATE_LABEL[board.state]}</span>
          ) : null}
          {board.tags.map((tag) => (
            <span key={tag} className={`tag${BOARD_WARN_TAGS.has(tag) ? ' warn' : ''}`}>
              {BOARD_TAG_LABEL[tag]}
            </span>
          ))}
        </span>
        <span className="r-px">
          <b className={`num ${signClass(board.changePct)}`}>{formatPct(board.changePct)}</b>
          <span className="num flat">{formatRatio(board.amountRatio)}</span>
        </span>
        <span className="b-one">
          <span className="meter" aria-hidden="true">
            <i className={isNumber(board.amountRatio) && board.amountRatio >= 2.5 ? 'hot' : ''} style={{ width: `${meterPercent(board.amountRatio)}%` }} />
            <b style={{ left: '50%' }} />
            <b style={{ left: '83.3%' }} />
          </span>
          <span className="b-stats">
            <span>
              5 日 <span className={`num ${signClass(board.change5dPct)}`}>{formatPct(board.change5dPct)}</span>
            </span>
            <span>
              超额 <span className={`num ${signClass(board.excessPct)}`}>{formatPct(board.excessPct)}</span>
            </span>
            <span>
              上涨 <span className="num">{isNumber(board.breadthPct) ? `${Math.round(board.breadthPct)}%` : '—'}</span>
            </span>
            {board.boardType === 'concept' ? (
              <span>
                涨停 <span className="num">{isNumber(board.limitUpCount) ? board.limitUpCount : '—'}</span>
              </span>
            ) : null}
          </span>
        </span>
        <span className="one board-reason">{board.reason}</span>
      </button>
    </StaggerItem>
  );
};

/** Board rows of the list window; the active row drives the detail window (aria-expanded + aria-controls). */
export const BoardList: React.FC<BoardListProps> = ({ boards, activeId, onSelect, followedIds }) => (
  <LayoutGroup id="board-list">
    <ul className="rows">
      {boards.map((board, index) => (
        <BoardRowItem
          key={board.id}
          board={board}
          index={index}
          active={board.id === activeId}
          followed={followedIds?.has(board.id) ?? false}
          onSelect={onSelect}
        />
      ))}
    </ul>
  </LayoutGroup>
);
