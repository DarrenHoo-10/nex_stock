import type React from 'react';
import { NavLink } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { useCurrentUser } from '../../hooks/useCurrentUser';
import { cn } from '../../utils/cn';

const TABS = [
  { to: '/settings', labelKey: 'layout.nav.systemSettings' as const, end: true },
  { to: '/settings/personal', labelKey: 'layout.nav.personalSettings' as const, end: true },
];

/**
 * 系统设置 / 个人设置 switch (docs/design/multi-user.md 3.6). Only an administrator in a multi-user deployment
 * has both pages; local mode keeps the single settings page exactly as before, regular users only have 个人设置.
 */
export const SettingsScopeTabs: React.FC<{ className?: string }> = ({ className }) => {
  const { t } = useUiLanguage();
  const { isAdmin, isMultiUser } = useCurrentUser();
  if (!isAdmin || !isMultiUser) {
    return null;
  }
  return (
    <nav
      className={cn('mb-4 inline-flex rounded-xl border settings-border bg-card/70 p-1', className)}
      aria-label={t('layout.nav.accountSettings')}
      data-testid="settings-scope-tabs"
    >
      {TABS.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) =>
            cn(
              'rounded-lg px-3 py-1.5 text-sm transition-colors',
              isActive
                ? 'bg-cyan text-background shadow-soft-card'
                : 'text-secondary-text hover:bg-hover hover:text-foreground',
            )
          }
        >
          {t(tab.labelKey)}
        </NavLink>
      ))}
    </nav>
  );
};
