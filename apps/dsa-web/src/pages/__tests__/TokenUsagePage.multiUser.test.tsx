import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../contexts/UiLanguageContext';
import { CurrentUserProvider } from '../../test-utils/currentUser';
import { adminUser, regularUser } from '../../test-utils/currentUserFixtures';
import type { CurrentUser } from '../../types/me';
import TokenUsagePage from '../TokenUsagePage';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('../../api/index', () => ({
  default: { get },
}));

function dashboard(totalTokens: number) {
  return {
    period: 'month',
    from_date: '2026-11-01',
    to_date: '2026-11-09',
    total_calls: 1,
    total_prompt_tokens: 1,
    total_completion_tokens: totalTokens - 1,
    total_tokens: totalTokens,
    by_call_type: [{ call_type: 'analysis', calls: 1, prompt_tokens: 1, completion_tokens: totalTokens - 1, total_tokens: totalTokens }],
    by_model: [{ model: 'openai/gpt-test', calls: 1, prompt_tokens: 1, completion_tokens: totalTokens - 1, total_tokens: totalTokens, max_total_tokens: totalTokens }],
    recent_calls: [],
  };
}

function renderPage(user: CurrentUser | null) {
  return render(
    <UiLanguageProvider>
      <CurrentUserProvider user={user}>
        <TokenUsagePage />
      </CurrentUserProvider>
    </UiLanguageProvider>,
  );
}

const endpointsCalled = () => get.mock.calls.map(([url]) => url as string);

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  get.mockImplementation((url: string) =>
    Promise.resolve({ data: url === '/api/v1/me/usage' ? dashboard(111) : dashboard(999) }),
  );
});

describe('TokenUsagePage by role', () => {
  it('shows a regular user their own usage from /me/usage and never calls the admin-only endpoint', async () => {
    renderPage(regularUser);

    expect((await screen.findAllByText('111')).length).toBeGreaterThan(0);
    expect(endpointsCalled()).toEqual(['/api/v1/me/usage']);
    expect(get).toHaveBeenCalledWith('/api/v1/me/usage', { params: { period: 'month', limit: 50 } });
    // No scope switch for someone who only has one view.
    expect(screen.queryByRole('group', { name: '用量范围' })).toBeNull();
    expect(screen.getByText(/查看你自己的 LLM 调用次数/)).toBeInTheDocument();
  });

  it('keeps the global view for an administrator and can switch to their own usage', async () => {
    renderPage(adminUser);

    expect((await screen.findAllByText('999')).length).toBeGreaterThan(0);
    expect(endpointsCalled()).toEqual(['/api/v1/usage/dashboard']);

    fireEvent.click(screen.getByRole('button', { name: '我的用量' }));

    expect((await screen.findAllByText('111')).length).toBeGreaterThan(0);
    expect(endpointsCalled()).toEqual(['/api/v1/usage/dashboard', '/api/v1/me/usage']);

    fireEvent.click(screen.getByRole('button', { name: '全站用量' }));
    await waitFor(() => expect(endpointsCalled().at(-1)).toBe('/api/v1/usage/dashboard'));
    expect((await screen.findAllByText('999')).length).toBeGreaterThan(0);
  });

  it('keeps the period selector working against the own endpoint', async () => {
    renderPage(regularUser);
    await screen.findAllByText('111');

    fireEvent.click(screen.getByRole('button', { name: '今日' }));

    await waitFor(() =>
      expect(get).toHaveBeenLastCalledWith('/api/v1/me/usage', { params: { period: 'today', limit: 50 } }),
    );
  });

  it('is unchanged in local mode: the global dashboard and no scope switch', async () => {
    renderPage(null);

    expect((await screen.findAllByText('999')).length).toBeGreaterThan(0);
    expect(endpointsCalled()).toEqual(['/api/v1/usage/dashboard']);
    expect(screen.queryByRole('group', { name: '用量范围' })).toBeNull();
    expect(screen.queryByRole('button', { name: '我的用量' })).toBeNull();
  });
});
