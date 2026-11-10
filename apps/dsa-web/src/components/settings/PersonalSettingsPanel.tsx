import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { CheckCircle2, RefreshCw, RotateCcw, Zap } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { meApi } from '../../api/me';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import { usePersonalChannels } from '../../hooks/usePersonalChannels';
import type { UiTextKey } from '../../i18n/uiText';
import type {
  PersonalSettingField,
  PersonalSettingGroup,
  PersonalSettingsResponse,
  TestModelResult,
} from '../../types/me';
import type {
  ConfigValidationIssue,
  SystemConfigCategory,
  SystemConfigCategorySchema,
  SystemConfigDataType,
  SystemConfigItem,
  SystemConfigUIControl,
} from '../../types/systemConfig';
import { ApiErrorAlert, Badge, Button } from '../common';
import { SettingsAlert } from './SettingsAlert';
import { SettingsCategoryNav } from './SettingsCategoryNav';
import { SettingsField } from './SettingsField';
import { SettingsLoading } from './SettingsLoading';
import { SettingsSectionCard } from './SettingsSectionCard';
import { PersonalChannelsSection } from './personal/PersonalChannelsSection';
import { PersonalScheduleSection } from './personal/PersonalScheduleSection';
import { SCHEDULE_SETTING_KEYS } from './personal/scheduleKeys';

/** Backend masks a configured secret with this token; sending it (or an empty string) keeps the stored value. */
const SECRET_MASK = '******';
const GROUP_ID_PREFIX = 'personal:';

/** Tabs that are not a backend settings group: the channel list and the schedule are their own sections. */
const CHANNELS_TAB = 'channels';
const SCHEDULE_TAB = 'schedule';

const GROUP_TITLE_KEYS: Record<string, UiTextKey> = {
  [CHANNELS_TAB]: 'personalSettings.group.channels',
  [SCHEDULE_TAB]: 'personalSettings.group.schedule',
  model: 'personalSettings.group.model',
  search: 'personalSettings.group.search',
  data_source: 'personalSettings.group.data_source',
  agent: 'personalSettings.group.agent',
  push: 'personalSettings.group.push',
  position: 'personalSettings.group.position',
  report: 'personalSettings.group.report',
};
const GROUP_DESCRIPTION_KEYS: Record<string, UiTextKey> = {
  [CHANNELS_TAB]: 'personalSettings.groupDescription.channels',
  [SCHEDULE_TAB]: 'personalSettings.groupDescription.schedule',
  model: 'personalSettings.groupDescription.model',
  search: 'personalSettings.groupDescription.search',
  data_source: 'personalSettings.groupDescription.data_source',
  agent: 'personalSettings.groupDescription.agent',
  push: 'personalSettings.groupDescription.push',
  position: 'personalSettings.groupDescription.position',
  report: 'personalSettings.groupDescription.report',
};
const SOURCE_LABEL_KEYS: Record<PersonalSettingField['source'], UiTextKey> = {
  user: 'personalSettings.source.user',
  system_default: 'personalSettings.source.systemDefault',
  unset: 'personalSettings.source.unset',
};
const TEST_FAILURE_KEYS: Record<string, UiTextKey> = {
  auth_failed: 'personalSettings.testModel.failure.auth_failed',
  unreachable: 'personalSettings.testModel.failure.unreachable',
  timeout: 'personalSettings.testModel.failure.timeout',
  bad_response: 'personalSettings.testModel.failure.bad_response',
};

/** Model fields shown first: the few a user needs to get a working model; the rest keep the backend order. */
const MODEL_PRIORITY_KEYS = [
  'LITELLM_MODEL',
  'OPENAI_API_KEY',
  'OPENAI_BASE_URL',
  'OPENAI_MODEL',
  'DEEPSEEK_API_KEY',
  'GEMINI_API_KEY',
  'ANTHROPIC_API_KEY',
  'LITELLM_FALLBACK_MODELS',
];
/**
 * Push preferences only. The switches and times of the next-day plan / pre-open push live in the 推送与定时 section and
 * the channels in 推送渠道 (docs/design/multi-user.md section 21). The legacy per-channel credential keys the personal
 * settings API still returns (`FEISHU_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, SMTP ...) are not read by any push path any
 * more, so none of them is offered: everything outside this allow-list stays hidden.
 */
