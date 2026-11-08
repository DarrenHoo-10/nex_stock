import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { getParsedApiError } from '../../api/error';
import { systemConfigApi } from '../../api/systemConfig';
import { AccountStrip } from '../../components/plan/mine/AccountStrip';
import { HoldingsDialog } from '../../components/plan/mine/HoldingsDialog';
import type { RecordBuyPrefill } from '../../components/plan/mine/PlanView';
import { RecordBuyDialog } from '../../components/plan/mine/RecordBuyDialog';
import { StockDetail } from '../../components/plan/mine/StockDetail';
import { StockList } from '../../components/plan/mine/StockList';
import { useMineOverview, useStockPlanDetail } from '../../components/plan/mine/useMineData';
import { PlanPageHeader } from '../../components/plan/PlanPageHeader';
import { PlanStockSearch } from '../../components/plan/PlanStockSearch';
import { watchlistCodeOf } from '../../utils/watchlistCode';
import { useStockIndex } from '../../hooks/useStockIndex';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { showPlanToast } from '../../stores/planToastStore';
import { usePlanUiStore } from '../../stores/planUiStore';
import type { AccountResponse, Horizon } from '../../types/dailyPlan';
import '../../styles/plan-mine.css';

/** Below this width the list and the detail are two screens (matches plan-glass.css). */
const COMPACT_QUERY = '(max-width: 860px)';

function isCompactViewport(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(COMPACT_QUERY).matches;
}

const ListSkeleton: React.FC = () => (
  <div className="list-sk" aria-busy="true" aria-label="股票列表载入中">
    {[0, 1, 2, 3, 4].map((index) => (
      <div key={index} className="sk-row">
        <span className="sk sk-line sk-mid" />
        <span className="sk sk-line sk-wide" />
      </div>
    ))}
  </div>
);

const DetailPlaceholderSkeleton: React.FC = () => (
  <div className="detail-sk" aria-busy="true" aria-label="计划载入中">
    <span className="sk sk-line sk-title" />
    <span className="sk sk-line sk-hero" />
    <span className="sk sk-line sk-wide" />
    <div className="sk-grid">
      <span className="sk sk-block" />
      <span className="sk sk-block" />
    </div>
  </div>
);

const StatePanel: React.FC<{ title: string; text: string; onRetry: () => void }> = ({ title, text, onRetry }) => (
  <section className="win empty" role="alert" aria-labelledby="plan-page-title">
    <h2 id="plan-page-title">{title}</h2>
    <p>{text}</p>
    <div className="actions">
      <button type="button" className="btn" onClick={onRetry}>
        重试
      </button>
    </div>
  </section>
);

/**
 * Shown while the next-day plan service is not available: the watchlist itself (codes from
 * `/stocks/watchlist`, names from the bundled stock index) so added stocks are visible and removable.
 */
