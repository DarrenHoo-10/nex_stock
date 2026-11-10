import type React from 'react';
import { useState } from 'react';
import { useCurrentUser } from '../../../hooks/useCurrentUser';
import type { FollowedBoard, RecommendationResponse } from '../../../types/boards';
import { BOARD_STATE_LABEL, BOARD_TYPE_LABEL, formatPct, signClass } from './boardMeta';
import { PlanSkeleton } from './PlanStatePanels';
import type { RecommendationState } from './useBoardRecommendations';

function formatClock(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function sourceLine(data: RecommendationResponse): string {
  const when = formatClock(data.generatedAt);
  if (data.source === 'model') return `模型挑选${when ? ` · ${when}` : ''}`;
  if (data.source === 'rule') return `按规则排序${data.note ? `：${data.note}` : ''}`;
  return '';
}

interface BoardFocusStripProps {
  recommendations: RecommendationState;
  onRegenerate: () => Promise<string | null>;
  followed: FollowedBoard[];
  followStatus: 'idle' | 'loading' | 'ready' | 'error';
  followError: string | null;
  onRetryFollows: () => void;
  activeId: string | null;
  onSelect: (boardId: string) => void;
  onMessage: (message: string) => void;
}

/**
 * Two windows above the board list: boards suggested for the next session (picked by the analysis model after
 * the 17:00 run, rule ranking as fallback) and the boards the user follows, each with today's state.
 */
export const BoardFocusStrip: React.FC<BoardFocusStripProps> = ({
  recommendations,
  onRegenerate,
  followed,
  followStatus,
  followError,
  onRetryFollows,
  activeId,
  onSelect,
  onMessage,
}) => {
  // Regenerating the shared recommendation (POST /boards/recommendations/refresh) is an administrator action.
  const { isAdmin } = useCurrentUser();
  const [requesting, setRequesting] = useState(false);
  const data = recommendations.status === 'ready' ? recommendations.data : null;
  const generating = data?.status === 'generating' || requesting;

  const regenerate = async () => {
    setRequesting(true);
    const error = await onRegenerate();
    setRequesting(false);
    onMessage(error ?? '正在重新生成推荐板块，完成后自动显示');
  };

  let recBody: React.ReactNode;
  if (recommendations.status === 'loading') {
    recBody = <PlanSkeleton rows={3} label="正在读取推荐板块" />;
  } else if (recommendations.status === 'error') {
    recBody = <p className="note">{recommendations.message}</p>;
  } else if (data && data.items.length > 0) {
    recBody = (
      <ol className="focus-list">
        {data.items.map((item) => (
          <li key={item.board.id}>
            <button
              type="button"
              className="focus-row"
              aria-current={item.board.id === activeId ? 'true' : undefined}
              onClick={() => onSelect(item.board.id)}
            >
              <span className="focus-rank num">{item.rank}</span>
              <span className="focus-main">
                <span className="focus-name">
                  <b>{item.board.name}</b>
                  <span className="tag line">{BOARD_TYPE_LABEL[item.board.boardType]}</span>
                  {item.board.state !== 'none' ? <span className="tag wait">{BOARD_STATE_LABEL[item.board.state]}</span> : null}
                  <span className={`num ${signClass(item.board.changePct)}`}>{formatPct(item.board.changePct)}</span>
                </span>
                {item.reason ? <span className="focus-reason">{item.reason}</span> : null}
              </span>
            </button>
          </li>
        ))}
      </ol>
    );
  } else if (data?.status === 'no_run') {
    recBody = <p className="note">还没有完成的板块运行，17:00 运行后会给出推荐。</p>;
  } else if (generating) {
    recBody = <p className="note" role="status">正在生成推荐板块…</p>;
  } else if (data?.note) {
    recBody = <p className="note">{data.note}</p>;
  } else {
    recBody = <p className="note">这次运行还没有推荐。点「重新生成」，模型会根据今天的异动挑选。</p>;
  }

  let followBody: React.ReactNode;
  if (followStatus === 'loading' || followStatus === 'idle') {
    followBody = <PlanSkeleton rows={2} label="正在读取关注的板块" />;
  } else if (followStatus === 'error') {
    followBody = (
      <p className="note">
        {followError}
        <button type="button" className="btn quiet sm" onClick={onRetryFollows}>
          重试
        </button>
      </p>
    );
  } else if (followed.length === 0) {
    followBody = <p className="note">在板块详情里点「关注」，关注的板块会一直显示在这里。</p>;
  } else {
    followBody = (
      <ul className="follow-list">
        {followed.map((board) => {
          const today = board.today;
          return (
            <li key={board.id}>
              <button
                type="button"
                className="follow-chip"
                aria-current={board.id === activeId ? 'true' : undefined}
                onClick={() => onSelect(board.id)}
              >
                <b>{board.name}</b>
                {today && today.state !== 'none' ? <span className="tag wait">{BOARD_STATE_LABEL[today.state]}</span> : null}
                <span className={`num ${signClass(today?.changePct ?? null)}`}>{formatPct(today?.changePct ?? null)}</span>
              </button>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <div className="focus-grid">
      <section className="win focus-win" aria-labelledby="focus-rec-title">
        <div className="focus-head">
          <h2 id="focus-rec-title">今日建议</h2>
          {data && data.items.length ? <span className="focus-meta">{sourceLine(data)}</span> : null}
          <span className="spacer" aria-hidden="true" />
          {isAdmin ? (
            <button
              type="button"
              className="btn quiet sm"
              disabled={generating || data?.status === 'no_run'}
              onClick={() => void regenerate()}
            >
              {generating ? '生成中…' : '重新生成'}
            </button>
          ) : null}
        </div>
        {recBody}
      </section>
      <section className="win focus-win" aria-labelledby="focus-follow-title">
        <div className="focus-head">
          <h2 id="focus-follow-title">我关注的</h2>
          {followed.length ? <span className="focus-meta">{followed.length} 个</span> : null}
        </div>
        {followBody}
      </section>
    </div>
  );
};