const PUSH_PREFERENCE_KEY = /^(NOTIFICATION_|SINGLE_STOCK_NOTIFY$|MERGE_EMAIL_NOTIFICATION$)/;

function visibleFields(group: PersonalSettingGroup): PersonalSettingField[] {
  if (group.id === 'push') {
    return group.fields.filter((field) => PUSH_PREFERENCE_KEY.test(field.key));
  }
  if (group.id === 'model') {
    const rank = (key: string) => {
      const index = MODEL_PRIORITY_KEYS.indexOf(key);
      return index < 0 ? MODEL_PRIORITY_KEYS.length : index;
    };
    return [...group.fields].sort((a, b) => rank(a.key) - rank(b.key));
  }
  return group.fields;
}

/** Adapt a personal field to the shape `SettingsField` renders, so the system settings control set is reused. */
function toConfigItem(field: PersonalSettingField): SystemConfigItem {
  return {
    key: field.key,
    value: field.value,
    rawValueExists: field.source === 'user',
    isMasked: field.isSecret && field.value === SECRET_MASK,
    schema: {
      key: field.key,
      title: field.title,
      description: field.description,
      category: 'base' as SystemConfigCategory,
      dataType: field.dataType as SystemConfigDataType,
      uiControl: (field.isSecret ? 'password' : field.uiControl) as SystemConfigUIControl,
      isSensitive: field.isSecret,
      isRequired: false,
      isEditable: field.editable,
      defaultValue: null,
      options: field.options,
      validation: {},
      displayOrder: 0,
    },
  };
}

function groupIssues(error: unknown): Record<string, ConfigValidationIssue[]> {
  const data = (error as { response?: { data?: unknown } } | null)?.response?.data as
    | { detail?: { issues?: unknown } }
    | undefined;
  const issues = data?.detail?.issues;
  const byKey: Record<string, ConfigValidationIssue[]> = {};
  if (!Array.isArray(issues)) {
    return byKey;
  }
  for (const raw of issues) {
    const issue = raw as Partial<ConfigValidationIssue>;
    if (!issue.key || !issue.message) continue;
    (byKey[issue.key] ??= []).push({
      key: issue.key,
      code: issue.code ?? 'invalid_value',
      message: issue.message,
      severity: issue.severity === 'warning' ? 'warning' : 'error',
    });
  }
  return byKey;
}

/** One tab of the group nav: a backend settings group, or the channels / schedule section (`group: null`). */
interface PanelEntry {
  id: string;
  group: PersonalSettingGroup | null;
  fields: PersonalSettingField[];
}

type TestState =
  | { status: 'idle' }
  | { status: 'testing' }
  | { status: 'done'; result: TestModelResult }
  | { status: 'error'; error: ParsedApiError };

/**
 * Personal settings (docs/design/multi-user.md 3.3 / 3.6): the `scope=user` options grouped as model / search /
 * data-source credentials / Agent / push / position rules / report preferences. Fields are rendered with the same
 * `SettingsField` control set as the system settings; every field shows where its value comes from, secrets are
 * masked (leave empty to keep), each own value can be restored to the default, and the model can be tested.
 * The primary user's personal settings are the system (.env) settings, so for them everything is read-only.
 */
const fieldTitle = (field: PersonalSettingField) => field.title || field.key;

