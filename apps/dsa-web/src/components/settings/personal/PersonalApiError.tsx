import type React from 'react';
import { getParsedApiError } from '../../../api/error';
import { CHANNEL_ERROR_TEXT, getApiErrorCode, isChannelErrorCode } from '../../../api/multiUserErrors';
import { useUiLanguage } from '../../../contexts/UiLanguageContext';
import { ApiErrorAlert } from '../../common';
import { SettingsAlert } from '../SettingsAlert';

interface PersonalApiErrorProps {
  error: unknown;
  className?: string;
  /** Text of the primary action (for example a retry), shown on the generic alert. */
  actionLabel?: string;
  onAction?: () => void;
  onDismiss?: () => void;
}

/**
 * An API failure of the personal channel calls: the channel codes (`unsafe_target`, `channel_limit_reached`,
 * `rate_limited` ...) get a translated, actionable message; everything else falls back to the shared alert
 * (which also translates the P2 multi-user codes such as `primary_user_uses_env`).
 */
export const PersonalApiError: React.FC<PersonalApiErrorProps> = ({
  error,
  className,
  actionLabel,
  onAction,
  onDismiss,
}) => {
  const { t } = useUiLanguage();
  const code = getApiErrorCode(error);
  if (isChannelErrorCode(code)) {
    const text = CHANNEL_ERROR_TEXT[code];
    return (
      <SettingsAlert
        className={className}
        variant="error"
        title={t(text.title)}
        message={t(text.message)}
        actionLabel={actionLabel}
        onAction={onAction}
      />
    );
  }
  return (
    <ApiErrorAlert
      className={className}
      error={getParsedApiError(error)}
      actionLabel={actionLabel}
      onAction={onAction}
      onDismiss={onDismiss}
    />
  );
};
