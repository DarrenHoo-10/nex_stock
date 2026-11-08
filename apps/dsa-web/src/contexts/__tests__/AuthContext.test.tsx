import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createApiError, createParsedApiError } from '../../api/error';
import { AuthProvider, useAuth } from '../AuthContext';
import { csrfHeadersFor, getUserCenterLoginUrl } from '../../utils/userCenterSession';

const { getStatus, getUserCenterSession, login, changePassword, logout, resetDashboardState } = vi.hoisted(() => ({
  getStatus: vi.fn(),
  getUserCenterSession: vi.fn(),
  login: vi.fn(),
  changePassword: vi.fn(),
  logout: vi.fn(),
  resetDashboardState: vi.fn(),
}));

vi.mock('../../api/auth', () => ({
  authApi: {
    getStatus,
    getUserCenterSession,
    login,
    changePassword,
    logout,
  },
}));

vi.mock('../../stores', () => ({
  useStockPoolStore: {
    getState: () => ({
      resetDashboardState,
    }),
  },
}));

const Probe = () => {
  const auth = useAuth();

  return (
    <div>
      <span data-testid="status">{auth.loggedIn ? 'logged-in' : 'logged-out'}</span>
      <span data-testid="password-set">{auth.passwordSet ? 'set' : 'unset'}</span>
      <button type="button" onClick={() => void auth.login('passwd6', 'passwd6')}>
        trigger-login
      </button>
      <button type="button" onClick={() => void auth.logout()}>
        trigger-logout
      </button>
    </div>
  );
};

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('refreshes auth state after a successful login', async () => {
    getStatus
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: false,
        passwordSet: false,
        passwordChangeable: true,
      })
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: true,
        passwordSet: true,
        passwordChangeable: true,
      });
    login.mockResolvedValue(undefined);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await screen.findByTestId('status');
    fireEvent.click(screen.getByRole('button', { name: 'trigger-login' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('logged-in'));
    expect(screen.getByTestId('password-set')).toHaveTextContent('set');
  });

  it('refreshes auth state after logout', async () => {
    getStatus
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: true,
        passwordSet: true,
        passwordChangeable: true,
      })
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: false,
        passwordSet: true,
        passwordChangeable: true,
        setupState: 'enabled',
      });
    logout.mockResolvedValue(undefined);

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await screen.findByTestId('status');
    fireEvent.click(screen.getByRole('button', { name: 'trigger-logout' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('logged-out'));
    expect(resetDashboardState).toHaveBeenCalled();
  });

  it('does not reset dashboard state when auth is disabled', async () => {
    getStatus.mockResolvedValueOnce({
      authEnabled: false,
      loggedIn: false,
      passwordSet: false,
      passwordChangeable: false,
      setupState: 'no_password',
    });

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await screen.findByTestId('status');
    expect(resetDashboardState).not.toHaveBeenCalled();
  });

  it('treats a 401 logout as already signed out after status refresh', async () => {
    getStatus
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: true,
        passwordSet: true,
        passwordChangeable: true,
        setupState: 'enabled',
      })
      .mockResolvedValueOnce({
        authEnabled: true,
        loggedIn: false,
        passwordSet: true,
        passwordChangeable: true,
        setupState: 'enabled',
      });
    logout.mockRejectedValue(
      createApiError(
        createParsedApiError({
          title: '未登录',
          message: 'Login required',
          rawMessage: 'Login required',
          status: 401,
          category: 'http_error',
        }),
        { response: { status: 401, data: { error: 'unauthorized' } } }
      )
    );

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );

    await screen.findByTestId('status');
    fireEvent.click(screen.getByRole('button', { name: 'trigger-logout' }));

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('logged-out'));
    expect(resetDashboardState).toHaveBeenCalled();
  });

  describe('AUTH_MODE=user_center', () => {
    const LOGIN_URL = 'https://user.example.test/login?return=https%3A%2F%2Fstock.example.test%2F';
    const LOGOUT_URL = 'https://user.example.test/logout?return=https%3A%2F%2Fstock.example.test%2F';
    const userCenterStatus = {
      authMode: 'user_center',
      authEnabled: true,
      loggedIn: false,
      passwordSet: false,
      passwordChangeable: false,
      setupState: 'enabled',
    };
    const loggedOutSession = {
      authenticated: false,
      authorized: false,
      username: null,
      role: null,
      csrfToken: null,
      loginUrl: LOGIN_URL,
      logoutUrl: LOGOUT_URL,
    };

    const UserCenterProbe = () => {
      const auth = useAuth();
      return (
        <div>
          <span data-testid="mode">{auth.authMode}</span>
          <span data-testid="status">{auth.loggedIn ? 'logged-in' : 'logged-out'}</span>
          <span data-testid="authenticated">{String(auth.userCenter?.authenticated)}</span>
          <span data-testid="authorized">{String(auth.userCenter?.authorized)}</span>
          <span data-testid="username">{auth.userCenter?.username ?? ''}</span>
          <button
            type="button"
            onClick={() => {
              void auth.login('passwd6').then((result) => {
                document.title = result.success ? 'login-ok' : 'login-failed:' + result.error?.rawMessage;
              });
            }}
          >
            trigger-login
          </button>
        </div>
      );
    };

    it('uses /api/session to decide and exposes the CSRF token for non-GET requests', async () => {
      getStatus.mockResolvedValueOnce(userCenterStatus);
      getUserCenterSession.mockResolvedValueOnce({
        ...loggedOutSession,
        authenticated: true,
        authorized: true,
        username: 'Darren',
        role: 'admin',
        csrfToken: 'csrf-123',
      });

      render(
        <AuthProvider>
          <UserCenterProbe />
        </AuthProvider>
      );

      await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('logged-in'));
      expect(screen.getByTestId('mode')).toHaveTextContent('user_center');
      expect(screen.getByTestId('username')).toHaveTextContent('Darren');
      expect(csrfHeadersFor('POST')).toEqual({ 'X-CSRF-Token': 'csrf-123' });
      expect(csrfHeadersFor('GET')).toEqual({});
      expect(getUserCenterLoginUrl()).toBe(LOGIN_URL);
      expect(resetDashboardState).not.toHaveBeenCalled();
    });

    it('treats an unauthenticated session as logged out and keeps the login URL', async () => {
      getStatus.mockResolvedValueOnce(userCenterStatus);
      getUserCenterSession.mockResolvedValueOnce(loggedOutSession);

      render(
        <AuthProvider>
          <UserCenterProbe />
        </AuthProvider>
      );

      await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('false'));
      expect(screen.getByTestId('status')).toHaveTextContent('logged-out');
      expect(csrfHeadersFor('POST')).toEqual({});
      expect(getUserCenterLoginUrl()).toBe(LOGIN_URL);
      expect(resetDashboardState).toHaveBeenCalled();
    });

    it('does not authorize a signed-in non-admin and withholds the CSRF token', async () => {
      getStatus.mockResolvedValueOnce(userCenterStatus);
      getUserCenterSession.mockResolvedValueOnce({
        ...loggedOutSession,
        authenticated: true,
        username: 'Alice',
        role: 'user',
      });

      render(
        <AuthProvider>
          <UserCenterProbe />
        </AuthProvider>
      );

      await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('true'));
      expect(screen.getByTestId('authorized')).toHaveTextContent('false');
      expect(screen.getByTestId('status')).toHaveTextContent('logged-out');
      expect(csrfHeadersFor('POST')).toEqual({});
    });

    it('never calls the disabled local password login', async () => {
      getStatus.mockResolvedValueOnce(userCenterStatus);
      getUserCenterSession.mockResolvedValueOnce(loggedOutSession);

      render(
        <AuthProvider>
          <UserCenterProbe />
        </AuthProvider>
      );

      await waitFor(() => expect(screen.getByTestId('authenticated')).toHaveTextContent('false'));
      fireEvent.click(screen.getByRole('button', { name: 'trigger-login' }));
      await waitFor(() => expect(document.title).toBe('login-failed:auth_mode_user_center'));
      expect(login).not.toHaveBeenCalled();
    });

    it('clears the CSRF token when the backend is in local mode', async () => {
      getStatus.mockResolvedValueOnce({
        authEnabled: false,
        loggedIn: false,
        passwordSet: false,
        passwordChangeable: false,
        setupState: 'no_password',
        authMode: 'local',
      });

      render(
        <AuthProvider>
          <UserCenterProbe />
        </AuthProvider>
      );

      await waitFor(() => expect(screen.getByTestId('mode')).toHaveTextContent('local'));
      expect(getUserCenterSession).not.toHaveBeenCalled();
      expect(csrfHeadersFor('POST')).toEqual({});
      expect(getUserCenterLoginUrl()).toBeNull();
    });
  });
});
