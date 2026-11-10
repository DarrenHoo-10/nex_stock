import type React from 'react';
import { useState } from 'react';
import { Pencil, Plus, Send, Trash2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { meApi } from '../../../api/me';
import { getApiErrorCode } from '../../../api/multiUserErrors';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import type { PersonalChannelsState } from '../../../hooks/usePersonalChannels';
import type { ChannelTestResult, ChannelTypeInfo, PersonalChannel } from '../../../types/me';
import { Badge, Button, ConfirmDialog } from '../../common';
import { SettingsAlert } from '../SettingsAlert';
import { SettingsLoading } from '../SettingsLoading';
import { SettingsSectionCard } from '../SettingsSectionCard';
import {
  channelFieldLabelKey,
  channelTestFailureKey,
  channelTypeIcon,
  channelTypeLabelKey,
  configFieldText,
  findChannelType,
} from './channelMeta';
import { ChannelFormDialog } from './ChannelFormDialog';
import { PersonalApiError } from './PersonalApiError';

type TestState =
  | { status: 'testing' }
  | { status: 'done'; result: ChannelTestResult }
  | { status: 'error'; error: unknown };

interface PersonalChannelsSectionProps {
  channels: PersonalChannelsState;
  /** Whether the section is shown inside the "push channels" tab (the title comes from the tab). */
  title: string;
  description: string;
}

/**
 * 推送渠道 (docs/design/multi-user.md section 5 / 21.1): the user's own channels with an enabled switch, the masked
 * config, a test message and an add / edit dialog with per-type fields. Secrets are only ever shown as `******`.
 * The primary user's channels are the `.env` ones: listed read-only by type and managed in the system settings.
 */
export const PersonalChannelsSection: React.FC<PersonalChannelsSectionProps> = ({ channels, title, description }) => {
  const navigate = useNavigate();
  const { t } = useUiLanguage();
  const { data, isLoading, loadError, reload, replaceAll, upsert } = channels;
  const [editing, setEditing] = useState<PersonalChannel | 'new' | null>(null);
  const [deleting, setDeleting] = useState<PersonalChannel | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [tests, setTests] = useState<Record<number, TestState>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const isManagedElsewhere = data?.managedInSystemSettings ?? false;
  const types: ChannelTypeInfo[] = data?.channelTypes ?? [];
  const list = data?.channels ?? [];
  const maxChannels = data?.maxChannels ?? 0;
  const limitReached = maxChannels > 0 && list.length >= maxChannels;

  const typeLabel = (channel: PersonalChannel): string => {
    // System (.env) channels carry their display name from the server; they are not in the personal catalog.
    if (channel.source === 'env') return channel.name || channel.channelType;
    const key = channelTypeLabelKey(channel.channelType);
    return key ? t(key) : findChannelType(types, channel.channelType)?.label ?? channel.channelType;
  };
  // The server names a channel after its type when the user gave no name; show that default in the UI language.
  const hasCustomName = (channel: PersonalChannel): boolean =>
    channel.source === 'user'
    && channel.name !== ''
    && channel.name !== findChannelType(types, channel.channelType)?.label
    && channel.name !== typeLabel(channel);
  const displayName = (channel: PersonalChannel): string => (hasCustomName(channel) ? channel.name : typeLabel(channel));

  const failWith = (error: unknown) => {
    setActionError(error);
    setNotice(null);
    // The channel disappeared (or never was the user's): the list on screen is stale.
    if (getApiErrorCode(error) === 'channel_not_found') void reload();
  };

  const handleToggle = async (channel: PersonalChannel, enabled: boolean) => {
    if (channel.id === null) return;
    setTogglingId(channel.id);
    setActionError(null);
    setNotice(null);
    try {
      upsert(await meApi.updateNotificationChannel(channel.id, { enabled }));
    } catch (error) {
      failWith(error);
    } finally {
      setTogglingId(null);
    }
  };

  const handleTest = async (channel: PersonalChannel) => {
    if (channel.id === null) return;
    const id = channel.id;
    setTests((current) => ({ ...current, [id]: { status: 'testing' } }));
    try {
      const result = await meApi.testNotificationChannel(id);
      setTests((current) => ({ ...current, [id]: { status: 'done', result } }));
    } catch (error) {
      setTests((current) => ({ ...current, [id]: { status: 'error', error } }));
      if (getApiErrorCode(error) === 'channel_not_found') void reload();
    }
  };

  const handleDelete = async () => {
    if (!deleting || deleting.id === null) return;
    const target = deleting;
    setIsDeleting(true);
    setActionError(null);
    try {
      replaceAll(await meApi.deleteNotificationChannel(target.id as number));
      setTests((current) => {
        const next = { ...current };
        delete next[target.id as number];
        return next;
      });
      setNotice(t('personalChannels.deleted', { name: displayName(target) }));
    } catch (error) {
      failWith(error);
    } finally {
      setIsDeleting(false);
      setDeleting(null);
    }
  };

  const renderTest = (channel: PersonalChannel) => {
    const state = channel.id === null ? undefined : tests[channel.id];
    if (!state) return null;
    if (state.status === 'testing') {
      return <p className="text-xs text-muted-text">{t('personalChannels.test.testing')}</p>;
    }
    if (state.status === 'error') {
      return <PersonalApiError error={state.error} />;
    }
    const { result } = state;
    if (result.success && result.status === 'sent') {
      return (
        <SettingsAlert
          variant="success"
          title={t('personalChannels.test.sent')}
          message=""
        />
      );
    }
    return (
      <SettingsAlert
        variant="error"
        title={t('personalChannels.test.failureTitle')}
        message={t(channelTestFailureKey(result.errorCategory))}
      />
    );
  };

  const renderConfig = (channel: PersonalChannel) => {
    const type = findChannelType(types, channel.channelType);
    if (!type || !channel.readable) return null;
    const rows = type.fields
      .map((field) => ({ field, text: configFieldText(channel.config, field.name) }))
      .filter(({ text }) => text !== '');
    if (rows.length === 0) return null;
    return (
      <dl className="mt-3 grid gap-x-6 gap-y-1 pl-8 text-xs text-muted-text sm:grid-cols-2 xl:grid-cols-3" data-testid={`channel-config-${channel.id}`}>
        {rows.map(({ field, text }) => {
          const labelKey = channelFieldLabelKey(field.name);
          return (
            <div key={field.name} className="flex min-w-0 gap-2">
              <dt className="shrink-0">{labelKey ? t(labelKey) : field.label}</dt>
              <dd className="min-w-0 truncate font-mono text-secondary-text">{text}</dd>
            </div>
          );
        })}
      </dl>
    );
  };

  const renderChannel = (channel: PersonalChannel) => {
    const Icon = channelTypeIcon(channel.channelType);
    const name = displayName(channel);
    const editable = channel.editable && channel.id !== null && !isManagedElsewhere;
    const testState = channel.id === null ? undefined : tests[channel.id];
    const rowKey = channel.id ?? `env-${channel.channelType}`;
    return (
      <li
        key={rowKey}
        data-testid={`channel-${rowKey}`}
        className="border-b settings-border px-4 py-4 last:border-b-0"
      >
        <div className="flex flex-wrap items-start gap-3">
          <Icon className="mt-0.5 h-5 w-5 shrink-0 text-muted-text" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-foreground">{name}</span>
              {hasCustomName(channel) ? (
                <Badge variant="default" size="sm">{typeLabel(channel)}</Badge>
              ) : null}
              {channel.source === 'env' ? (
                <Badge variant="info" size="sm">{t('personalChannels.primary.sourceEnv')}</Badge>
              ) : null}
              {!channel.readable ? (
                <Badge variant="warning" size="sm">{t('personalChannels.unreadable')}</Badge>
              ) : null}
              {channel.source === 'user' ? (
                <Badge variant={channel.enabled ? 'success' : 'default'} size="sm">
                  {channel.enabled ? t('common.enabled') : t('common.disabled')}
                </Badge>
              ) : null}
            </div>
          </div>

          {editable ? (
            <div className="flex flex-wrap items-center gap-2">
              <label className="inline-flex cursor-pointer items-center gap-2 text-xs text-secondary-text">
                <input
                  type="checkbox"
                  role="switch"
                  aria-label={t('personalChannels.enabledAria', { name })}
                  checked={channel.enabled}
                  disabled={togglingId === channel.id}
                  onChange={(event) => void handleToggle(channel, event.target.checked)}
                />
              </label>
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                aria-label={t('personalChannels.test.aria', { name })}
                isLoading={testState?.status === 'testing'}
                loadingText={t('personalChannels.test.testing')}
                disabled={!channel.readable}
                onClick={() => void handleTest(channel)}
              >
                <Send className="h-4 w-4" aria-hidden="true" />
                {t('personalChannels.test.button')}
              </Button>
              <Button
                type="button"
                variant="settings-secondary"
                size="sm"
                aria-label={t('personalChannels.editAria', { name })}
                onClick={() => setEditing(channel)}
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />
                {t('personalChannels.edit')}
              </Button>
              <Button
                type="button"
                variant="danger-subtle"
                size="sm"
                aria-label={t('personalChannels.deleteAria', { name })}
                onClick={() => setDeleting(channel)}
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
                {t('personalChannels.delete')}
              </Button>
            </div>
          ) : null}
        </div>
        {renderConfig(channel)}
        {testState ? <div className="mt-3 pl-8" data-testid={`channel-test-${rowKey}`}>{renderTest(channel)}</div> : null}
      </li>
    );
  };

  return (
    <SettingsSectionCard
      title={title}
      description={description}
      actions={
        !isManagedElsewhere && data ? (
          <Button
            type="button"
            variant="settings-primary"
            size="sm"
            disabled={limitReached}
            onClick={() => setEditing('new')}
          >
            <Plus className="h-4 w-4" aria-hidden="true" />
            {t('personalChannels.add')}
          </Button>
        ) : undefined
      }
    >
      <div className="space-y-3" data-testid="personal-channels">
        {isManagedElsewhere ? (
          <SettingsAlert
            variant="warning"
            title={t('personalChannels.primary.title')}
            message={t('personalChannels.primary.message')}
            actionLabel={t('personalSettings.primary.action')}
            onAction={() => navigate('/settings')}
          />
        ) : null}

        {loadError ? (
          <PersonalApiError error={loadError} actionLabel={t('common.retry')} onAction={() => void reload()} />
        ) : null}
        {actionError ? <PersonalApiError error={actionError} onDismiss={() => setActionError(null)} /> : null}
        {notice && !actionError ? <SettingsAlert variant="success" title={notice} message="" /> : null}

        {isLoading && !data ? (
          <div role="status" aria-label={t('personalChannels.loading')}>
            <SettingsLoading />
          </div>
        ) : null}

        {data && !isManagedElsewhere ? (
          <p className="text-xs text-muted-text" data-testid="channel-count">
            {t('personalChannels.count', { count: list.length, max: maxChannels })}
            {limitReached ? ` · ${t('personalChannels.error.limitReached.title')}` : ''}
          </p>
        ) : null}

        {data && list.length === 0 ? (
          <p className="rounded-2xl border settings-border bg-[var(--settings-surface)] px-4 py-6 text-center text-sm text-muted-text">
            {isManagedElsewhere ? t('personalChannels.primary.empty') : t('personalChannels.empty')}
          </p>
        ) : null}

        {list.length > 0 ? (
          <ul className="overflow-hidden rounded-2xl border settings-border bg-[var(--settings-surface)]">
            {list.map(renderChannel)}
          </ul>
        ) : null}
      </div>

      <ChannelFormDialog
        channel={editing}
        types={types}
        onClose={() => setEditing(null)}
        onSaved={(saved) => {
          upsert(saved);
          setEditing(null);
          setActionError(null);
          setNotice(t('personalChannels.saved', { name: saved.name || displayName(saved) }));
        }}
        onStale={() => void reload()}
      />
      <ConfirmDialog
        isOpen={deleting !== null}
        isDanger
        title={t('personalChannels.deleteConfirm.title')}
        message={t('personalChannels.deleteConfirm.message', { name: deleting ? displayName(deleting) : '' })}
        confirmText={t('personalChannels.delete')}
        confirmDisabled={isDeleting}
        cancelDisabled={isDeleting}
        onConfirm={() => void handleDelete()}
        onCancel={() => setDeleting(null)}
      />
    </SettingsSectionCard>
  );
};
