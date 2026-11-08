import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { dataCapabilityApi } from '../../../api/dataCapability';
import { useLatestPlanRun } from '../../../hooks/useLatestPlanRun';
import type { DataCapabilityOverview, DatasetQualityStatus, ProviderCapabilityStatus } from '../../../types/dataCapability';
import { formatDateClock } from './format';
import { PanelHead } from './shared';

type OverviewState =
  | { status: 'loading' }
  | { status: 'ready'; overview: DataCapabilityOverview }
  | { status: 'error' };

const DATASET_STATUS: Record<DatasetQualityStatus, { label: string; tone: 'ok' | 'warn' | 'line' }> = {
  ok: { label: '正常', tone: 'ok' },
  degraded: { label: '降级', tone: 'warn' },
  partial: { label: '部分不足', tone: 'warn' },
  unconfigured: { label: '未配置', tone: 'line' },
  unavailable: { label: '不可用', tone: 'warn' },
  unknown: { label: '未知', tone: 'line' },
  stale: { label: '数据偏旧', tone: 'warn' },
};

const PROVIDER_STATUS: Record<ProviderCapabilityStatus, { label: string; tone: 'ok' | 'warn' | 'line' }> = {
  ok: { label: '正常', tone: 'ok' },
  partial: { label: '部分可用', tone: 'warn' },
  unconfigured: { label: '未配置', tone: 'line' },
  unavailable: { label: '不可用', tone: 'warn' },
  unknown: { label: '未知', tone: 'line' },
};

function statusOf<T extends string>(
  table: Record<T, { label: string; tone: 'ok' | 'warn' | 'line' }>,
  status: T,
): { label: string; tone: 'ok' | 'warn' | 'line' } {
  return table[status] ?? { label: String(status || '未知'), tone: 'line' };
}

export const DataStatusPanel: React.FC = () => {
  const [state, setState] = useState<OverviewState>({ status: 'loading' });
  const latest = useLatestPlanRun();

  const fetchOverview = useCallback(async () => {
    try {
      setState({ status: 'ready', overview: await dataCapabilityApi.getOverview() });
    } catch {
      setState({ status: 'error' });
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    dataCapabilityApi
      .getOverview()
      .then((overview) => {
        if (!cancelled) {
          setState({ status: 'ready', overview });
        }
      })
      .catch(() => {
        if (!cancelled) {
          setState({ status: 'error' });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const run = latest.status === 'ready' ? latest.run : null;

  return (
    <>
      <PanelHead title="数据状态" sub="各数据集和数据源现在能不能用，以及最近一次运行的结果" />

      <section className="pref-section" aria-label="最近一次运行">
        <h3>最近一次运行</h3>
        {latest.status === 'loading' ? (
          <p className="note" role="status">
            正在读取运行记录…
          </p>
        ) : null}
        {latest.status === 'unavailable' ? <p className="note">次日计划服务未就绪，暂时没有运行记录。</p> : null}
        {latest.status === 'error' ? <p className="note">运行记录读取失败。</p> : null}
        {latest.status === 'ready' && run === null ? <p className="note">还没有运行记录。</p> : null}
        {run ? (
          <>
            <dl className="data-cards" data-testid="run-counts">
              <div className="data-card">
                <dt>交易日</dt>
                <dd className="num">{run.tradeDate}</dd>
              </div>
              <div className="data-card">
                <dt>分析股票</dt>
                <dd className="num">{run.stockTotal} 只</dd>
              </div>
              <div className="data-card">
                <dt>成功</dt>
                <dd className="num ok-t">{run.stockSucceeded} 只</dd>
              </div>
              <div className="data-card">
                <dt>失败</dt>
                <dd className={`num ${run.stockFailed > 0 ? 'warn-t' : ''}`}>{run.stockFailed} 只</dd>
              </div>
            </dl>
            {run.failures.length > 0 ? (
              <ul className="data-list" aria-label="失败的股票">
                {run.failures.map((failure) => (
                  <li key={failure.code}>
                    <span>
                      {failure.name} <span className="cd">{failure.code}</span>
                    </span>
                    <span className="tag warn">失败</span>
                    <span className="data-detail">{failure.reason}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {run.dataGaps.length > 0 ? (
              <ul className="data-list" aria-label="数据缺口">
                {run.dataGaps.map((gap) => (
                  <li key={gap}>
                    <span>数据缺口</span>
                    <span className="tag warn">缺失</span>
                    <span className="data-detail">{gap}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </section>

      {state.status === 'loading' ? (
        <p className="note" role="status">
          正在读取数据状态…
        </p>
      ) : null}
      {state.status === 'error' ? (
        <div className="notice pref-notice" role="alert">
          <b>数据状态读取失败</b>
          <p>请稍后重试。</p>
          <button
            type="button"
            className="btn sm"
            onClick={() => {
              setState({ status: 'loading' });
              void fetchOverview();
            }}
          >
            重试
          </button>
        </div>
      ) : null}
      {state.status === 'ready' ? (
        <>
          <section className="pref-section" aria-label="数据集">
            <h3>数据集</h3>
            {state.overview.datasets.length === 0 ? (
              <p className="note">没有数据集记录。</p>
            ) : (
              <ul className="data-list">
                {state.overview.datasets.map((dataset) => {
                  const info = statusOf(DATASET_STATUS, dataset.status);
                  const detail = [
                    dataset.source ? `来源 ${dataset.source}` : null,
                    dataset.fallbackFrom.length > 0 ? `由 ${dataset.fallbackFrom.join('、')} 回退` : null,
                    dataset.lastSuccess ? `最近成功 ${formatDateClock(dataset.lastSuccess)}` : null,
                    dataset.lastError,
                    ...dataset.warnings,
                  ].filter(Boolean);
                  return (
                    <li key={dataset.dataset}>
                      <span>{dataset.dataset}</span>
                      <span className={`tag ${info.tone}`}>{info.label}</span>
                      <span className="data-detail">{detail.join('；') || '—'}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
          <section className="pref-section" aria-label="数据源">
            <h3>数据源</h3>
            {state.overview.providers.length === 0 ? (
              <p className="note">没有数据源记录。</p>
            ) : (
              <ul className="data-list">
                {state.overview.providers.map((provider) => {
                  const info = statusOf(PROVIDER_STATUS, provider.status);
                  const detail = [
                    provider.enabled ? null : '已停用',
                    provider.configured ? null : '未配置',
                    provider.cooldown ? '冷却中' : null,
                    provider.lastError,
                    ...provider.warnings,
                  ].filter(Boolean);
                  return (
                    <li key={provider.name}>
                      <span>{provider.label || provider.name}</span>
                      <span className={`tag ${info.tone}`}>{info.label}</span>
                      <span className="data-detail">{detail.join('；') || '—'}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
          {state.overview.warnings.length > 0 ? (
            <p className="note">提醒：{state.overview.warnings.join('；')}</p>
          ) : null}
        </>
      ) : null}
    </>
  );
};
