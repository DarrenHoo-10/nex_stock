import type React from 'react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { usePlanUiStore } from '../../stores/planUiStore';
import { PlanIcon } from './PlanIcon';
import { RunStatusCapsule } from './RunStatusCapsule';

export interface PlanPageHeaderProps {
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  /** Extra controls rendered before the run-status capsule. */
  actions?: React.ReactNode;
  /** Capsule click. Default: sets `runDialogOpen` in `usePlanUiStore`. */
  onOpenRun?: () => void;
  /** Assistant button click. Default: sets `assistantOpen` in `usePlanUiStore`. */
  onOpenAssistant?: () => void;
  /** Hide the run-status capsule (default: shown). */
  hideRunStatus?: boolean;
  /** Hide the white assistant button (default: shown). */
  hideAssistant?: boolean;
}

/** Page title block with the run-status capsule and the white 「助手」 button on the right. */
export const PlanPageHeader: React.FC<PlanPageHeaderProps> = ({
  title,
  subtitle,
  actions,
  onOpenRun,
  onOpenAssistant,
  hideRunStatus = false,
  hideAssistant = false,
}) => {
  const { t } = useUiLanguage();
  const assistantOpen = usePlanUiStore((state) => state.assistantOpen);
  const setAssistantOpen = usePlanUiStore((state) => state.setAssistantOpen);

  return (
    <header className="head">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p className="sub">{subtitle}</p> : null}
      </div>
      <div className="head-right">
        {actions}
        {hideRunStatus ? null : <RunStatusCapsule onClick={onOpenRun} />}
        {hideAssistant ? null : (
          <button
            type="button"
            className="btn primary"
            aria-controls="assistant"
            aria-expanded={assistantOpen}
            aria-haspopup="dialog"
            onClick={onOpenAssistant ?? (() => setAssistantOpen(true))}
          >
            <PlanIcon name="assist" size={16} />
            {t('plan.assistant')}
          </button>
        )}
      </div>
    </header>
  );
};
