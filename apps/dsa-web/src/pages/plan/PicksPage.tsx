import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { ScreeningScope, ScreeningStrategy } from '../../api/screening';
import { toApiErrorMessage } from '../../api/error';
import { systemConfigApi } from '../../api/systemConfig';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { StockChartDialog } from '../../components/plan/quote/StockChartDialog';
import { targetFromCode } from '../../components/plan/quote/quoteTarget';
import type { QuoteTarget } from '../../components/plan/quote/quoteTarget';
import { SegmentedControl, StaggerItem } from '../../components/plan/motion';
import { PlanErrorPanel, PlanSkeleton, PlanUnavailablePanel, useBoardDetail } from '../../components/plan/boards';
import {
  BoardPicker,
  PickCard,
  PickProgress,
  StrategyChips,
  formatPickTime,
  isActiveTask,
  matchesHorizon,
  scopeNames,
  scopeText,
  startPickTask,
  strategyName,
  taskStatus,
  toPickView,
  usePickSetup,
  usePickTask,
  usePickTaskList,
  useWatchlist,
} from '../../components/plan/picks';
import type { HorizonFilter, PickTask, PickView, PickedBoard } from '../../components/plan/picks';
import { showPlanToast } from '../../stores/planToastStore';
import { usePlanUiStore } from '../../stores/planUiStore';
import '../../styles/plan-boards.css';

/** Results per task requested from the screening API. */
const PICK_MAX_RESULTS = 10;

const HORIZON_OPTIONS = [
  { value: 'all' as const, label: '全部' },
  { value: 'short' as const, label: '短线' },
  { value: 'swing' as const, label: '波段' },
];

function toScope(boards: PickedBoard[]): ScreeningScope | undefined {
  return boards.length
    ? { boards: [], topics: boards.map((board) => ({ name: board.name, source: board.boardType })), topicMatch: 'any' }
    : undefined;
}

/** One row of the task list. */
const TaskRow: React.FC<{ task: PickTask; strategies: ScreeningStrategy[]; onOpen: (taskId: string) => void }> = ({
  task,
  strategies,
  onOpen,
}) => {
  const status = taskStatus(task);
  const progress = Number(task.progress);
  return (
    <li>
      <button type="button" className="task-row" onClick={() => onOpen(task.taskId)}>
        <span className="task-time num">{formatPickTime(task.createdAt) ?? '—'}</span>
        <span className="task-main">
          <b>{strategyName(task.strategy, strategies)}</b>
          <span className="task-scope">{scopeText(task.scope)}</span>
        </span>
        <span className={`task-status tone-${status.tone}`}>
          {status.label}
          {isActiveTask(task) ? (
            <span className="task-bar" aria-hidden="true">
              <i style={{ width: `${Number.isFinite(progress) ? Math.max(4, Math.min(100, progress)) : 4}%` }} />
            </span>
          ) : null}
        </span>
      </button>
    </li>
  );
};

