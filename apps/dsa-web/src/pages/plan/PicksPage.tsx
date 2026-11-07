import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import type { ScreeningRunSummary, ScreeningScope } from '../../api/screening';
import { toApiErrorMessage } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { SegmentedControl, StaggerItem } from '../../components/plan/motion';
import { PlanErrorPanel, PlanSkeleton, PlanUnavailablePanel, useBoardDetail } from '../../components/plan/boards';
import {
  PickCard,
  PickProgress,
  StrategyChips,
  matchesHorizon,
  strategyLabel,
  toPickView,
  usePickRun,
  usePickSetup,
  useWatchlist,
} from '../../components/plan/picks';
import type { HorizonFilter, PickView } from '../../components/plan/picks';
import { showPlanToast } from '../../stores/planToastStore';
import { usePlanUiStore } from '../../stores/planUiStore';
import '../../styles/plan-boards.css';

/** Results per run requested from the screening API. */
const PICK_MAX_RESULTS = 10;

const HORIZON_OPTIONS = [
  { value: 'all' as const, label: '全部' },
  { value: 'short' as const, label: '短线' },
  { value: 'swing' as const, label: '波段' },
];

function formatRunTime(value: string | undefined): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function scopeTopicNames(scope: ScreeningScope | undefined): string[] {
  return (scope?.topics ?? []).map((topic) => topic.name);
}

