import type React from 'react';
import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, RotateCcw } from 'lucide-react';
import { meApi } from '../../../api/me';
import {
  getApiErrorCode,
  getApiErrorIssues,
  PLAN_TIME_TOO_EARLY_CODE,
  PREOPEN_TIME_TOO_EARLY_CODE,
  PREOPEN_TIME_TOO_LATE_CODE,
} from '../../../api/multiUserErrors';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import type { PersonalChannelsState } from '../../../hooks/usePersonalChannels';
import type { MeSchedule, PersonalSettingField, PersonalSettingsResponse } from '../../../types/me';
import { Button, Input } from '../../common';
import { SettingsAlert } from '../SettingsAlert';
import { SettingsLoading } from '../SettingsLoading';
import { SettingsSectionCard } from '../SettingsSectionCard';
import { isEnabledChannel } from './channelMeta';
import {
  PLAN_ENABLED_KEY,
  PLAN_TIME_KEY,
  PREOPEN_ENABLED_KEY,
  PREOPEN_TIME_KEY,
  SCHEDULE_SETTING_KEYS,
} from './scheduleKeys';
import { PersonalApiError } from './PersonalApiError';

const TRUE_VALUES = new Set(['1', 'true', 'yes', 'on']);
const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

type Drafts = Partial<Record<string, string>>;

interface PersonalScheduleSectionProps {
  title: string;
  description: string;
  /** The four schedule settings as returned by `GET /me/settings` (absent ones are simply not editable). */
  fields: Record<string, PersonalSettingField | undefined>;
  channels: PersonalChannelsState;
  /** The primary user's schedule is the system (.env) one: shown read-only. */
  readOnly: boolean;
  /** Called with the settings after a save / reset so the panel stays in sync (and refreshes `/me`). */
  onSettingsChanged: (next: PersonalSettingsResponse) => void;
  /** Where to add the first channel (the channels tab, or the system settings for the primary user). */
  onAddChannel?: () => void;
}

/** Own value of a time setting: empty while the user relies on the system default. */
function ownTime(field: PersonalSettingField | undefined): string {
  return field && field.source === 'user' ? field.value.trim() : '';
}

/**
 * 推送与定时 (docs/design/multi-user.md section 6 / 21.3): the next-day plan and pre-open push switches and times,
 * the effective schedule from `GET /me/schedule`, the allowed range as a hint and a notice when nothing can be pushed to.
 * The values are ordinary personal settings, saved through `PUT /me/settings`; the server rejects a plan time that is
 * too early (`plan_time_too_early`) or a pre-open time that is too late (`preopen_time_too_late`) and saves nothing.
 */
