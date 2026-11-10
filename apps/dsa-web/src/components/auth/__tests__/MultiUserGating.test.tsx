import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { adminUser, regularUser } from '../../../test-utils/currentUserFixtures';
import type { CurrentUser } from '../../../types/me';
import { SidebarNav } from '../../layout/SidebarNav';
import { AdminOnly } from '../AdminOnly';
import { ModelKeyBanner } from '../ModelKeyBanner';

vi.mock('../../theme/ThemeToggle', () => ({
  ThemeToggle: () => <button type="button">切换主题</button>,
}));

function renderWithUser(ui: React.ReactElement, user: CurrentUser | null, path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <UiLanguageProvider>
        <CurrentUserProvider user={user}>{ui}</CurrentUserProvider>
      </UiLanguageProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
});

describe('AdminOnly', () => {
  it('renders the page for an administrator', () => {
    renderWithUser(<AdminOnly><div>screening page</div></AdminOnly>, adminUser);

    expect(screen.getByText('screening page')).toBeInTheDocument();
    expect(screen.queryByTestId('admin-required-notice')).toBeNull();
  });

  it('renders the page in local mode (the single user is the administrator)', () => {
    renderWithUser(<AdminOnly><div>screening page</div></AdminOnly>, null);

    expect(screen.getByText('screening page')).toBeInTheDocument();
  });

  it('replaces the page with a clear administrator-permission notice for a regular user', () => {
    renderWithUser(<AdminOnly><div>screening page</div></AdminOnly>, regularUser);

    expect(screen.queryByText('screening page')).toBeNull();
    expect(screen.getByRole('heading', { name: '需要管理员权限' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '前往个人设置' })).toHaveAttribute('href', '/settings/personal');
  });

  it('uses the plan styling inside the plan shell', () => {
    const { container } = renderWithUser(<AdminOnly variant="plan"><div>picks</div></AdminOnly>, regularUser);

    expect(container.querySelector('section.win.empty')).not.toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('需要管理员权限');
  });
});

describe('ModelKeyBanner', () => {
  it('prompts a user without a model key and links to the personal settings', () => {
    renderWithUser(<ModelKeyBanner />, { ...regularUser, modelKeyConfigured: false });

    const banner = screen.getByTestId('model-key-banner');
    expect(banner).toHaveTextContent('尚未配置你自己的模型 Key');
    expect(screen.getByRole('link', { name: '去个人设置' })).toHaveAttribute('href', '/settings/personal');
  });

  it('has a plan-shell variant', () => {
    const { container } = renderWithUser(<ModelKeyBanner variant="plan" />, { ...regularUser, modelKeyConfigured: false });

    expect(container.querySelector('.notice.model-key-notice')).not.toBeNull();
  });

  it('stays hidden once the model key is configured', () => {
    renderWithUser(<ModelKeyBanner />, regularUser);

    expect(screen.queryByTestId('model-key-banner')).toBeNull();
  });

  it('never shows in local mode', () => {
    renderWithUser(<ModelKeyBanner />, null);

    expect(screen.queryByTestId('model-key-banner')).toBeNull();
  });
});

describe('SidebarNav admin gating', () => {
  const linkTargets = () => screen.getAllByRole('link').map((link) => link.getAttribute('href'));

  it('hides screening, picks and system settings from a regular user and offers the personal settings', () => {
    renderWithUser(<SidebarNav />, regularUser);

    const hrefs = linkTargets();
    expect(hrefs).not.toContain('/screening');
    expect(hrefs).not.toContain('/picks');
    expect(hrefs).not.toContain('/settings');
    expect(hrefs).toContain('/settings/personal');
    // Everything that works on the user's own data stays.
    expect(hrefs).toEqual(expect.arrayContaining(['/home', '/chat', '/portfolio', '/usage', '/alerts', '/backtest', '/data']));
  });

  it('shows the signed-in user and role in the sidebar', () => {
    renderWithUser(<SidebarNav />, regularUser);

    const badge = screen.getByTestId('current-user-badge');
    expect(badge).toHaveTextContent('Alice');
    expect(badge).toHaveTextContent('普通用户');
  });

  it('keeps every entry for an administrator and adds the personal settings', () => {
    renderWithUser(<SidebarNav />, adminUser);

    const hrefs = linkTargets();
    expect(hrefs).toEqual(expect.arrayContaining(['/screening', '/picks', '/settings', '/settings/personal']));
    expect(screen.getByTestId('current-user-badge')).toHaveTextContent('管理员');
  });

  it('is exactly the single-user navigation in local mode', () => {
    renderWithUser(<SidebarNav />, null);

    const hrefs = linkTargets();
    expect(hrefs).toEqual([
      '/boards', '/picks', '/mine', '/home', '/chat', '/screening', '/portfolio',
      '/decision-signals', '/backtest', '/alerts', '/data', '/usage', '/settings',
    ]);
    expect(screen.queryByTestId('current-user-badge')).toBeNull();
  });

  it('marks only the system settings entry active on /settings', () => {
    renderWithUser(<SidebarNav />, adminUser, '/settings/personal');

    expect(screen.getByRole('link', { name: '个人设置' })).toHaveClass('font-medium');
    expect(screen.getByRole('link', { name: '设置' })).not.toHaveClass('font-medium');
  });
});

describe('route guard pattern', () => {
  it('lets AdminOnly sit directly in a route element', () => {
    render(
      <MemoryRouter initialEntries={['/screening']}>
        <UiLanguageProvider>
          <CurrentUserProvider user={regularUser}>
            <Routes>
              <Route path="/screening" element={<AdminOnly><div>run screening</div></AdminOnly>} />
            </Routes>
          </CurrentUserProvider>
        </UiLanguageProvider>
      </MemoryRouter>,
    );

    expect(screen.queryByText('run screening')).toBeNull();
    expect(screen.getByTestId('admin-required-notice')).toBeInTheDocument();
  });
});