/** 机会选股: start a task (strategy + optional boards), list persisted tasks, open one for its picks. */
const PicksPage: React.FC = () => {
  const { t } = useUiLanguage();
  const [searchParams, setSearchParams] = useSearchParams();
  const boardId = searchParams.get('board');
  const taskId = searchParams.get('task');
  const title = t('plan.page.picks.title');
  const openAssistant = usePlanUiStore((state) => state.openAssistant);

  const setup = usePickSetup();
  const board = useBoardDetail(boardId);
  const watchlist = useWatchlist();
  const list = usePickTaskList();
  const detail = usePickTask(taskId);

  const [enabling, setEnabling] = useState(false);
  const [chosenStrategy, setChosenStrategy] = useState<string | null>(null);
  const [boards, setBoards] = useState<PickedBoard[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [horizon, setHorizon] = useState<HorizonFilter>('all');
  const [adding, setAdding] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  // Arriving from a board page (?board=) preselects that board once, when it can be picked.
  const boardReady = boardId !== null && board.status === 'ready' ? board.data : null;
  const preselected = useRef<string | null>(null);
  useEffect(() => {
    if (!boardReady || !boardReady.canPick || preselected.current === boardReady.id) return;
    preselected.current = boardReady.id;
    setBoards((current) =>
      current.some((item) => item.id === boardReady.id)
        ? current
        : [...current, { id: boardReady.id, name: boardReady.name, boardType: boardReady.boardType }],
    );
  }, [boardReady]);

  /** Switches SCREENING_ENABLED on in the config (same key the backend checks) and reloads the strategy list. */
  const enableScreening = async () => {
    setEnabling(true);
    try {
      const current = await systemConfigApi.getConfig(false);
      await systemConfigApi.update({
        configVersion: current.configVersion,
        items: [{ key: 'SCREENING_ENABLED', value: 'true' }],
        reloadNow: true,
      });
      showPlanToast('选股已开启');
      setup.reload();
    } catch (error) {
      showPlanToast(toApiErrorMessage(error, '没能开启选股，请稍后重试'));
    } finally {
      setEnabling(false);
    }
  };

  const strategies = useMemo(() => (setup.status === 'ready' ? setup.strategies : []), [setup]);
  const strategy =
    chosenStrategy && strategies.some((item) => item.id === chosenStrategy) ? chosenStrategy : (strategies[0]?.id ?? null);
  const canStart = setup.status === 'ready' && strategy !== null && !submitting;

  const openTask = useCallback(
    (id: string | null) => {
      const next = new URLSearchParams(searchParams);
      if (id) next.set('task', id);
      else next.delete('task');
      setSearchParams(next);
      setHorizon('all');
    },
    [searchParams, setSearchParams],
  );

  const submit = useCallback(
    async (strategyId: string, scope: ScreeningScope | undefined) => {
      setSubmitting(true);
      try {
        await startPickTask({ market: 'cn', strategy: strategyId, maxResults: PICK_MAX_RESULTS, ...(scope ? { scope } : {}) });
        showPlanToast('选股任务已开始，可以离开本页，完成后在任务列表里查看');
        list.reload();
        return true;
      } catch (err) {
        showPlanToast(toApiErrorMessage(err, '选股任务提交失败，请稍后重试'));
        return false;
      } finally {
        setSubmitting(false);
      }
    },
    [list],
  );

  const handleStart = useCallback(() => {
    if (strategy) void submit(strategy, toScope(boards));
  }, [strategy, boards, submit]);

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

  const [chartTarget, setChartTarget] = useState<QuoteTarget | null>(null);
  const handleOpen = useCallback((pick: PickView) => setChartTarget(targetFromCode(pick.code, pick.name)), []);

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
        boards.length === 1
          ? { kind: 'board', boardId: boards[0].id, boardName: boards[0].name }
          : { kind: 'none' },
      ),
    [openAssistant, boards],
  );

  const task = taskId && detail.state.status === 'ready' ? detail.state.task : null;
  const candidates = useMemo(() => (task?.result?.candidates ?? []).map(toPickView), [task]);
  const visible = useMemo(() => candidates.filter((pick) => matchesHorizon(pick, horizon)), [candidates, horizon]);

  // ---------- task detail (?task=) ----------
  let detailView: React.ReactNode = null;
  if (taskId) {
    let body: React.ReactNode;
    if (detail.state.status === 'loading') {
      body = (
        <div className="cand-skel">
          <PlanSkeleton rows={4} label="正在读取选股任务" />
        </div>
      );
    } else if (detail.state.status === 'missing') {
      body = (
        <div className="empty">
          <h3>找不到这个选股任务</h3>
          <p>任务可能已被清理。回到任务列表重新发起一次。</p>
        </div>
      );
    } else if (detail.state.status === 'error') {
      body = <PlanErrorPanel message={detail.state.message} onRetry={detail.reload} title="选股任务读取失败" />;
    } else if (task && isActiveTask(task)) {
      body = (
        <PickProgress
          progress={typeof task.progress === 'number' ? task.progress : null}
          message={task.message || (task.status === 'pending' ? '排队中，前面的任务完成后开始' : '正在选股…')}
        />
      );
    } else if (task && task.status !== 'completed') {
      body = (
        <PlanErrorPanel
          message={task.error || task.message || '选股任务没有完成。'}
          onRetry={() => {
            if (task.strategy) {
              void submit(task.strategy, task.scope && scopeNames(task.scope).length ? task.scope : undefined).then((ok) => {
                if (ok) openTask(null);
              });
            }
          }}
          title={task.status === 'interrupted' ? '选股任务被中断' : '选股没有完成'}
        />
      );
    } else if (task) {
      const warnings = (task.result?.warnings ?? []).filter((line) => line.trim() !== '');
      body = (
        <>
          {warnings.length ? (
            <p className="notice picks-notice" role="note">
              <b>提示</b>
              <span>{warnings.join('；')}</span>
            </p>
          ) : null}
          {!task.result ? (
            <div className="empty">
              <h3>结果没有保存下来</h3>
              <p>任务完成了，但服务端没有找到对应的结果记录。可以重新发起一次。</p>
            </div>
          ) : visible.length ? (
            <ul className="cands" key={`${task.taskId}-${horizon}`}>
              {visible.map((pick, index) => (
                <PickCard
                  key={pick.code}
                  pick={pick}
                  index={index}
                  inWatchlist={watchlist.codes.has(pick.code)}
                  adding={adding.has(pick.code)}
                  onAdd={handleAdd}
                  onAsk={handleAsk}
                  onOpen={handleOpen}
                />
              ))}
            </ul>
          ) : (
            <div className="empty">
              <h3>{candidates.length ? '这个周期下没有候选股' : '没有满足条件的股票'}</h3>
              <p>
                {candidates.length
                  ? '换成「全部」或另一个周期看看。'
                  : '条件没有放宽，空结果也是一种结论。可以换个策略或板块再发起一次。'}
              </p>
            </div>
          )}
        </>
      );
    }

    const status = task ? taskStatus(task) : null;
    detailView = (
      <StaggerItem>
        <section className="win picks-win" aria-label="选股任务详情">
          <div className="toolbar">
            <button type="button" className="btn quiet sm" onClick={() => openTask(null)}>
              ← 全部任务
            </button>
            {task?.status === 'completed' && task.result ? (
              <SegmentedControl options={HORIZON_OPTIONS} value={horizon} onChange={setHorizon} ariaLabel="周期" />
            ) : null}
          </div>
          {task ? (
            <p className="cond-line task-head" data-testid="task-head">
              策略 <b>{strategyName(task.strategy, strategies)}</b>，范围 <b>{scopeText(task.scope)}</b>
              {formatPickTime(task.createdAt) ? `，发起于 ${formatPickTime(task.createdAt)}` : ''}
              {formatPickTime(task.finishedAt) && !isActiveTask(task) ? `，结束于 ${formatPickTime(task.finishedAt)}` : ''}
              {status ? <span className={`tag task-tag tone-${status.tone}`}>{status.label}</span> : null}
              {task.status === 'completed' && horizon !== 'all' ? `，当前周期显示 ${visible.length} 只` : ''}
            </p>
          ) : null}
          <div aria-live="polite" className="pick-results">
            {body}
          </div>
        </section>
      </StaggerItem>
    );
  }

  // ---------- new task + task list ----------
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
        {setup.canEnable ? (
          <button type="button" className="btn sm" disabled={enabling} onClick={() => void enableScreening()}>
            {enabling ? '正在开启…' : '开启选股'}
          </button>
        ) : (
          <button type="button" className="btn sm" onClick={setup.reload}>
            重试
          </button>
        )}
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
    setupBlock = <StrategyChips strategies={strategies} value={strategy} onChange={setChosenStrategy} disabled={submitting} />;
  }

  let boardNotice: React.ReactNode = null;
  if (boardId !== null && board.status === 'unavailable') {
    boardNotice = <PlanUnavailablePanel compact headingLevel={3} />;
  } else if (boardId !== null && board.status === 'error') {
    boardNotice = <PlanErrorPanel compact message={board.message} onRetry={board.reload} title="板块信息加载失败" />;
  } else if (boardReady && !boardReady.canPick) {
    boardNotice = (
      <p className="notice picks-notice" role="note">
        <b>没有自动带入板块</b>
        <span>{boardReady.name}的涨幅主要来自个别股票，或历史数据不足，所以没有自动带入。仍然可以在上面手动选择。</span>
      </p>
    );
  }

  let listBody: React.ReactNode;
  if (list.state.status === 'loading') {
    listBody = (
      <div className="cand-skel">
        <PlanSkeleton rows={3} label="正在读取选股任务" />
      </div>
    );
  } else if (list.state.status === 'error') {
    listBody = <PlanErrorPanel compact message={list.state.message} onRetry={list.reload} title="选股任务读取失败" />;
  } else if (list.state.tasks.length === 0) {
    listBody = (
      <div className="empty">
        <h3>还没有选股任务</h3>
        <p>在上面选好策略和板块后点「开始选股」。任务在后台运行，可以离开本页，完成后点进任务查看结果。</p>
      </div>
    );
  } else {
    listBody = (
      <>
        <ul className="task-list" aria-label="选股任务列表">
          {list.state.tasks.map((item) => (
            <TaskRow key={item.taskId} task={item} strategies={strategies} onOpen={openTask} />
          ))}
        </ul>
        {list.state.total > list.state.tasks.length ? (
          <div className="actions task-more">
            <button type="button" className="btn quiet sm" onClick={list.loadMore}>
              加载更早的任务（还有 {list.state.total - list.state.tasks.length} 个）
            </button>
          </div>
        ) : null}
      </>
    );
  }

  return (
    <>
      <PlanPageHeader
        title={title}
        subtitle="列出的是值得关注的股票，不是涨跌预测；每条都带买入条件和失效条件"
        onOpenAssistant={handleHeaderAsk}
      />
      {taskId ? (
        detailView
      ) : (
        <>
          <StaggerItem>
            <section className="win picks-win" aria-labelledby="picks-new-title">
              <div className="toolbar">
                <h2 className="picks-title" id="picks-new-title">
                  新建选股
                </h2>
                <span className="spacer" aria-hidden="true" />
                <button type="button" className="btn primary" disabled={!canStart} onClick={handleStart}>
                  {submitting ? '正在提交…' : '开始选股'}
                </button>
              </div>
              {setupBlock}
              <BoardPicker value={boards} onChange={setBoards} disabled={submitting} />
              {boardNotice}
            </section>
          </StaggerItem>
          <StaggerItem>
            <section className="win picks-win" aria-labelledby="picks-list-title">
              <div className="toolbar">
                <h2 className="picks-title" id="picks-list-title">
                  选股任务
                </h2>
              </div>
              <div aria-live="polite">{listBody}</div>
            </section>
          </StaggerItem>
        </>
      )}
      <StockChartDialog
        target={chartTarget}
        onClose={() => setChartTarget(null)}
        onAddWatch={(target) => {
          const pick = candidates.find((item) => item.code === target.watchCode);
          if (pick && !watchlist.codes.has(pick.code)) {
            void handleAdd(pick);
          } else if (pick) {
            showPlanToast(`${pick.name} 已在自选`);
          }
        }}
      />
    </>
  );
};

export default PicksPage;