const WatchlistFallback: React.FC<{
  codes: string[] | null;
  busy: boolean;
  onRemove: (code: string) => void;
  onRetry: () => void;
}> = ({ codes, busy, onRemove, onRetry }) => {
  const stockIndex = useStockIndex(true);
  const names = useMemo(() => {
    const map = new Map<string, string>();
    for (const item of stockIndex.index) map.set(watchlistCodeOf(item), item.nameZh);
    return map;
  }, [stockIndex.index]);
  return (
    <section className="win watch-fallback" aria-labelledby="plan-page-title">
      <div className="wf-head">
        <h2 id="plan-page-title">我的自选</h2>
        {codes ? <span className="wf-count">{codes.length} 只</span> : null}
      </div>
      <p className="notice wf-notice" role="note">
        <b>次日计划还没有就绪</b>
        <span>后端的次日计划模块还没有就绪，暂时只能维护自选列表；就绪后这里会出现每只股票的计划。</span>
        <button type="button" className="btn sm" onClick={onRetry}>
          重试
        </button>
      </p>
      {codes === null ? (
        <p className="note">正在读取自选…</p>
      ) : codes.length === 0 ? (
        <p className="note">还没有自选股。用右上角的搜索框加几只。</p>
      ) : (
        <ul className="wf-list" aria-label="自选列表">
          {codes.map((code) => (
            <li key={code}>
              <span className="wf-name">{names.get(code.toUpperCase()) ?? '—'}</span>
              <span className="num wf-code">{code}</span>
              <button type="button" className="btn quiet sm" disabled={busy} onClick={() => onRemove(code)}>
                移出
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
};

const MinePage: React.FC = () => {
  const { t } = useUiLanguage();
  const title = t('plan.page.mine.title');
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const overview = useMineOverview();
  const { account, list } = overview;

  const paramCode = params.get('code');
  const items = list.status === 'ready' ? list.data.items : null;
  const selectedCode = paramCode ?? items?.[0]?.code ?? null;
  const selectedSummary = items?.find((item) => item.code === selectedCode) ?? null;
  const detail = useStockPlanDetail(items ? selectedCode : null, overview.version);

  const [holdingsOpen, setHoldingsOpen] = useState(false);
  const [holdingsKey, setHoldingsKey] = useState(0);
  const [recordOpen, setRecordOpen] = useState(false);
  const [recordKey, setRecordKey] = useState(0);
  const [recordPrefill, setRecordPrefill] = useState<RecordBuyPrefill | null>(null);
  const [busy, setBusy] = useState(false);
  const [watchCodes, setWatchCodes] = useState<string[] | null>(null);
  const [watchKey, setWatchKey] = useState(0);
  const watchSet = useMemo(() => new Set((watchCodes ?? []).map((code) => code.toUpperCase())), [watchCodes]);

  useEffect(() => {
    document.title = `${title} - DSA`;
  }, [title]);

  // The watchlist itself (independent of the plan service): marks searched stocks already added and backs the
  // fallback list while the plan service is not ready.
  useEffect(() => {
    let cancelled = false;
    systemConfigApi
      .getWatchlist()
      .then((codes) => {
        if (!cancelled) setWatchCodes(codes);
      })
      .catch(() => {
        if (!cancelled) setWatchCodes((current) => current ?? []);
      });
    return () => {
      cancelled = true;
    };
  }, [watchKey]);
  const reloadWatch = useCallback(() => setWatchKey((key) => key + 1), []);

  // Compact layout: opening a stock moves focus to the back button, going back returns it to the row.
  const previousParam = useRef(paramCode);
  useEffect(() => {
    const previous = previousParam.current;
    previousParam.current = paramCode;
    if (previous && !paramCode) {
      document.querySelector<HTMLElement>(`.plan-shell .row[data-code="${previous}"]`)?.focus({ preventScroll: true });
    } else if (!previous && paramCode && isCompactViewport()) {
      document.querySelector<HTMLElement>('.plan-shell .detail .back')?.focus({ preventScroll: true });
    }
  }, [paramCode]);

  const select = useCallback(
    (code: string) => {
      const compact = isCompactViewport();
      const opening = compact && !paramCode;
      navigate(
        { pathname: location.pathname, search: `?code=${encodeURIComponent(code)}` },
        { replace: !opening, state: opening ? { fromList: true } : location.state },
      );
      if (compact) {
        window.scrollTo({ top: 0 });
      }
    },
    [location.pathname, location.state, navigate, paramCode],
  );

  const goBack = useCallback(() => {
    if ((location.state as { fromList?: boolean } | null)?.fromList) {
      navigate(-1);
    } else {
      navigate({ pathname: location.pathname, search: '' }, { replace: true });
    }
  }, [location.pathname, location.state, navigate]);

  const contextTradeDate = list.status === 'ready' ? list.data.tradeDate : null;
  const openAssistant = useCallback(
    (horizon?: Horizon) => {
      if (!selectedSummary) {
        usePlanUiStore.getState().openAssistant({ kind: 'none' });
        return;
      }
      usePlanUiStore.getState().openAssistant({
        kind: 'stock',
        stockCode: selectedSummary.code,
        stockName: selectedSummary.name,
        held: selectedSummary.held,
        horizon: horizon ?? null,
        tradeDate: contextTradeDate,
      });
    },
    [selectedSummary, contextTradeDate],
  );

  const { reload } = overview;
  const addWatch = useCallback(
    async (code: string): Promise<string | null> => {
      try {
        await systemConfigApi.addToWatchlist(code);
        showPlanToast('已加入自选');
        reload(true);
        reloadWatch();
        return null;
      } catch (error: unknown) {
        return getParsedApiError(error).message || '加入自选失败，请稍后重试。';
      }
    },
    [reload, reloadWatch],
  );

  const addFromSearch = useCallback(
    async (code: string, name: string): Promise<string | null> => {
      try {
        await systemConfigApi.addToWatchlist(code);
        showPlanToast(`已把 ${name} 加入自选`);
        reload(true);
        reloadWatch();
        return null;
      } catch (error: unknown) {
        return getParsedApiError(error).message || '加入自选失败，请稍后重试。';
      }
    },
    [reload, reloadWatch],
  );

  const addWatchFromDetail = useCallback(
    async (code: string) => {
      setBusy(true);
      const failure = await addWatch(code);
      setBusy(false);
      if (failure) {
        showPlanToast(failure);
      }
    },
    [addWatch],
  );

  const removeWatch = useCallback(
    async (code: string) => {
      setBusy(true);
      try {
        await systemConfigApi.removeFromWatchlist(code);
        showPlanToast('已移出自选');
        navigate({ pathname: location.pathname, search: '' }, { replace: true });
        reload(true);
        reloadWatch();
      } catch (error: unknown) {
        showPlanToast(getParsedApiError(error).message || '移出自选失败，请稍后重试。');
      } finally {
        setBusy(false);
      }
    },
    [location.pathname, navigate, reload, reloadWatch],
  );

  const openHoldings = () => {
    setHoldingsKey((key) => key + 1);
    setHoldingsOpen(true);
  };

  const openRecordBuy = (prefill: RecordBuyPrefill) => {
    setRecordPrefill(prefill);
    setRecordKey((key) => key + 1);
    setRecordOpen(true);
  };

  const handleSaved = (saved: AccountResponse) => {
    overview.setAccount(saved);
    overview.reload(true);
  };

  const subtitle =
    list.status === 'ready'
      ? `${list.data.nextTradeDate} 的计划，基于 ${list.data.tradeDate} 收盘数据，包含全部自选和持仓`
      : t('plan.page.mine.subtitle');
  const header = (
    <PlanPageHeader
      title={title}
      subtitle={subtitle}
      onOpenAssistant={() => openAssistant()}
      actions={<PlanStockSearch watchlist={watchSet} onAdd={addFromSearch} />}
    />
  );

  if (list.status === 'unavailable' || account.status === 'unavailable') {
    return (
      <>
        {header}
        <WatchlistFallback
          codes={watchCodes}
          busy={busy}
          onRemove={(code) => void removeWatch(code)}
          onRetry={() => overview.reload()}
        />
      </>
    );
  }
  if (list.status === 'error' || list.status === 'notfound') {
    return (
      <>
        {header}
        <StatePanel
          title="计划读取失败"
          text={list.status === 'error' ? list.message : '没有找到计划列表。'}
          onRetry={() => overview.reload()}
        />
      </>
    );
  }

  const accountData = account.status === 'ready' ? account.data : null;
  const empty = items !== null && items.length === 0;

  return (
    <>
      {header}
      <div className={`grid2${paramCode ? ' show-detail' : ''}`}>
        <section className="win side" aria-label="账户和股票列表">
          <AccountStrip account={account} onEdit={openHoldings} onRetry={() => overview.reloadAccount()} />
          <div className="list-scroll">
            {items === null ? (
              <ListSkeleton />
            ) : (
              <StockList
                items={items}
                selectedCode={selectedCode}
                maxHoldings={accountData?.rules.maxHoldings ?? null}
                onSelect={select}
                onAddWatch={addWatch}
              />
            )}
          </div>
        </section>

        <article className="win detail" aria-labelledby="plan-page-title">
          {items === null ? (
            <DetailPlaceholderSkeleton />
          ) : empty && !paramCode ? (
            <section className="empty" aria-labelledby="plan-page-title">
              <h2 id="plan-page-title">还没有自选和持仓</h2>
              <p>加几只自选股，或录入你现在的持仓，下一个收盘后这里会出现每只股票的次日计划。</p>
              <div className="actions">
                <Link className="btn primary" to="/picks">
                  去选股
                </Link>
                <button type="button" className="btn" aria-haspopup="dialog" onClick={openHoldings}>
                  录入持仓
                </button>
              </div>
              <p className="note">也可以在左侧「+ 加自选」里直接输入 6 位代码。</p>
            </section>
          ) : selectedCode ? (
            <StockDetail
              code={selectedCode}
              summary={selectedSummary}
              detail={detail.state}
              account={account}
              onRetry={() => detail.reload()}
              onBack={goBack}
              onSelectStock={select}
              onAsk={openAssistant}
              onRecordBuy={openRecordBuy}
              onAddWatch={(code) => void addWatchFromDetail(code)}
              onRemoveWatch={(code) => void removeWatch(code)}
              busy={busy}
            />
          ) : null}
        </article>
      </div>

      <HoldingsDialog
        key={`holdings-${holdingsKey}`}
        open={holdingsOpen}
        onClose={() => setHoldingsOpen(false)}
        account={accountData}
        onSaved={handleSaved}
      />
      {recordPrefill ? (
        <RecordBuyDialog
          key={`record-${recordKey}`}
          open={recordOpen}
          onClose={() => setRecordOpen(false)}
          code={recordPrefill.code}
          name={recordPrefill.name}
          suggestedShares={recordPrefill.shares}
          suggestedPrice={recordPrefill.price}
          horizon={recordPrefill.horizon}
          onSaved={handleSaved}
        />
      ) : null}
    </>
  );
};

export default MinePage;
