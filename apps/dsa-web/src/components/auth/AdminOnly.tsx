import type React from 'react';
import { Link } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import { EmptyState } from '../common/EmptyState';

type Variant = 'legacy' | 'plan';

/**
 * Shown instead of a page whose purpose is an administrator-only endpoint (system settings, screening run ...).
 * A regular user reaching it by URL gets this explanation rather than a wall of 403 errors.
 */
export const AdminRequiredNotice: React.FC<{ variant?: Variant; className?: string }> = ({
  variant = 'legacy',
  className,
}) => {
  const { t } = useUiLanguage();
  const title = t('me.error.adminRequired.title');
  const description = t('me.adminRequiredPage.hint');
  const action = (
    <Link className={variant === 'plan' ? 'btn' : 'btn-secondary'} to="/settings/personal">
      {t('me.adminRequiredPage.action')}
    </Link>
  );

  if (variant === 'plan') {
    return (
      <section className="win empty" role="alert" aria-labelledby="admin-required-title">
        <h2 id="admin-required-title">{title}</h2>
        <p>{description}</p>
        <div className="actions">{action}</div>
      </section>
    );
  }
  return (
    <div className="flex min-h-[50vh] items-center justify-center px-4" data-testid="admin-required-notice">
      <EmptyState title={title} description={description} action={action} className={className ?? 'max-w-xl'} />
    </div>
  );
};

/** Renders `children` for administrators (and in local mode); everyone else sees {@link AdminRequiredNotice}. */
export const AdminOnly: React.FC<{ children: React.ReactNode; variant?: Variant }> = ({ children, variant }) => {
  const { isAdmin } = useCurrentUser();
  return isAdmin ? <>{children}</> : <AdminRequiredNotice variant={variant} />;
};
