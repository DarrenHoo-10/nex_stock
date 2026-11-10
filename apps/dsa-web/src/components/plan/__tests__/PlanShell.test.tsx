import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../../test-utils/currentUser';
import { adminUser, regularUser } from '../../../test-utils/currentUserFixtures';
import type { CurrentUser } from '../../../types/me';
import { usePlanUiStore } from '../../../stores/planUiStore';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { PlanPageHeader } from '../PlanPageHeader';
import { PlanShell } from '../PlanShell';

const { getLatestRun, isPlanServiceUnavailable, authState } = vi.hoisted(() => ({
  getLatestRun: vi.fn(),
  isPlanServiceUnavailable: vi.fn(),
  authState: { value: { authMode: 'local', userCenter: null } as Record<string, unknown> },
}));

vi.mock('../../../api/dailyPlan', () => ({ getLatestRun, isPlanServiceUnavailable }));
vi.mock('../../../contexts/AuthContext', () => ({ useAuth: () => authState.value }));

function renderShell(path = '/mine') {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<PlanShell />}>
            <Route path="/mine" element={<PlanPageHeader title="我的股票" />} />
            <Route path="/boards" element={<h1>板块异动页</h1>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </UiLanguageProvider>,
  );
}

beforeEach(() => {
  getLatestRun.mockReset();
  isPlanServiceUnavailable.mockReset();
  isPlanServiceUnavailable.mockReturnValue(false);
  usePlanUiStore.setState({ assistantOpen: false, runDialogOpen: false, runRefreshToken: 0 });
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  authState.value = { authMode: 'local', userCenter: null };
});

describe('PlanShell', () => {
  it('renders the ornament nav with aria-current, a focusable main landmark and a skip link', async () => {
    getLatestRun.mockResolvedValue(null);
    renderShell('/mine');

    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '我的股票' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: '板块异动' })).not.toHaveAttribute('aria-current');
    expect(screen.getByRole('link', { name: '设置' })).toHaveAttribute('href', '/preferences');
    const main = screen.getByRole('main');
    expect(main).toHaveAttribute('id', 'main');
    expect(main).toHaveAttribute('tabindex', '-1');
    expect(screen.getByRole('link', { name: '跳到主要内容' })).toHaveAttribute('href', '#main');
    await screen.findByText('还没有运行记录');
  });

  it('has no links to the old pages', async () => {
    getLatestRun.mockResolvedValue(null);
    renderShell('/mine');
    expect(screen.queryByRole('button', { name: '更多' })).not.toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: '主导航' });
    const hrefs = Array.from(nav.querySelectorAll('a')).map((link) => link.getAttribute('href'));
    expect(hrefs).toEqual(['/boards', '/picks', '/mine', '/assistant', '/preferences']);
    await screen.findByText('还没有运行记录');
  });

  it('offers a user-center logout link in the nav only in user_center mode', async () => {
    getLatestRun.mockResolvedValue(null);
    authState.value = {
      authMode: 'user_center',
      userCenter: { logoutUrl: 'https://user.example.test/logout?return=https%3A%2F%2Fstock.example.test%2F' },
    };
    renderShell('/mine');
    expect(screen.getByRole('link', { name: '退出' })).toHaveAttribute(
      'href',
      'https://user.example.test/logout?return=https%3A%2F%2Fstock.example.test%2F',
    );
    await screen.findByText('还没有运行记录');
  });
});

describe('RunStatusCapsule / PlanPageHeader', () => {
  it('shows date and status for the latest run and opens the run dialog flag on click', async () => {
    getLatestRun.mockResolvedValue({ id: 'r1', tradeDate: '2026-11-06', status: 'succeeded' });
    renderShell('/mine');
    const capsule = await screen.findByRole('button', { name: /11-06 收盘计划已生成/ });
    expect(capsule).toHaveTextContent('11-06');
    fireEvent.click(capsule);
    expect(usePlanUiStore.getState().runDialogOpen).toBe(true);
  });

  it('shows the not-ready text on 503 daily_plan_unavailable', async () => {
    getLatestRun.mockRejectedValue(new Error('unavailable'));
    isPlanServiceUnavailable.mockReturnValue(true);
    renderShell('/mine');
    expect(await screen.findByText('次日计划服务未就绪')).toBeInTheDocument();
  });

  it('opens the assistant flag from the white button', async () => {
    getLatestRun.mockResolvedValue(null);
    renderShell('/mine');
    const button = screen.getByRole('button', { name: '助手' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(button);
    expect(usePlanUiStore.getState().assistantOpen).toBe(true);
    await screen.findByText('还没有运行记录');
  });
});

describe('PlanShell in a multi-user deployment', () => {
  function renderAs(user: CurrentUser) {
    return render(
      <UiLanguageProvider>
        <CurrentUserProvider user={user}>
          <MemoryRouter initialEntries={['/mine']}>
            <Routes>
              <Route element={<PlanShell />}>
                <Route path="/mine" element={<PlanPageHeader title="我的股票" />} />
              </Route>
            </Routes>
          </MemoryRouter>
        </CurrentUserProvider>
      </UiLanguageProvider>,
    );
  }
  const hrefs = () =>
    Array.from(screen.getByRole('navigation', { name: '主导航' }).querySelectorAll('a')).map((link) =>
      link.getAttribute('href'),
    );

  it('shows who is signed in and hides the screening run page from a regular user', async () => {
    getLatestRun.mockResolvedValue(null);
    renderAs(regularUser);

    expect(screen.getByTestId('current-user-badge')).toHaveTextContent('Alice');
    expect(screen.getByTestId('current-user-badge')).toHaveTextContent('普通用户');
    // 选股 (screening run) is administrator-only; settings become the personal settings.
    expect(hrefs()).toEqual(['/boards', '/mine', '/assistant', '/settings/personal']);
    await screen.findByText('还没有运行记录');
  });

  it('keeps the full navigation and the system preferences for an administrator', async () => {
    getLatestRun.mockResolvedValue(null);
    renderAs(adminUser);

    expect(screen.getByTestId('current-user-badge')).toHaveTextContent('管理员');
    expect(hrefs()).toEqual(['/boards', '/picks', '/mine', '/assistant', '/preferences']);
    await screen.findByText('还没有运行记录');
  });
});
