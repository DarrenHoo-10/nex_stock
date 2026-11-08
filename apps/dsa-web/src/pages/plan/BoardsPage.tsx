import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { usePlanUiStore } from '../../stores/planUiStore';
import { useBoardFollowStore } from '../../stores/boardFollowStore';
import { showPlanToast } from '../../stores/planToastStore';
import {
  BOARD_DETAIL_ID,
  BoardDetailPanel,
  BoardFocusStrip,
  BoardFilters,
  BoardList,
  MarketStrip,
  PlanErrorPanel,
  PlanSkeleton,
  PlanUnavailablePanel,
  useBoardAnomalies,
  useBoardDetail,
  useBoardRecommendations,
} from '../../components/plan/boards';
import type { BoardStateFilter, BoardTypeFilter } from '../../components/plan/boards';
import { StaggerItem } from '../../components/plan/motion';
import '../../styles/plan-boards.css';

const NARROW_QUERY = '(max-width: 860px)';

function isNarrow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(NARROW_QUERY).matches;
}

/** 板块异动: market summary, filterable board list and the detail window of the active board. */
const BoardsPage: React.FC = () => {
  const { t } = useUiLanguage();
  const navigate = useNavigate();
  const title = t('plan.page.boards.title');
  const openAssistant = usePlanUiStore((state) => state.openAssistant);

  const anomalies = useBoardAnomalies();
  const [typeFilter, setTypeFilter] = useState<BoardTypeFilter>('all');
  const [stateFilter, setStateFilter] = useState<BoardStateFilter>('all');
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // A board opened from 今日建议 / 我关注的 stays shown even when the list filters (or the list itself) do not include it.
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const recommendations = useBoardRecommendations();
  const follows = useBoardFollowStore();
  const loadFollows = follows.load;
  useEffect(() => {
    void loadFollows();
  }, [loadFollows]);
  const followedIds = useMemo(() => new Set(follows.boards.map((board) => board.id)), [follows.boards]);
  const [showDetail, setShowDetail] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusDetailRef = useRef(false);

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  const allBoards = useMemo(() => (anomalies.status === 'ready' ? anomalies.data.boards : []), [anomalies]);
  const visibleBoards = useMemo(
    () =>
      allBoards.filter(
        (board) =>
          (typeFilter === 'all' || board.boardType === typeFilter) && (stateFilter === 'all' || board.state === stateFilter),
      ),
    [allBoards, typeFilter, stateFilter],
  );
  // The selection survives filtering while its row is visible; otherwise the first visible row is shown.
  const activeId =
    pinnedId ?? visibleBoards.find((board) => board.id === selectedId)?.id ?? visibleBoards[0]?.id ?? null;
  const detail = useBoardDetail(activeId);

  const handleSelect = useCallback((id: string) => {
    setPinnedId(null);
    setSelectedId(id);
    if (isNarrow()) {
      focusDetailRef.current = true;
      setShowDetail(true);
      window.scrollTo({ top: 0 });
    }
  }, []);

  const handleFocusSelect = useCallback(
    (id: string) => {
      handleSelect(id);
      setPinnedId(id);
    },
    [handleSelect],
  );

  const handleBack = useCallback(() => {
    setShowDetail(false);
  }, []);

  // Narrow layout: move focus into the detail window once it has content.
  useEffect(() => {
    if (focusDetailRef.current && detail.status === 'ready') {
      focusDetailRef.current = false;
      headingRef.current?.focus({ preventScroll: true });
    }
  }, [detail.status, activeId]);

  const handlePick = useCallback(
    (boardId: string) => navigate(`/picks?board=${encodeURIComponent(boardId)}`),
    [navigate],
  );
  // Both the header and the detail button open the assistant on the board being shown.
  const activeBoard = detail.status === 'ready' ? detail.data : null;
  const handleAsk = useCallback(
    () =>
      openAssistant(
        activeBoard
          ? { kind: 'board', boardId: activeBoard.id, boardName: activeBoard.name, tradeDate: anomalies.status === 'ready' ? anomalies.data.tradeDate : null }
          : { kind: 'none' },
      ),
    [openAssistant, activeBoard, anomalies],
  );

  const tradeDate = anomalies.status === 'ready' ? anomalies.data.tradeDate : '';
  const benchmark = anomalies.status === 'ready' ? anomalies.data.market.indexName : '中证全指';
  const subtitle = tradeDate
    ? `${tradeDate} 收盘，按成交额放大、相对${benchmark}的超额收益和板块内部强度判定`
    : t('plan.page.boards.subtitle');

  let body: React.ReactNode;
  if (anomalies.status === 'unavailable') {
    body = (
      <section className="win" aria-label="次日计划服务状态">
        <PlanUnavailablePanel />
      </section>
    );
  } else if (anomalies.status === 'error') {
    body = (
      <section className="win" aria-label="板块数据">
        <PlanErrorPanel message={anomalies.message} onRetry={anomalies.reload} title="板块数据加载失败" />
      </section>
    );
  } else if (anomalies.status === 'loading') {
    body = (
      <>
        <div className="market glass market-skel" aria-hidden="true" />
        <div className="grid2">
          <section className="win side" aria-label="板块列表" aria-busy="true">
            <div className="list-scroll">
              <PlanSkeleton rows={6} label="正在加载板块列表" className="skel-rows" />
            </div>
          </section>
          <article className="win detail" aria-busy="true">
            <PlanSkeleton rows={5} label="正在加载板块详情" />
          </article>
        </div>
      </>
    );
  } else {
    const { data } = anomalies;
    let detailBody: React.ReactNode;
    if (!activeId) {
      detailBody = (
        <div className="empty sm">
          <h3>没有可显示的板块</h3>
          <p>调整左侧的类型或状态筛选后，这里会显示板块详情。</p>
        </div>
      );
    } else if (detail.status === 'loading') {
      detailBody = <PlanSkeleton rows={6} label="正在加载板块详情" />;
    } else if (detail.status === 'unavailable') {
      detailBody = <PlanUnavailablePanel compact headingLevel={3} />;
    } else if (detail.status === 'error') {
      detailBody = <PlanErrorPanel compact message={detail.message} onRetry={detail.reload} title="板块详情加载失败" />;
    } else {
      const shown = detail.data;
      detailBody = (
        <BoardDetailPanel
          key={shown.id}
          ref={headingRef}
          detail={shown}
          tradeDate={data.tradeDate}
          benchmark={benchmark}
          onBack={handleBack}
          onPick={handlePick}
          onAsk={handleAsk}
          followed={followedIds.has(shown.id)}
          followPending={follows.pending.has(shown.id)}
          onToggleFollow={() => {
            const wasFollowed = followedIds.has(shown.id);
            void follows.toggle({ id: shown.id, name: shown.name, boardType: shown.boardType }).then((error) => {
              showPlanToast(error ?? (wasFollowed ? `已取消关注 ${shown.name}` : `已关注 ${shown.name}`));
            });
          }}
        />
      );
    }

    body = (
      <>
        <MarketStrip market={data.market} />
        <BoardFocusStrip
          recommendations={recommendations.state}
          onRegenerate={recommendations.regenerate}
          followed={follows.boards}
          followStatus={follows.status}
          followError={follows.error}
          onRetryFollows={() => void follows.load(true)}
          activeId={activeId}
          onSelect={handleFocusSelect}
          onMessage={showPlanToast}
        />
        {data.historyNote ? (
          <p className="notice market-notice" role="note">
            <b>历史数据</b>
            <span>{data.historyNote}</span>
          </p>
        ) : null}
        <div className={`grid2${showDetail ? ' show-detail' : ''}`}>
          <section className="win side" aria-label="板块列表">
            <BoardFilters
              type={typeFilter}
              state={stateFilter}
              onTypeChange={setTypeFilter}
              onStateChange={setStateFilter}
            />
            <div className="list-scroll">
              {allBoards.length === 0 ? (
                <div className="empty sm">
                  <h3>今天没有板块数据</h3>
                  <p>收盘后的板块数据还没有生成，稍后再来看。</p>
                </div>
              ) : visibleBoards.length === 0 ? (
                <div className="empty sm">
                  <h3>没有符合条件的板块</h3>
                  <p>换一个类型或状态试试。</p>
                </div>
              ) : (
                <BoardList
                  key={`${typeFilter}-${stateFilter}`}
                  boards={visibleBoards}
                  activeId={activeId}
                  onSelect={handleSelect}
                  followedIds={followedIds}
                />
              )}
            </div>
          </section>
          <article id={BOARD_DETAIL_ID} className="win detail" aria-live="polite" aria-label="板块详情">
            {detailBody}
          </article>
        </div>
      </>
    );
  }

  return (
    <>
      <PlanPageHeader title={title} subtitle={subtitle} onOpenAssistant={handleAsk} />
      <StaggerItem>{body}</StaggerItem>
    </>
  );
};

export default BoardsPage;
