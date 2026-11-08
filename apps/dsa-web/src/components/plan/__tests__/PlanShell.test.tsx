import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { usePlanUiStore } from '../../../stores/planUiStore';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { PlanPageHeader } from '../PlanPageHeader';
import { PlanShell } from '../PlanShell';

const { getLatestRun, isPlanServiceUnavailable } = vi.hoisted(() => ({
  getLatestRun: vi.fn(),
  isPlanServiceUnavailable: vi.fn(),
}));

vi.mock('../../../api/dailyPlan', () => ({ getLatestRun, isPlanServiceUnavailable }));

function renderShell(path = '/mine') {
  return render(
    <UiLanguageProvider>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<PlanShell />}>
            <Route path="/mine" element={<PlanPageHeader title="我的股票" />} />
            <Route path="/boards" element={<h1>板块异动页</h1>} />
            <Route path="/home" element={<h1>旧首页</h1>} />
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

  it('lists the old pages in the more popover and closes on Escape', async () => {
    getLatestRun.mockResolvedValue(null);
    renderShell('/mine');
    const more = screen.getByRole('button', { name: '更多' });
    expect(more).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(more);
    const popover = await screen.findByRole('navigation', { name: '更多页面' });
    const hrefs = Array.from(popover.querySelectorAll('a')).map((link) => link.getAttribute('href'));
    expect(hrefs).toEqual([
      '/home',
      '/chat',
      '/portfolio',
      '/decision-signals',
      '/screening',
      '/backtest',
      '/alerts',
      '/data',
      '/usage',
      '/settings',
    ]);
    expect(more).toHaveAttribute('aria-expanded', 'true');

    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('navigation', { name: '更多页面' })).not.toBeInTheDocument());
    expect(more).toHaveFocus();
  });

  it('closes the popover after navigating to an old page', async () => {
    getLatestRun.mockResolvedValue(null);
    renderShell('/mine');
    fireEvent.click(screen.getByRole('button', { name: '更多' }));
    fireEvent.click(await screen.findByRole('link', { name: '首页' }));
    expect(await screen.findByRole('heading', { name: '旧首页' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('navigation', { name: '更多页面' })).not.toBeInTheDocument());
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
