import type React from 'react';
import { Link, useInRouterContext } from 'react-router-dom';
import type { ParsedApiError } from '../../api/error';
import {
  getMultiUserErrorCode,
  MODEL_KEY_REQUIRED_CODE,
  MULTI_USER_ERROR_TEXT,
} from '../../api/multiUserErrors';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

interface ApiErrorAlertProps {
  error: ParsedApiError;
  className?: string;
  actionLabel?: string;
  onAction?: () => void;
  dismissLabel?: string;
  onDismiss?: () => void;
}

/** Multi-user errors the user fixes in their personal settings (own model key / endpoint). */
const PERSONAL_SETTINGS_ERROR_CODES = new Set<string>([MODEL_KEY_REQUIRED_CODE, 'model_route_not_allowed', 'unsafe_url']);
const PERSONAL_SETTINGS_PATH = '/settings/personal';

const ACTION_CLASS_NAME = 'mt-3 inline-flex items-center justify-center rounded-md border border-[hsl(var(--color-danger-alert-border)/0.3)] bg-[hsl(var(--color-danger-alert-bg)/0.1)] px-3 py-1.5 text-xs font-medium text-[hsl(var(--color-danger-alert-text))] transition hover:bg-[hsl(var(--color-danger-alert-bg)/0.15)]';

export const ApiErrorAlert: React.FC<ApiErrorAlertProps> = ({
  error,
  className = '',
  actionLabel,
  onAction,
  dismissLabel,
  onDismiss,
}) => {
  const { t } = useUiLanguage();
  const inRouter = useInRouterContext();
  // Multi-user contract errors (admin guard, model key, personal-settings validation) get a translated message.
  const multiUserCode = getMultiUserErrorCode(error);
  const multiUserText = multiUserCode ? MULTI_USER_ERROR_TEXT[multiUserCode] : null;
  const title = multiUserText ? t(multiUserText.title) : error.title;
  const message = multiUserText ? t(multiUserText.message) : error.message;
  const showDetails = error.rawMessage.trim() && error.rawMessage.trim() !== message.trim();
  const showPersonalSettingsLink = multiUserCode !== null && PERSONAL_SETTINGS_ERROR_CODES.has(multiUserCode);

  return (
    <div
      className={`rounded-xl border border-[hsl(var(--color-danger-alert-border)/0.3)] bg-[hsl(var(--color-danger-alert-bg)/0.1)] px-4 py-3 text-[hsl(var(--color-danger-alert-text))] ${className}`}
      role="alert"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold">{title}</p>
          <p className="mt-1 whitespace-pre-line break-words text-xs opacity-90">{message}</p>
        </div>
        {onDismiss ? (
          <button
            type="button"
            className="shrink-0 rounded-md border border-[hsl(var(--color-danger-alert-border)/0.3)] bg-[hsl(var(--color-danger-alert-bg)/0.1)] px-2 py-1 text-[11px] text-[hsl(var(--color-danger-alert-text))] transition hover:bg-[hsl(var(--color-danger-alert-bg)/0.15)]"
            onClick={onDismiss}
          >
            {dismissLabel ?? t('common.close')}
          </button>
        ) : null}
      </div>
      {showDetails ? (
        <details className="mt-3 rounded-lg border border-subtle bg-surface-2 px-3 py-2">
          <summary className="cursor-pointer text-xs text-[hsl(var(--color-danger-alert-text))] opacity-90">{t('common.details')}</summary>
          <pre className="mt-2 whitespace-pre-wrap break-words text-[11px] leading-5 text-[hsl(var(--color-danger-alert-text))] opacity-85">
            {error.rawMessage}
          </pre>
        </details>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        {actionLabel && onAction ? (
          <button type="button" className={ACTION_CLASS_NAME} onClick={onAction}>
            {actionLabel}
          </button>
        ) : null}
        {showPersonalSettingsLink ? (
          inRouter ? (
            <Link className={ACTION_CLASS_NAME} to={PERSONAL_SETTINGS_PATH}>
              {t('me.error.goPersonalSettings')}
            </Link>
          ) : (
            <a className={ACTION_CLASS_NAME} href={PERSONAL_SETTINGS_PATH}>
              {t('me.error.goPersonalSettings')}
            </a>
          )
        ) : null}
      </div>
    </div>
  );
};
