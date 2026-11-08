import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import { UI_LANGUAGE_STORAGE_KEY } from '../../../utils/uiLanguage';
import { UserCenterForbidden, UserCenterLoginRedirect } from '../UserCenterGate';

const { redirectToUserCenter } = vi.hoisted(() => ({ redirectToUserCenter: vi.fn() }));

vi.mock('../../../utils/userCenterSession', () => ({ redirectToUserCenter }));

const LOGIN_URL = 'https://user.example.test/login?return=https%3A%2F%2Fstock.example.test%2F';
const LOGOUT_URL = 'https://user.example.test/logout?return=https%3A%2F%2Fstock.example.test%2F';

beforeEach(() => {
  redirectToUserCenter.mockReset();
  window.sessionStorage.clear();
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
});

describe('UserCenterLoginRedirect', () => {
  it('sends the browser to the user-center login URL', () => {
    render(
      <UiLanguageProvider>
        <UserCenterLoginRedirect loginUrl={LOGIN_URL} />
      </UiLanguageProvider>,
    );

    expect(redirectToUserCenter).toHaveBeenCalledTimes(1);
    expect(redirectToUserCenter).toHaveBeenCalledWith(LOGIN_URL);
    expect(screen.getByRole('link', { name: '前往用户中心登录' })).toHaveAttribute('href', LOGIN_URL);
  });

  it('stops auto-redirecting when a redirect just happened (login loop guard)', () => {
    window.sessionStorage.setItem('dsa.userCenter.loginRedirectAt', String(Date.now()));

    render(
      <UiLanguageProvider>
        <UserCenterLoginRedirect loginUrl={LOGIN_URL} />
      </UiLanguageProvider>,
    );

    expect(redirectToUserCenter).not.toHaveBeenCalled();
    expect(screen.getByText(/登录后仍未获得有效会话/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '前往用户中心登录' })).toHaveAttribute('href', LOGIN_URL);
  });
});

describe('UserCenterForbidden', () => {
  it('shows the no-permission page with a logout link for non-admin sessions', () => {
    render(
      <UiLanguageProvider>
        <UserCenterForbidden
          session={{
            authenticated: true,
            authorized: false,
            username: 'Alice',
            role: 'user',
            loginUrl: LOGIN_URL,
            logoutUrl: LOGOUT_URL,
          }}
        />
      </UiLanguageProvider>,
    );

    expect(screen.getByRole('heading', { name: '无权限' })).toBeInTheDocument();
    expect(screen.getByText(/Alice/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '退出登录' })).toHaveAttribute('href', LOGOUT_URL);
  });
});