export const PersonalSettingsPanel: React.FC = () => {
  const navigate = useNavigate();
  const { language, t } = useUiLanguage();
  const { refresh: refreshCurrentUser } = useCurrentUser();
  const [data, setData] = useState<PersonalSettingsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<ParsedApiError | null>(null);
  const [activeGroup, setActiveGroup] = useState('model');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [isSaving, setIsSaving] = useState(false);
  const [actionError, setActionError] = useState<ParsedApiError | null>(null);
  const [issuesByKey, setIssuesByKey] = useState<Record<string, ConfigValidationIssue[]>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [resettingKey, setResettingKey] = useState<string | null>(null);
  const [testState, setTestState] = useState<TestState>({ status: 'idle' });
  const channels = usePersonalChannels();

  const load = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      setData(await meApi.getSettings());
    } catch (error) {
      setLoadError(getParsedApiError(error));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const fieldsByKey = useMemo(() => {
    const map = new Map<string, PersonalSettingField>();
    data?.groups.forEach((group) => group.fields.forEach((field) => map.set(field.key, field)));
    return map;
  }, [data]);

  const isDirty = useCallback(
    (key: string): boolean => {
      const field = fieldsByKey.get(key);
      const draft = drafts[key];
      if (!field || draft === undefined || draft === field.value) return false;
      // An empty secret means "keep the stored value" - nothing to send.
      return !(field.isSecret && (draft === '' || draft === SECRET_MASK));
    },
    [drafts, fieldsByKey],
  );
  const dirtyKeys = useMemo(() => Object.keys(drafts).filter(isDirty), [drafts, isDirty]);

  const applyResponse = useCallback(
    (next: PersonalSettingsResponse) => {
      setData(next);
      void refreshCurrentUser();
    },
    [refreshCurrentUser],
  );

  const handleChange = (key: string, value: string) => {
    setNotice(null);
    setDrafts((current) => ({ ...current, [key]: value }));
  };

  const handleDiscard = () => {
    setDrafts({});
    setIssuesByKey({});
    setActionError(null);
    setNotice(null);
  };

  const handleSave = async () => {
    if (!dirtyKeys.length) return;
    setIsSaving(true);
    setActionError(null);
    setIssuesByKey({});
    setNotice(null);
    try {
      const next = await meApi.updateSettings(dirtyKeys.map((key) => ({ key, value: drafts[key] })));
      applyResponse(next);
      setDrafts({});
      setNotice(t('personalSettings.saved'));
    } catch (error) {
      setActionError(getParsedApiError(error));
      setIssuesByKey(groupIssues(error));
    } finally {
      setIsSaving(false);
    }
  };

  const handleReset = async (field: PersonalSettingField) => {
    setResettingKey(field.key);
    setActionError(null);
    setIssuesByKey({});
    setNotice(null);
    try {
      const next = await meApi.resetSetting(field.key);
      applyResponse(next);
      setDrafts((current) => Object.fromEntries(Object.entries(current).filter(([key]) => key !== field.key)));
      setNotice(t('personalSettings.resetDone', { name: fieldTitle(field) }));
    } catch (error) {
      setActionError(getParsedApiError(error));
    } finally {
      setResettingKey(null);
    }
  };

  const handleTestModel = async () => {
    setTestState({ status: 'testing' });
    try {
      setTestState({ status: 'done', result: await meApi.testModel() });
    } catch (error) {
      setTestState({ status: 'error', error: getParsedApiError(error) });
    }
  };

  const entries = useMemo<PanelEntry[]>(() => {
    const list: PanelEntry[] = (data?.groups ?? [])
      .map((group) => ({ id: group.id, group, fields: visibleFields(group) }))
      .filter(({ fields }) => fields.length > 0);
    if (!data) return list;
    // The channel list and the schedule sit right before the remaining push preferences.
    const at = list.findIndex(({ id }) => id === 'push' || id === 'position' || id === 'report');
    const virtual: PanelEntry[] = [
      { id: CHANNELS_TAB, group: null, fields: [] },
      { id: SCHEDULE_TAB, group: null, fields: [] },
    ];
    list.splice(at < 0 ? list.length : at, 0, ...virtual);
    return list;
  }, [data]);
  const currentEntry = entries.find(({ id }) => id === activeGroup) ?? entries[0];
  const entryTitle = (entry: PanelEntry): string =>
    GROUP_TITLE_KEYS[entry.id] ? t(GROUP_TITLE_KEYS[entry.id]) : entry.group?.title ?? entry.id;
  const entryDescription = (entry: PanelEntry): string =>
    GROUP_DESCRIPTION_KEYS[entry.id] ? t(GROUP_DESCRIPTION_KEYS[entry.id]) : '';

  const navCategories: SystemConfigCategorySchema[] = entries.map((entry) => ({
    category: `${GROUP_ID_PREFIX}${entry.id}` as SystemConfigCategory,
    title: entryTitle(entry),
    description: entryDescription(entry),
    displayOrder: 0,
    fields: [],
  }));
  const navItems: Record<string, SystemConfigItem[]> = Object.fromEntries(
    entries.map((entry) => [`${GROUP_ID_PREFIX}${entry.id}`, entry.fields.map(toConfigItem)]),
  );
  const navCounts: Record<string, number> = {
    [`${GROUP_ID_PREFIX}${CHANNELS_TAB}`]: channels.data?.channels.length ?? 0,
    [`${GROUP_ID_PREFIX}${SCHEDULE_TAB}`]: SCHEDULE_SETTING_KEYS.filter((key) => fieldsByKey.has(key)).length,
  };
  const scheduleFields = Object.fromEntries(SCHEDULE_SETTING_KEYS.map((key) => [key, fieldsByKey.get(key)]));

  const isReadOnly = data?.managedInSystemSettings ?? false;

  const renderTestResult = () => {
    if (testState.status === 'testing') {
      return <p className="text-xs text-muted-text">{t('personalSettings.testModel.testing')}</p>;
    }
    if (testState.status === 'error') {
      return <ApiErrorAlert error={testState.error} />;
    }
    if (testState.status !== 'done') {
      return null;
    }
    const { result } = testState;
    if (result.success) {
      return (
        <SettingsAlert
          variant="success"
          title={t('personalSettings.testModel.success', { model: result.model || '-', latency: result.latencyMs ?? '-' })}
          message=""
        />
      );
    }
    const failureKey = TEST_FAILURE_KEYS[result.errorCategory ?? ''] ?? 'personalSettings.testModel.failure.unknown';
    return (
      <SettingsAlert
        variant="error"
        title={t('personalSettings.testModel.failureTitle')}
        message={`${t(failureKey)}${result.httpStatus ? ` (${t('personalSettings.testModel.httpStatus', { status: result.httpStatus })})` : ''}`}
      />
    );
  };

  const renderField = (field: PersonalSettingField) => {
    const draft = drafts[field.key];
    const value = draft ?? field.value;
    const dirty = isDirty(field.key);
    const canReset = field.editable && field.source === 'user';
    const issues = issuesByKey[field.key] ?? [];
    return (
      <div key={field.key} data-testid={`personal-field-${field.key}`} className="border-b settings-border last:border-b-0">
        <SettingsField
          item={toConfigItem(field)}
          value={value}
          disabled={isSaving || resettingKey !== null || !field.editable}
          onChange={handleChange}
          issues={issues}
        />
        <div className="flex flex-wrap items-center gap-2 px-4 pb-3 text-[11px] text-muted-text md:pl-[calc(250px+2.5rem)]">
          <Badge
            variant={field.source === 'user' ? 'info' : 'default'}
            size="sm"
            data-testid={`personal-source-${field.key}`}
            data-source={field.source}
          >
            {t(SOURCE_LABEL_KEYS[field.source])}
          </Badge>
          {dirty ? <Badge variant="warning" size="sm">{t('personalSettings.saveWithCount', { count: 1 })}</Badge> : null}
          {field.isSecret && field.editable ? <span>{t('personalSettings.secretHint')}</span> : null}
          {field.source === 'system_default' ? <span>{t('personalSettings.systemDefaultHint')}</span> : null}
          {!field.inherits && field.source === 'unset' && field.editable ? (
            <span>{t('personalSettings.noFallbackHint')}</span>
          ) : null}
          {canReset ? (
            <Button
              type="button"
              variant="settings-secondary"
              size="xsm"
              className="ml-auto h-7 gap-1 px-2 text-xs"
              disabled={isSaving || resettingKey !== null}
              isLoading={resettingKey === field.key}
              loadingText={t('personalSettings.resetting')}
              aria-label={t('personalSettings.resetAria', { name: fieldTitle(field) })}
              onClick={() => void handleReset(field)}
            >
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              {t('personalSettings.reset')}
            </Button>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <div className="settings-page min-h-full px-4 pb-6 pt-4 md:px-6" data-testid="personal-settings">
      <div className="mb-4 rounded-lg border settings-border bg-card/90 px-4 py-4 shadow-soft-card backdrop-blur-sm">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight text-foreground">{t('personalSettings.pageTitle')}</h1>
            <p className="max-w-3xl text-xs leading-5 text-muted-text sm:text-sm sm:leading-6">
              {t('personalSettings.pageDescription')}
            </p>
          </div>
          {!isReadOnly ? (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                className="px-2.5"
                onClick={handleDiscard}
                disabled={isLoading || isSaving || (!dirtyKeys.length && !actionError)}
              >
                <RefreshCw className="h-4 w-4" aria-hidden="true" />
                {t('personalSettings.discard')}
              </Button>
              <Button
                type="button"
                variant="settings-primary"
                size="sm"
                className="px-2.5"
                onClick={() => void handleSave()}
                disabled={!dirtyKeys.length || isSaving || isLoading}
                isLoading={isSaving}
                loadingText={t('personalSettings.saving')}
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                {dirtyKeys.length ? t('personalSettings.saveWithCount', { count: dirtyKeys.length }) : t('personalSettings.save')}
              </Button>
            </div>
          ) : null}
        </div>
        {actionError ? <ApiErrorAlert className="mt-3" error={actionError} onDismiss={() => setActionError(null)} /> : null}
        {notice && !actionError ? (
          <SettingsAlert className="mt-3" variant="success" title={notice} message="" />
        ) : null}
      </div>

      {isReadOnly ? (
        <SettingsAlert
          className="mb-4"
          variant="warning"
          title={t('personalSettings.primary.title')}
          message={t('personalSettings.primary.message')}
          actionLabel={t('personalSettings.primary.action')}
          onAction={() => navigate('/settings')}
        />
      ) : null}

      {loadError ? (
        <ApiErrorAlert
          className="mb-4"
          error={loadError}
          actionLabel={t('common.retry')}
          onAction={() => void load()}
        />
      ) : null}

      {isLoading && !data ? (
        <div role="status" aria-label={t('personalSettings.loading')}>
          <SettingsLoading />
        </div>
      ) : null}

      {data && currentEntry ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[260px_minmax(0,1fr)]">
          <aside className="lg:sticky lg:top-4 lg:self-start">
            <SettingsCategoryNav
              categories={navCategories}
              itemsByCategory={navItems}
              activeCategory={`${GROUP_ID_PREFIX}${currentEntry.id}`}
              counts={navCounts}
              onSelect={(category) => setActiveGroup(category.slice(GROUP_ID_PREFIX.length))}
              title={t('personalSettings.groupNav')}
              description=""
            />
          </aside>

          <section className="space-y-4">
            {currentEntry.id === CHANNELS_TAB ? (
              <PersonalChannelsSection
                channels={channels}
                title={entryTitle(currentEntry)}
                description={entryDescription(currentEntry)}
              />
            ) : currentEntry.id === SCHEDULE_TAB ? (
              <PersonalScheduleSection
                title={entryTitle(currentEntry)}
                description={entryDescription(currentEntry)}
                fields={scheduleFields}
                channels={channels}
                readOnly={isReadOnly}
                onSettingsChanged={applyResponse}
                onAddChannel={isReadOnly ? () => navigate('/settings') : () => setActiveGroup(CHANNELS_TAB)}
              />
            ) : (
            <SettingsSectionCard
              title={entryTitle(currentEntry)}
              description={entryDescription(currentEntry) || undefined}
              actions={
                currentEntry.id === 'model' && !isReadOnly ? (
                  <Button
                    type="button"
                    variant="settings-secondary"
                    size="sm"
                    isLoading={testState.status === 'testing'}
                    loadingText={t('personalSettings.testModel.testing')}
                    disabled={testState.status === 'testing' || dirtyKeys.length > 0}
                    onClick={() => void handleTestModel()}
                  >
                    <Zap className="h-4 w-4" aria-hidden="true" />
                    {t('personalSettings.testModel.button')}
                  </Button>
                ) : undefined
              }
            >
              {currentEntry.id === 'model' && !isReadOnly ? (
                <div className="space-y-2" data-testid="personal-model-test">
                  {dirtyKeys.length > 0 ? (
                    <p className="text-xs text-muted-text">{t('personalSettings.testModel.unsavedHint')}</p>
                  ) : null}
                  {renderTestResult()}
                </div>
              ) : null}
              <div
                className="overflow-hidden rounded-2xl border settings-border bg-[var(--settings-surface)]"
                lang={language}
              >
                {currentEntry.fields.map(renderField)}
              </div>
            </SettingsSectionCard>
            )}
          </section>
        </div>
      ) : null}

      {data && !currentEntry ? (
        <p className="text-sm text-muted-text">{t('personalSettings.groupEmpty')}</p>
      ) : null}
    </div>
  );
};
