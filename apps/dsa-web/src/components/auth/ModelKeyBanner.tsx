import type React from 'react';
import { Link } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import { InlineAlert } from '../common/InlineAlert';

/**
 * Entry-point prompt for a signed-in user who has no model of their own (`/me` -> `model_key_configured=false`):
 * analysis, Ask and the plan would be refused with `model_key_required`, so say so up front and link to
 * 个人设置. Renders nothing in local mode and for users that already have a model.
 */
export const ModelKeyBanner: React.FC<{ variant?: 'legacy' | 'plan'; className?: string }> = ({
  variant = 'legacy',
  className,
}) => {
  const { t } = useUiLanguage();
  const { user, modelKeyConfigured } = useCurrentUser();
  if (!user || modelKeyConfigured) {
    return null;
  }

  if (variant === 'plan') {
    return (
      <div className={className ? `notice model-key-notice ${className}` : 'notice model-key-notice'} role="status" data-testid="model-key-banner">
        <div>
          <b>{t('me.modelKeyBanner.title')}</b>
          <span>{t('me.modelKeyBanner.message')}</span>
        </div>
        <Link to="/settings/personal">{t('me.modelKeyBanner.action')}</Link>
      </div>
    );
  }
  return (
    <div data-testid="model-key-banner" className={className}>
      <InlineAlert
        variant="warning"
        title={t('me.modelKeyBanner.title')}
        message={t('me.modelKeyBanner.message')}
        action={(
          <Link className="btn-secondary" to="/settings/personal">
            {t('me.modelKeyBanner.action')}
          </Link>
        )}
        className="rounded-xl px-3 py-2 text-xs shadow-none"
      />
    </div>
  );
};
