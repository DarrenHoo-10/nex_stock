import type React from 'react';
import { useLatestPlanRun } from '../../hooks/useLatestPlanRun';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { formatUiText } from '../../i18n/uiText';
import type { UiTextKey } from '../../i18n/uiText';
import { usePlanUiStore } from '../../stores/planUiStore';
import type { RunStatus } from '../../types/dailyPlan';

export interface RunStatusCapsuleProps {
  /** Called when the capsule is clicked. Default: opens the run dialog flag in `usePlanUiStore`. */
  onClick?: () => void;
}

const RUN_STATUS_KEY: Record<RunStatus, UiTextKey> = {
  succeeded: 'plan.run.succeeded',
  partial: 'plan.run.partial',
  failed: 'plan.run.failed',
  running: 'plan.run.running',
  pending: 'plan.run.pending',
  skipped: 'plan.run.skipped',
};

const RUN_STATUS_DOT: Record<RunStatus, string> = {
  succeeded: '',
  partial: 'warn',
  failed: 'bad',
  running: 'busy',
  pending: 'busy',
  skipped: 'idle',
};

/** "2026-11-06" -> "11-06". */
function shortDate(tradeDate: string): string {
  return /^\d{4}-\d{2}-\d{2}/.test(tradeDate) ? tradeDate.slice(5, 10) : tradeDate;
}

/**
 * Header capsule showing the latest plan run (date + status). Handles loading, no run yet
 * (`run: null`) and the 503 `daily_plan_unavailable` state.
 */
export const RunStatusCapsule: React.FC<RunStatusCapsuleProps> = ({ onClick }) => {
  const { t } = useUiLanguage();
  const latest = useLatestPlanRun();
  const setRunDialogOpen = usePlanUiStore((state) => state.setRunDialogOpen);

  let date: string | null = null;
  let text: string;
  let dot = 'idle';
  if (latest.status === 'loading') {
    text = t('plan.run.loading');
    dot = 'busy';
  } else if (latest.status === 'unavailable') {
    text = t('plan.run.unavailable');
    dot = 'warn';
  } else if (latest.status === 'error') {
    text = t('plan.run.error');
    dot = 'warn';
  } else if (latest.run === null) {
    text = t('plan.run.none');
  } else {
    date = shortDate(latest.run.tradeDate);
    text = t(RUN_STATUS_KEY[latest.run.status]);
    dot = RUN_STATUS_DOT[latest.run.status];
  }

  const label = formatUiText(t('plan.run.openLabel'), { status: date ? `${date} ${text}` : text });

  return (
    <button
      type="button"
      className="cap"
      aria-haspopup="dialog"
      aria-label={label}
      data-testid="plan-run-capsule"
      onClick={onClick ?? (() => setRunDialogOpen(true))}
    >
      <span className={`dot ${dot}`.trim()} aria-hidden="true" />
      {date ? <span className="num">{date}</span> : null}
      <span className={date ? 'cap-text hide-s' : 'cap-text'}>{text}</span>
    </button>
  );
};
