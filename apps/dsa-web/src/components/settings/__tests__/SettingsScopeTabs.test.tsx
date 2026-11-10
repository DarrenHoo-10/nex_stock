import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { adminUser, regularUser } from '../../../test-utils/currentUserFixtures';
import type { CurrentUser } from '../../../types/me';
import { SettingsScopeTabs } from '../SettingsScopeTabs';

function renderTabs(user: CurrentUser | null, path = '/settings') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <UiLanguageProvider>
        <CurrentUserProvider user={user}>
          <SettingsScopeTabs />
        </CurrentUserProvider>
      </UiLanguageProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
});

describe('SettingsScopeTabs', () => {
  it('switches between system and personal settings for an administrator', () => {
    renderTabs(adminUser, '/settings/personal');

    expect(screen.getByRole('link', { name: '系统设置' })).toHaveAttribute('href', '/settings');
    expect(screen.getByRole('link', { name: '个人设置' })).toHaveAttribute('href', '/settings/personal');
    expect(screen.getByRole('link', { name: '个人设置' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: '系统设置' })).not.toHaveAttribute('aria-current');
  });

  it('is not shown to a regular user (they only have the personal settings)', () => {
    renderTabs(regularUser);

    expect(screen.queryByTestId('settings-scope-tabs')).toBeNull();
  });

  it('is not shown in local mode, where the settings page stays as it was', () => {
    renderTabs(null);

    expect(screen.queryByTestId('settings-scope-tabs')).toBeNull();
  });
});
