import type React from 'react';
import { useEffect, useId, useState } from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Link } from 'react-router-dom';
import { isPlanRunConflict, isPlanServiceUnavailable, triggerRun } from '../../../api/dailyPlan';
import { systemConfigApi } from '../../../api/systemConfig';
import { useLatestPlanRun } from '../../../hooks/useLatestPlanRun';
import { showPlanToast } from '../../../stores/planToastStore';
import { usePlanUiStore } from '../../../stores/planUiStore';
import type { PlanRun, PushStatus, RunStatus } from '../../../types/dailyPlan';
import type { SchedulerStatusResponse } from '../../../types/systemConfig';
import { formatClock, formatDateClock, formatDuration } from './format';
import { PanelHead, SaveProblem } from './shared';
import type { ConfigView, SaveOutcome } from './usePreferencesConfig';

const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;
const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);

const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  running: '运行中',
  pending: '等待运行',
  skipped: '已跳过',
};

const PUSH_STATUS_LABEL: Record<PushStatus, string> = {
  sent: '已发送',
  failed: '发送失败',
  skipped: '已跳过',
  pending: '等待发送',
};

const STEP_MARK: Record<RunStatus, { mark: string; className: string }> = {
  succeeded: { mark: '✓', className: 'ok' },
  partial: { mark: '!', className: 'part' },
  failed: { mark: '✕', className: 'part' },
  running: { mark: '…', className: '' },
  pending: { mark: '…', className: '' },
  skipped: { mark: '–', className: '' },
};

interface RunPushPanelProps {
  config: ConfigView;
  onSave: (items: Array<{ key: string; value: string }>) => Promise<SaveOutcome>;
  onReload: () => void;
}

function isTrue(value: string): boolean {
  return TRUE_VALUES.has(value.trim().toLowerCase());
}

const RunSummary: React.FC<{ run: PlanRun }> = ({ run }) => (
  <div className="pref-run">
    <div className="inset">
      <p className="pref-run-head">
        <span className="big">{run.tradeDate.slice(5)}</span>
        <span className={`tag ${run.status === 'succeeded' ? 'ok' : run.status === 'failed' ? 'warn' : run.status === 'partial' ? 'warn' : 'line'}`}>
          {RUN_STATUS_LABEL[run.status]}
        </span>
        <span className="tag line">{run.trigger === 'manual' ? '手动运行' : '定时运行'}</span>
      </p>
      <p className="note pref-flush">
        {run.startedAt ? `${formatDateClock(run.startedAt)} 开始` : '尚未开始'}
        {run.durationSec !== null ? `，用时 ${formatDuration(run.durationSec)}` : ''}。股票 {run.stockTotal} 只，成功{' '}
        {run.stockSucceeded}，失败 {run.stockFailed}。
      </p>
    </div>

    <ol className="steps pref-steps" aria-label="运行步骤">
      {run.steps.map((step) => {
        const mark = STEP_MARK[step.status];
        return (
          <li key={step.key}>
            <span className={mark.className} aria-hidden="true">
              {mark.mark}
            </span>
            <span>
              {step.label}
              <span className="vh">：{RUN_STATUS_LABEL[step.status]}</span>
              {step.detail ? <span className="pref-step-detail">{step.detail}</span> : null}
            </span>
            <span className="num">{formatClock(step.finishedAt)}</span>
          </li>
        );
      })}
    </ol>

    <p className="pref-push-line" data-testid="push-status">
      飞书推送：{run.push.configured ? PUSH_STATUS_LABEL[run.push.status] : '未配置，没有发送'}
      {run.push.status === 'sent' && run.push.sentAt ? `（${formatClock(run.push.sentAt)}）` : ''}
      {run.push.status === 'failed' && run.push.error ? `：${run.push.error}` : ''}
    </p>

    {run.failures.length > 0 ? (
      <div className="notice pref-notice">
        <b>失败 {run.failures.length} 只</b>
        <ul>
          {run.failures.map((failure) => (
            <li key={failure.code}>
              {failure.name} {failure.code}：{failure.reason}
            </li>
          ))}
        </ul>
      </div>
    ) : null}
    {run.dataGaps.length > 0 ? (
      <p className="note">数据提醒：{run.dataGaps.join('；')}</p>
    ) : null}
  </div>
);

