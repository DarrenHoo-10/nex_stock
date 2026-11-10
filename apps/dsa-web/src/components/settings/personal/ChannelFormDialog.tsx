import type React from 'react';
import { useId, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { meApi } from '../../../api/me';
import { getApiErrorCode, getApiErrorIssues } from '../../../api/multiUserErrors';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import type {
  ChannelConfig,
  ChannelFieldInfo,
  ChannelTypeInfo,
  PersonalChannel,
} from '../../../types/me';
import { Button, Input, Select } from '../../common';
import { SettingsAlert } from '../SettingsAlert';
import {
  channelFieldLabelKey,
  channelIssueTextKey,
  channelTypeLabelKey,
  configFieldText,
  isMultilineField,
  isSecretSet,
} from './channelMeta';
import { PersonalApiError } from './PersonalApiError';

interface ChannelFormDialogProps {
  /** Null while closed. */
  channel: PersonalChannel | 'new' | null;
  types: ChannelTypeInfo[];
  onClose: () => void;
  /** The channel as stored after a successful create / update. */
  onSaved: (channel: PersonalChannel) => void;
  /** The channel no longer exists (404): the list should be refreshed. */
  onStale: () => void;
}

const TEXTAREA_CLASS =
  'input-surface input-focus-glow min-h-[96px] w-full resize-y rounded-xl border bg-transparent px-4 py-3 text-sm transition-all focus:outline-none disabled:cursor-not-allowed disabled:opacity-60';

function initialValues(type: ChannelTypeInfo | undefined, channel: PersonalChannel | null): Record<string, string> {
  const values: Record<string, string> = {};
  for (const field of type?.fields ?? []) {
    values[field.name] = channel ? configFieldText(channel.config, field.name) : '';
  }
  return values;
}

/** The body of the form; remounted (via `key`) for every open so the drafts never leak between channels. */
const ChannelForm: React.FC<Omit<ChannelFormDialogProps, 'channel'> & { channel: PersonalChannel | null }> = ({
  channel,
  types,
  onClose,
  onSaved,
  onStale,
}) => {
  const { t } = useUiLanguage();
  const idPrefix = useId();
  const isEdit = channel !== null;
  const [channelType, setChannelType] = useState(channel?.channelType ?? '');
  const type = useMemo(() => types.find((item) => item.channelType === channelType), [types, channelType]);
  const [name, setName] = useState(channel?.name ?? '');
  const [enabled, setEnabled] = useState(channel?.enabled ?? true);
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(
    types.find((item) => item.channelType === channel?.channelType),
    channel,
  ));
  const [fieldIssues, setFieldIssues] = useState<Record<string, string>>({});
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [isSaving, setIsSaving] = useState(false);

  const typeLabel = (info: ChannelTypeInfo) => {
    const key = channelTypeLabelKey(info.channelType);
    return key ? t(key) : info.label;
  };
  const fieldLabel = (field: ChannelFieldInfo) => {
    const key = channelFieldLabelKey(field.name);
    return key ? t(key) : field.label;
  };

  const handleTypeChange = (next: string) => {
    setChannelType(next);
    setValues(initialValues(types.find((item) => item.channelType === next), null));
    setFieldIssues({});
    setSubmitError(null);
  };

  const setValue = (fieldName: string, value: string) => {
    setValues((current) => ({ ...current, [fieldName]: value }));
    setFieldIssues((current) => {
      if (!(fieldName in current)) return current;
      const next = { ...current };
      delete next[fieldName];
      return next;
    });
  };

  /** A secret that still holds the mask (or is empty) keeps the stored value, so it is never sent. */
  const buildConfig = (): ChannelConfig => {
    const config: ChannelConfig = {};
    for (const field of type?.fields ?? []) {
      const draft = values[field.name] ?? '';
      if (field.secret) {
        if (draft !== '' && !(isEdit && draft === configFieldText(channel.config, field.name))) {
          config[field.name] = draft;
        }
        continue;
      }
      const value = draft.trim();
      if (!isEdit) {
        if (value !== '') config[field.name] = value;
      } else if (value !== configFieldText(channel.config, field.name)) {
        config[field.name] = value; // "" clears an optional field
      }
    }
    return config;
  };

  const missingRequired = (): Record<string, string> => {
    const missing: Record<string, string> = {};
    for (const field of type?.fields ?? []) {
      if (!field.required) continue;
      const draft = (values[field.name] ?? '').trim();
      const stored = isEdit && isSecretSet(channel.config, field);
      if (draft === '' && !stored) missing[field.name] = t('personalChannels.issue.required');
    }
    return missing;
  };

  const config = buildConfig();
  const trimmedName = name.trim();
  const dirty = !isEdit
    || Object.keys(config).length > 0
    || trimmedName !== channel.name
    || enabled !== channel.enabled;

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!type) return;
    const missing = missingRequired();
    if (Object.keys(missing).length > 0) {
      setFieldIssues(missing);
      setSubmitError(null);
      return;
    }
    setIsSaving(true);
    setSubmitError(null);
    setFieldIssues({});
    try {
      const saved = isEdit
        ? await meApi.updateNotificationChannel(channel.id as number, {
          ...(trimmedName !== channel.name ? { name: trimmedName } : {}),
          ...(enabled !== channel.enabled ? { enabled } : {}),
          ...(Object.keys(config).length > 0 ? { config } : {}),
        })
        : await meApi.createNotificationChannel({
          channelType: type.channelType,
          name: trimmedName || undefined,
          enabled,
          config,
        });
      onSaved(saved);
    } catch (error) {
      const issues: Record<string, string> = {};
      for (const issue of getApiErrorIssues(error)) {
        if (!(issue.key in issues)) issues[issue.key] = t(channelIssueTextKey(issue.code));
      }
      setFieldIssues(issues);
      setSubmitError(error);
      if (getApiErrorCode(error) === 'channel_not_found') onStale();
    } finally {
      setIsSaving(false);
    }
  };

  const titleId = `${idPrefix}-title`;
  const typeOptions = types.map((item) => ({ value: item.channelType, label: typeLabel(item) }));

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={() => {
        if (!isSaving) onClose();
      }}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid="channel-form-dialog"
        className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl border border-border/70 bg-elevated p-6 shadow-2xl"
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => void handleSubmit(event)}
        noValidate
      >
        <h3 id={titleId} className="text-lg font-medium text-foreground">
          {isEdit ? t('personalChannels.dialog.editTitle') : t('personalChannels.dialog.addTitle')}
        </h3>
        <p className="mt-1 text-xs leading-5 text-muted-text">{t('personalChannels.dialog.secretNote')}</p>

        <div className="mt-4 space-y-4">
          {submitError ? (
            <PersonalApiError error={submitError} onDismiss={() => setSubmitError(null)} />
          ) : null}
          {isEdit && !channel.readable ? (
            <SettingsAlert variant="warning" title={t('personalChannels.unreadable')} message="" />
          ) : null}

          {isEdit ? (
            <div>
              <p className="mb-2 text-sm font-medium text-foreground">{t('personalChannels.dialog.type')}</p>
              <p className="text-sm text-secondary-text" data-testid="channel-form-type">{type ? typeLabel(type) : channelType}</p>
            </div>
          ) : (
            <Select
              id={`${idPrefix}-type`}
              label={t('personalChannels.dialog.type')}
              value={channelType}
              onChange={handleTypeChange}
              options={typeOptions}
              placeholder={t('personalChannels.dialog.typePlaceholder')}
              disabled={isSaving}
            />
          )}

          <Input
            id={`${idPrefix}-name`}
            label={t('personalChannels.dialog.name')}
            value={name}
            maxLength={64}
            placeholder={t('personalChannels.dialog.namePlaceholder')}
            disabled={isSaving}
            error={fieldIssues.name}
            onChange={(event) => setName(event.target.value)}
          />

          {type?.fields.map((field) => {
            const id = `${idPrefix}-field-${field.name}`;
            const stored = isEdit && isSecretSet(channel.config, field);
            const hint = field.secret && stored ? t('personalChannels.dialog.keepSecretHint') : undefined;
            const label = field.required ? `${fieldLabel(field)} *` : fieldLabel(field);
            if (isMultilineField(field)) {
              return (
                <div key={field.name} className="flex flex-col">
                  <label htmlFor={id} className="mb-2 text-sm font-medium text-foreground">{label}</label>
                  <textarea
                    id={id}
                    name={`channel-${field.name}`}
                    className={TEXTAREA_CLASS}
                    value={values[field.name] ?? ''}
                    disabled={isSaving}
                    aria-invalid={fieldIssues[field.name] ? true : undefined}
                    onChange={(event) => setValue(field.name, event.target.value)}
                  />
                  {fieldIssues[field.name] ? (
                    <p role="alert" className="mt-2 text-xs text-danger">{fieldIssues[field.name]}</p>
                  ) : null}
                </div>
              );
            }
            return (
              <Input
                key={field.name}
                id={id}
                name={`channel-${field.name}`}
                label={label}
                type={field.secret ? 'password' : 'text'}
                allowTogglePassword={field.secret}
                autoComplete="off"
                inputMode={field.kind === 'port' ? 'numeric' : undefined}
                value={values[field.name] ?? ''}
                hint={hint}
                error={fieldIssues[field.name]}
                disabled={isSaving}
                onChange={(event) => setValue(field.name, event.target.value)}
              />
            );
          })}

          <label className="inline-flex cursor-pointer items-center gap-3">
            <input
              type="checkbox"
              checked={enabled}
              disabled={isSaving}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            <span className="text-sm text-secondary-text">{t('personalChannels.dialog.enabled')}</span>
          </label>
        </div>

        <div className="mt-6 flex justify-end gap-3">
          <Button type="button" variant="secondary" size="sm" onClick={onClose} disabled={isSaving}>
            {t('common.cancel')}
          </Button>
          <Button
            type="submit"
            variant="settings-primary"
            size="sm"
            disabled={!type || !dirty}
            isLoading={isSaving}
            loadingText={t('personalChannels.dialog.saving')}
          >
            {t('personalChannels.dialog.save')}
          </Button>
        </div>
      </form>
    </div>
  );
};

/** Add / edit dialog of one personal push channel: per-type fields from the API catalog, inline validation. */
export const ChannelFormDialog: React.FC<ChannelFormDialogProps> = ({ channel, ...rest }) => {
  if (channel === null) return null;
  const form = (
    <ChannelForm
      key={channel === 'new' ? 'new' : `edit-${channel.id}`}
      channel={channel === 'new' ? null : channel}
      {...rest}
    />
  );
  return createPortal(form, document.body);
};