/** 机会选股: strategy chips, optional board scope, run -> progress -> candidate cards. */
const PicksPage: React.FC = () => {
  const { t } = useUiLanguage();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const boardId = searchParams.get('board');
  const title = t('plan.page.picks.title');
  const openAssistant = usePlanUiStore((state) => state.openAssistant);

  const setup = usePickSetup();
  const board = useBoardDetail(boardId);
  const watchlist = useWatchlist();
  const { state: run, start, restoreLatest, skipRestore } = usePickRun();

  const [chosenStrategy, setChosenStrategy] = useState<string | null>(null);
  const [horizon, setHorizon] = useState<HorizonFilter>('all');
  const [adding, setAdding] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  // The board scope: its name and type become a screening topic.
  const boardReady = boardId !== null && board.status === 'ready' ? board.data : null;
  const boardName = boardReady?.name ?? null;
  const scopeSettled = boardId === null || board.status !== 'loading';

  // Show the newest earlier run of the same scope on arrival (and when the scope changes).
  useEffect(() => {
    if (!scopeSettled) return;
    if (boardId !== null && boardName === null) {
      skipRestore();
      return;
    }
    const matches = (item: ScreeningRunSummary) => {
      const names = scopeTopicNames(item.scope);
      return boardName === null ? names.length === 0 : names.includes(boardName);
    };
    void restoreLatest(matches);
  }, [scopeSettled, boardId, boardName, restoreLatest, skipRestore]);

  const strategies = useMemo(() => (setup.status === 'ready' ? setup.strategies : []), [setup]);
  const restoredStrategy = run.result?.strategy;
  const strategy =
    chosenStrategy && strategies.some((item) => item.id === chosenStrategy)
      ? chosenStrategy
      : restoredStrategy && strategies.some((item) => item.id === restoredStrategy)
        ? restoredStrategy
        : (strategies[0]?.id ?? null);

  const running = run.phase === 'starting' || run.phase === 'running';
  const boardBlocked = boardId !== null && (boardReady === null || !boardReady.canPick);
  const canStart = setup.status === 'ready' && strategy !== null && !running && !boardBlocked;

  const handleStart = useCallback(() => {
    if (!strategy) return;
    const scope: ScreeningScope | undefined = boardReady
      ? { boards: [], topics: [{ name: boardReady.name, source: boardReady.boardType }], topicMatch: 'any' }
      : undefined;
    void start({ market: 'cn', strategy, maxResults: PICK_MAX_RESULTS, ...(scope ? { scope } : {}) });
  }, [strategy, boardReady, start]);

  const clearBoardScope = useCallback(() => {
    const next = new URLSearchParams(searchParams);
    next.delete('board');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const handleAdd = useCallback(
    async (pick: PickView) => {
      setAdding((previous) => new Set(previous).add(pick.code));
      try {
        await watchlist.add(pick.code);
        showPlanToast(`已把 ${pick.name} 加入自选`);
      } catch (err) {
        showPlanToast(toApiErrorMessage(err, `${pick.name} 加入自选失败，请稍后重试`));
      } finally {
        setAdding((previous) => {
          const next = new Set(previous);
          next.delete(pick.code);
          return next;
        });
      }
    },
    [watchlist],
  );

  // A pick opens the assistant on that stock; the header opens it on the board scope, if any.
  const handleAsk = useCallback(
    (pick: PickView) =>
      openAssistant({
        kind: 'stock',
        stockCode: pick.code,
        stockName: pick.name,
        horizon: pick.horizons.length === 1 ? pick.horizons[0] : null,
      }),
    [openAssistant],
  );
  const handleHeaderAsk = useCallback(
    () =>
      openAssistant(
        boardReady
          ? { kind: 'board', boardId: boardReady.id, boardName: boardReady.name }
          : { kind: 'none' },
      ),
    [openAssistant, boardReady],
  );

  const candidates = useMemo(() => (run.result?.candidates ?? []).map(toPickView), [run.result]);
  const visible = useMemo(() => candidates.filter((pick) => matchesHorizon(pick, horizon)), [candidates, horizon]);

  const resultStrategy = strategies.find((item) => item.id === run.result?.strategy);
  const resultStrategyName = resultStrategy ? strategyLabel(resultStrategy) : (run.result?.strategy ?? '');
  const resultScopeNames = scopeTopicNames(run.result?.scope);
  const runTime = formatRunTime(run.result?.createdAt);
  const warnings = (run.result?.warnings ?? []).filter((line) => line.trim() !== '');

  let results: React.ReactNode;
  if (run.phase === 'restoring') {
    results = (
      <div className="cand-skel">
        <PlanSkeleton rows={4} label="正在查找上次的选股结果" />
      </div>
    );
  } else if (running) {
    results = <PickProgress progress={run.progress} message={run.message} />;
  } else if (run.phase === 'failed') {
    results = (
      <PlanErrorPanel
        message={run.error ?? '选股任务失败，请稍后重试。'}
        onRetry={handleStart}
        title="选股没有完成"
      />
    );
  } else if (run.phase === 'done' && run.result) {
    results = (
      <>
        <p className="cond-line">
          策略 <b>{resultStrategyName || '未记录'}</b>，范围 <b>{resultScopeNames.join('、') || '全部 A 股'}</b>
          {runTime ? `，运行于 ${runTime}` : ''}。共 {candidates.length} 只
          {horizon !== 'all' ? `，当前周期显示 ${visible.length} 只` : ''}。
          {run.fromHistory ? <span className="tag line">上次结果</span> : null}
        </p>
        {warnings.length ? (
          <p className="notice picks-notice" role="note">
            <b>提示</b>
            <span>{warnings.join('；')}</span>
          </p>
        ) : null}
        {visible.length ? (
          <ul className="cands" key={`${run.result.runId ?? run.result.createdAt ?? 'run'}-${horizon}`}>
            {visible.map((pick, index) => (
              <PickCard
                key={pick.code}
                pick={pick}
                index={index}
                inWatchlist={watchlist.codes.has(pick.code)}
                adding={adding.has(pick.code)}
                onAdd={handleAdd}
                onAsk={handleAsk}
              />
            ))}
          </ul>
        ) : (
          <div className="empty">
            <h3>{candidates.length ? '这个周期下没有候选股' : '今天没有满足条件的股票'}</h3>
            <p>
              {candidates.length
                ? '换成「全部」或另一个周期看看。'
                : '条件没有放宽，空结果也是一种结论。可以换个策略或板块，也可以明天 16:00 再看。'}
            </p>
          </div>
        )}
      </>
    );
  } else {
    results = (
      <div className="empty">
        <h3>还没有选股结果</h3>
        <p>
          {boardName
            ? `选好策略后点「开始选股」，会在「${boardName}」板块的成分股里筛选。`
            : '选好策略后点「开始选股」，会在全部 A 股里筛选。列出的是值得关注的股票，不是涨跌预测。'}
        </p>
      </div>
    );
  }

  let scopeControl: React.ReactNode;
  if (boardId === null) {
    scopeControl = (
      <Link className="btn quiet sm" to="/boards">
        从异动板块里选
      </Link>
    );
  } else if (board.status === 'loading') {
    scopeControl = <span className="scope-pill" aria-busy="true">正在读取板块…</span>;
  } else if (boardReady) {
    scopeControl = (
      <span className="scope-pill" data-testid="pick-scope">
        <span>范围：{boardReady.name}板块</span>
        <button type="button" className="btn quiet sm" onClick={clearBoardScope}>
          清除范围
        </button>
      </span>
    );
  } else {
    scopeControl = (
      <span className="scope-pill" data-testid="pick-scope">
        <span>板块范围不可用</span>
        <button type="button" className="btn quiet sm" onClick={clearBoardScope}>
          清除范围
        </button>
      </span>
    );
  }

  let boardNotice: React.ReactNode = null;
  if (boardId !== null && board.status === 'unavailable') {
    boardNotice = <PlanUnavailablePanel compact headingLevel={3} />;
  } else if (boardId !== null && board.status === 'error') {
    boardNotice = (
      <PlanErrorPanel compact message={board.message} onRetry={board.reload} title="板块信息加载失败，无法限定选股范围" />
    );
  } else if (boardReady && !boardReady.canPick) {
    boardNotice = (
      <p className="notice picks-notice" role="note">
        <b>不按板块选股</b>
        <span>{boardReady.name}的涨幅主要来自个别股票，或历史数据不足，所以不按板块机会选股。可以清除范围后按策略在全市场选。</span>
      </p>
    );
  }

  let setupBlock: React.ReactNode;
  if (setup.status === 'loading') {
    setupBlock = (
      <div className="strategy-block">
        <PlanSkeleton rows={2} label="正在加载选股策略" />
      </div>
    );
  } else if (setup.status === 'error') {
    setupBlock = <PlanErrorPanel compact message={setup.message} onRetry={setup.reload} title="选股策略加载失败" />;
  } else if (setup.status === 'disabled') {
    setupBlock = (
      <div className="empty sm">
        <h3>选股功能未就绪</h3>
        <p>{setup.message}</p>
        <button type="button" className="btn sm" onClick={() => navigate('/screening')}>
          去选股工具开启
        </button>
      </div>
    );
  } else if (strategies.length === 0) {
    setupBlock = (
      <div className="empty sm">
        <h3>没有可用的选股策略</h3>
        <p>策略列表为空，请检查选股的策略配置。</p>
      </div>
    );
  } else {
    setupBlock = <StrategyChips strategies={strategies} value={strategy} onChange={setChosenStrategy} disabled={running} />;
  }

  return (
    <>
      <PlanPageHeader
        title={title}
        subtitle="列出的是值得关注的股票，不是涨跌预测；每条都带买入条件和失效条件"
        onOpenAssistant={handleHeaderAsk}
      />
      <StaggerItem>
        <section className="win picks-win" aria-label="选股结果">
          <div className="toolbar">
            {scopeControl}
            <SegmentedControl options={HORIZON_OPTIONS} value={horizon} onChange={setHorizon} ariaLabel="周期" />
            <span className="spacer" aria-hidden="true" />
            <button type="button" className="btn primary" disabled={!canStart} onClick={handleStart}>
              {running ? '选股中…' : '开始选股'}
            </button>
          </div>
          {setupBlock}
          {boardNotice}
          <div aria-live="polite" className="pick-results">
            {results}
          </div>
        </section>
      </StaggerItem>
    </>
  );
};

export default PicksPage;