export const PersonalScheduleSection: React.FC<PersonalScheduleSectionProps> = ({
  title,
  description,
  fields,
  channels,
  readOnly,
  onSettingsChanged,
  onAddChannel,
}) => {
  const { t } = useUiLanguage();
  const [schedule, setSchedule] = useState<MeSchedule | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [drafts, setDrafts] = useState<Drafts>({});
  const [isSaving, setIsSaving] = useState(false);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<unknown>(null);
  const [timeErrors, setTimeErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const loadSchedule = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      setSchedule(await meApi.getSchedule());
    } catch (error) {
      setLoadError(error);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSchedule();
  }, [loadSchedule]);

  const plan = schedule?.dailyPlan;
  const preopen = schedule?.preopenPush;
  const saved: Record<string, string> = {
    [PLAN_ENABLED_KEY]: plan ? String(plan.enabled) : '',
    [PLAN_TIME_KEY]: ownTime(fields[PLAN_TIME_KEY]),
    [PREOPEN_ENABLED_KEY]: preopen ? String(preopen.enabled) : '',
    [PREOPEN_TIME_KEY]: ownTime(fields[PREOPEN_TIME_KEY]),
  };
  const valueOf = (key: string): string => drafts[key] ?? saved[key];
  const isOn = (key: string): boolean => TRUE_VALUES.has(valueOf(key).trim().toLowerCase());
  const dirtyKeys = SCHEDULE_SETTING_KEYS.filter((key) => drafts[key] !== undefined && drafts[key] !== saved[key]);

  const setDraft = (key: string, value: string) => {
    setNotice(null);
    setSaveError(null);
    setTimeErrors((current) => {
      if (!(key in current)) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    setDrafts((current) => ({ ...current, [key]: value }));
  };

  const timeErrorMessage = (code: string, key: string, limit: string | undefined): string => {
    if (code === PLAN_TIME_TOO_EARLY_CODE) {
      return `${t('personalSchedule.error.planTimeTooEarly.title')}：${t('personalSchedule.error.planTimeTooEarly.message', {
        earliest: limit ?? plan?.earliestTime ?? '',
      })}`;
    }
    if (code === PREOPEN_TIME_TOO_EARLY_CODE) {
      return `${t('personalSchedule.error.preopenTimeTooEarly.title')}：${t('personalSchedule.error.preopenTimeTooEarly.message', {
        earliest: limit ?? preopen?.earliestTime ?? '',
      })}`;
    }
    if (code === PREOPEN_TIME_TOO_LATE_CODE) {
      return `${t('personalSchedule.error.preopenTimeTooLate.title')}：${t('personalSchedule.error.preopenTimeTooLate.message', {
        latest: limit ?? preopen?.latestTime ?? '',
      })}`;
    }
    return key.endsWith('_TIME') ? t('personalSchedule.error.invalidTime') : '';
  };

  const handleSave = async () => {
    if (!dirtyKeys.length) return;
    const malformed = dirtyKeys.filter((key) => key.endsWith('_TIME') && drafts[key] !== '' && !TIME_PATTERN.test(drafts[key] ?? ''));
    if (malformed.length) {
      setTimeErrors(Object.fromEntries(malformed.map((key) => [key, t('personalSchedule.error.invalidTime')])));
      return;
    }
    setIsSaving(true);
    setSaveError(null);
    setTimeErrors({});
    setNotice(null);
    try {
      const next = await meApi.updateSettings(dirtyKeys.map((key) => ({ key, value: drafts[key] ?? '' })));
      onSettingsChanged(next);
      setDrafts({});
      setNotice(t('personalSchedule.saved'));
      await loadSchedule();
    } catch (error) {
      const code = getApiErrorCode(error);
      const issues = getApiErrorIssues(error).filter((issue) => issue.key.endsWith('_TIME'));
      const perField: Record<string, string> = {};
      for (const issue of issues) {
        const message = timeErrorMessage(issue.code, issue.key, issue.earliestTime ?? issue.latestTime);
        if (message) perField[issue.key] = message;
      }
      if (
        !Object.keys(perField).length
        && (code === PLAN_TIME_TOO_EARLY_CODE || code === PREOPEN_TIME_TOO_EARLY_CODE || code === PREOPEN_TIME_TOO_LATE_CODE)
      ) {
        const key = code === PLAN_TIME_TOO_EARLY_CODE ? PLAN_TIME_KEY : PREOPEN_TIME_KEY;
        perField[key] = timeErrorMessage(code, key, undefined);
      }
      if (Object.keys(perField).length) {
        setTimeErrors(perField);
      } else {
        setSaveError(error);
      }
    } finally {
      setIsSaving(false);
    }
  };

  const handleReset = async (key: string) => {
    setResettingKey(key);
    setSaveError(null);
    setNotice(null);
    try {
      onSettingsChanged(await meApi.resetSetting(key));
      setDrafts((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      await loadSchedule();
    } catch (error) {
      setSaveError(error);
    } finally {
      setResettingKey(null);
    }
  };

  const renderReset = (key: string, label: string) => {
    const field = fields[key];
    if (readOnly || !field || !field.editable || field.source !== 'user') return null;
    return (
      <Button
        type="button"
        variant="settings-secondary"
        size="xsm"
        className="h-7 gap-1 px-2 text-xs"
        disabled={isSaving || resettingKey !== null}
        isLoading={resettingKey === key}
        loadingText={t('personalSettings.resetting')}
        aria-label={t('personalSettings.resetAria', { name: label })}
        onClick={() => void handleReset(key)}
      >
        <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
        {t('personalSettings.reset')}
      </Button>
    );
  };

  const renderSwitch = (key: string, label: string) => (
    <label className="inline-flex cursor-pointer items-center gap-3">
      <input
        type="checkbox"
        role="switch"
        data-testid={`schedule-switch-${key}`}
        checked={isOn(key)}
        disabled={readOnly || isSaving || !fields[key]?.editable}
        onChange={(event) => setDraft(key, event.target.checked ? 'true' : 'false')}
      />
      <span className="text-sm font-medium text-foreground">{label}</span>
    </label>
  );

  const renderTime = (key: string, label: string, hint: string, effective: string) => (
    <div className="space-y-1">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[10rem] flex-1">
          <Input
            id={`schedule-${key}`}
            type="time"
            label={label}
            value={valueOf(key)}
            hint={hint}
            error={timeErrors[key]}
            disabled={readOnly || isSaving || !fields[key]?.editable}
            onChange={(event) => setDraft(key, event.target.value)}
          />
        </div>
        {renderReset(key, label)}
      </div>
      <p className="text-xs text-secondary-text" data-testid={`schedule-effective-${key}`}>
        {t('personalSchedule.effective', { time: effective })}
      </p>
    </div>
  );

  const enabledChannels = (channels.data?.channels ?? []).filter(isEnabledChannel);
  const showNoChannel = Boolean(channels.data) && !channels.isLoading && !channels.loadError && enabledChannels.length === 0;
  const planTimes = plan?.times.join('、') ?? '';

  return (
    <SettingsSectionCard
      title={title}
      description={description}
      actions={
        !readOnly && schedule ? (
          <Button
            type="button"
            variant="settings-primary"
            size="sm"
            disabled={!dirtyKeys.length || isSaving || resettingKey !== null}
            isLoading={isSaving}
            loadingText={t('personalSchedule.saving')}
            onClick={() => void handleSave()}
          >
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            {t('personalSchedule.save')}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-4" data-testid="personal-schedule">
        {readOnly ? <SettingsAlert variant="warning" title={t('personalSchedule.managed')} message="" /> : null}

        {showNoChannel ? (
          <SettingsAlert
            variant="warning"
            title={t('personalSchedule.noChannel.title')}
            message={t('personalSchedule.noChannel.message')}
            actionLabel={onAddChannel ? t('personalSchedule.noChannel.action') : undefined}
            onAction={onAddChannel}
          />
        ) : null}

        {loadError ? (
          <PersonalApiError error={loadError} actionLabel={t('common.retry')} onAction={() => void loadSchedule()} />
        ) : null}
        {saveError ? <PersonalApiError error={saveError} onDismiss={() => setSaveError(null)} /> : null}
        {notice && !saveError ? <SettingsAlert variant="success" title={notice} message="" /> : null}

        {isLoading && !schedule ? (
          <div role="status" aria-label={t('personalSchedule.loading')}>
            <SettingsLoading />
          </div>
        ) : null}

        {plan && preopen ? (
          <>
            <div
              className="rounded-2xl border settings-border bg-[var(--settings-surface)] px-4 py-3"
              data-testid="schedule-summary"
            >
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-text">{t('personalSchedule.summary.title')}</p>
              <ul className="mt-2 space-y-1 text-sm text-secondary-text">
                <li data-testid="schedule-summary-plan">
                  {plan.enabled
                    ? t('personalSchedule.summary.planOn', { times: planTimes })
                    : t('personalSchedule.summary.planOff')}
                </li>
                <li data-testid="schedule-summary-preopen">
                  {preopen.enabled
                    ? t('personalSchedule.summary.preopenOn', { time: preopen.time })
                    : t('personalSchedule.summary.preopenOff')}
                </li>
              </ul>
            </div>

            {!readOnly ? (
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="space-y-3 rounded-2xl border settings-border bg-[var(--settings-surface)] p-4" data-testid="schedule-plan">
                  <h3 className="text-sm font-semibold text-foreground">{t('personalSchedule.plan.title')}</h3>
                  {renderSwitch(PLAN_ENABLED_KEY, t('personalSchedule.plan.enabled'))}
                  {renderTime(
                    PLAN_TIME_KEY,
                    t('personalSchedule.plan.time'),
                    t('personalSchedule.plan.timeHint', { earliest: plan.earliestTime }),
                    planTimes || plan.defaultTime,
                  )}
                </div>
                <div className="space-y-3 rounded-2xl border settings-border bg-[var(--settings-surface)] p-4" data-testid="schedule-preopen">
                  <h3 className="text-sm font-semibold text-foreground">{t('personalSchedule.preopen.title')}</h3>
                  {renderSwitch(PREOPEN_ENABLED_KEY, t('personalSchedule.preopen.enabled'))}
                  {renderTime(
                    PREOPEN_TIME_KEY,
                    t('personalSchedule.preopen.time'),
                    preopen.earliestTime
                      ? t('personalSchedule.preopen.timeRangeHint', { earliest: preopen.earliestTime, latest: preopen.latestTime })
                      : t('personalSchedule.preopen.timeHint', { latest: preopen.latestTime }),
                    preopen.time || preopen.defaultTime,
                  )}
                </div>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </SettingsSectionCard>
  );
};