export const RunPushPanel: React.FC<RunPushPanelProps> = ({ config, onSave, onReload }) => {
  const latest = useLatestPlanRun();
  const refreshRun = usePlanUiStore((state) => state.refreshRun);
  const [scheduler, setScheduler] = useState<SchedulerStatusResponse | null>(null);
  const [timeEdit, setTimeEdit] = useState<string | null>(null);
  const [enabledEdit, setEnabledEdit] = useState<boolean | null>(null);
  const [timeError, setTimeError] = useState<string | null>(null);
  const [problem, setProblem] = useState<Exclude<SaveOutcome, { ok: true }> | null>(null);
  const [saving, setSaving] = useState(false);
  const [running, setRunning] = useState(false);
  const idPrefix = useId();

  useEffect(() => {
    let cancelled = false;
    systemConfigApi
      .getSchedulerStatus()
      .then((status) => {
        if (!cancelled) {
          setScheduler(status);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [config.version]);

  const savedTime = config.value('SCHEDULE_TIME').trim();
  const savedEnabled = isTrue(config.value('DAILY_PLAN_ENABLED'));
  const time = timeEdit ?? savedTime;
  const enabled = enabledEdit ?? savedEnabled;
  const changes: Array<{ key: string; value: string }> = [];
  if (timeEdit !== null && timeEdit.trim() !== savedTime) {
    changes.push({ key: 'SCHEDULE_TIME', value: timeEdit.trim() });
  }
  if (enabledEdit !== null && enabledEdit !== savedEnabled) {
    changes.push({ key: 'DAILY_PLAN_ENABLED', value: enabledEdit ? 'true' : 'false' });
  }
  const dirty = changes.length > 0;
  const multiTimes = config.value('SCHEDULE_TIMES').trim() !== '';

  const feishuFromConfig = config.get('FEISHU_WEBHOOK_URL') ? config.isSet('FEISHU_WEBHOOK_URL') : null;
  const latestRun = latest.status === 'ready' ? latest.run : null;
  const feishuConfigured = feishuFromConfig ?? latestRun?.push.configured ?? false;
  const runActive = latestRun?.status === 'running' || latestRun?.status === 'pending';

  const discard = () => {
    setTimeEdit(null);
    setEnabledEdit(null);
    setTimeError(null);
    setProblem(null);
  };

  const handleSave = async (event: React.FormEvent) => {
    event.preventDefault();
    if (timeEdit !== null && !TIME_PATTERN.test(timeEdit.trim())) {
      setTimeError('按 24 小时制填写，例如 16:00');
      return;
    }
    setTimeError(null);
    if (!dirty) {
      return;
    }
    setSaving(true);
    const outcome = await onSave(changes);
    setSaving(false);
    if (outcome.ok) {
      discard();
      showPlanToast('运行设置已保存');
      return;
    }
    setProblem(outcome);
  };

  const runNow = async () => {
    setRunning(true);
    try {
      await triggerRun();
      showPlanToast('已开始运行');
      refreshRun();
    } catch (error) {
      if (isPlanRunConflict(error)) {
        showPlanToast('已有运行在进行中');
      } else if (isPlanServiceUnavailable(error)) {
        showPlanToast('次日计划服务未就绪');
      } else {
        showPlanToast('没能开始运行，请稍后重试');
      }
    } finally {
      setRunning(false);
    }
  };

  const preview = latestRun?.pushPreviewMarkdown ?? null;

  return (
    <>
      <PanelHead title="运行和推送" sub="每个 A 股交易日按下面的时间自动运行，节假日和调休周末按交易所日历跳过" />

      <form className="pref-form" onSubmit={handleSave} noValidate>
        <div className="form-grid pref-run-form">
          <label className="f" htmlFor={`${idPrefix}-time`}>
            运行时间
            <input
              id={`${idPrefix}-time`}
              inputMode="numeric"
              placeholder="16:00"
              value={time}
              aria-invalid={timeError ? true : undefined}
              aria-describedby={timeError ? `${idPrefix}-time-err` : `${idPrefix}-time-note`}
              onChange={(event) => {
                setTimeEdit(event.target.value);
                setTimeError(null);
                setProblem(null);
              }}
            />
            {timeError ? (
              <span className="field-err" id={`${idPrefix}-time-err`}>
                {timeError}
              </span>
            ) : null}
          </label>
          <label className="switch" htmlFor={`${idPrefix}-enabled`}>
            <input
              id={`${idPrefix}-enabled`}
              type="checkbox"
              role="switch"
              checked={enabled}
              onChange={(event) => {
                setEnabledEdit(event.target.checked);
                setProblem(null);
              }}
            />
            <span className="switch-track" aria-hidden="true" />
            <span>每个交易日自动生成次日计划</span>
          </label>
        </div>
        <p className="note" id={`${idPrefix}-time-note`}>
          顺序：板块异动 → 选股 → 自选和持仓 → 换股判断 → 推送。一只股票失败不影响其他股票。
          {multiTimes ? ' 已在高级设置里配置了多个运行时间（SCHEDULE_TIMES），以那里为准。' : ''}
        </p>
        <p className="note pref-scheduler" data-testid="scheduler-status">
          {scheduler === null
            ? '调度器状态读取中…'
            : `调度器：${scheduler.enabled ? (scheduler.running ? '运行中' : '已启用') : '未启用'}${
                scheduler.scheduleTimes.length > 0 ? `，运行时间 ${scheduler.scheduleTimes.join('、')}` : ''
              }${scheduler.nextRunAt ? `，下次 ${formatDateClock(scheduler.nextRunAt)}` : ''}${
                scheduler.lastRunAt ? `，上次 ${formatDateClock(scheduler.lastRunAt)}` : ''
              }`}
          {scheduler?.lastError ? `。最近错误：${scheduler.lastError}` : ''}
        </p>
        <SaveProblem outcome={problem} onReload={() => { discard(); onReload(); }} />
        <div className="actions pref-actions">
          <button type="submit" className="btn primary" disabled={!dirty || saving}>
            {saving ? '正在保存…' : '保存运行设置'}
          </button>
          {dirty ? (
            <button type="button" className="btn quiet" onClick={discard}>
              放弃修改
            </button>
          ) : null}
        </div>
      </form>

      <section className="pref-section inset" aria-labelledby={`${idPrefix}-feishu`}>
        <div className="sec-head">
          <h3 id={`${idPrefix}-feishu`}>飞书群机器人</h3>
          <span className={`tag ${feishuConfigured ? 'ok' : 'warn'}`} data-testid="feishu-status">
            {feishuConfigured ? '已配置' : '未配置'}
          </span>
        </div>
        <p className="note pref-flush">
          Webhook 地址、签名密钥和关键词只保存在本机配置里，这里不显示。
          {feishuConfigured ? '' : '没有配置时，计划只在网页里查看，不会推送。'}
        </p>
        <div className="actions pref-actions">
          <Link className="btn sm" to="/settings?category=notification">
            在高级设置里配置
          </Link>
        </div>
      </section>

      <section className="pref-section" aria-labelledby={`${idPrefix}-latest`}>
        <div className="sec-head">
          <h3 id={`${idPrefix}-latest`}>最近一次运行</h3>
          <button
            type="button"
            className="btn sm"
            disabled={running || runActive}
            onClick={() => void runNow()}
          >
            {running ? '正在提交…' : runActive ? '运行中' : '立即运行'}
          </button>
        </div>
        {latest.status === 'loading' ? (
          <p className="note" role="status">
            正在读取运行记录…
          </p>
        ) : null}
        {latest.status === 'unavailable' ? (
          <p className="notice pref-notice" role="status">
            次日计划服务未就绪，暂时没有运行记录。
          </p>
        ) : null}
        {latest.status === 'error' ? (
          <p className="notice pref-notice" role="alert">
            运行记录读取失败。
            <button type="button" className="btn sm" onClick={latest.reload}>
              重试
            </button>
          </p>
        ) : null}
        {latest.status === 'ready' && latest.run === null ? (
          <p className="note">还没有运行记录。点「立即运行」可以马上跑一次。</p>
        ) : null}
        {latestRun ? <RunSummary run={latestRun} /> : null}
      </section>

      <section className="pref-section" aria-labelledby={`${idPrefix}-preview`}>
        <h3 id={`${idPrefix}-preview`}>飞书消息预览</h3>
        {preview ? (
          <div className="push-preview" data-testid="push-preview">
            <Markdown remarkPlugins={[remarkGfm]}>{preview}</Markdown>
          </div>
        ) : (
          <p className="note">还没有可预览的推送。运行一次后，这里会显示飞书消息正文。</p>
        )}
      </section>
    </>
  );
};
