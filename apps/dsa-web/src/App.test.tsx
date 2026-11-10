import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import * as AuthContext from './contexts/AuthContext';
import { UI_LANGUAGE_STORAGE_KEY } from './utils/uiLanguage';

type AuthState = ReturnType<typeof AuthContext.useAuth>;

const { chatPageShouldThrow, setCurrentRoute, useAgentChatStoreMock } = vi.hoisted(() => {
  const setCurrentRoute = vi.fn();
  const chatPageShouldThrow = { value: false };
  const state = { completionBadge: false };
  const useAgentChatStoreMock = Object.assign(
    vi.fn((selector?: (value: typeof state) => unknown) => (selector ? selector(state) : state)),
    { getState: () => ({ setCurrentRoute }) },
  );
  return { chatPageShouldThrow, setCurrentRoute, useAgentChatStoreMock };
});

const { currentUserState } = vi.hoisted(() => ({
  currentUserState: { isAdmin: true },
}));

vi.mock('./hooks/useCurrentUser', () => ({
  useCurrentUser: () => ({
    user: null,
    isAdmin: currentUserState.isAdmin,
    modelKeyConfigured: true,
    displayName: '',
    isMultiUser: !currentUserState.isAdmin,
    refresh: async () => undefined,
  }),
}));

vi.mock('./pages/PersonalSettingsPage', () => ({
  default: () => <div data-testid="personal-settings-page">Personal settings</div>,
}));

vi.mock('./contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: vi.fn(),
}));

vi.mock('./stores/agentChatStore', () => ({
  useAgentChatStore: useAgentChatStoreMock,
}));

vi.mock('./pages/HomePage', () => ({
  default: () => <div data-testid="home-page">Home</div>,
}));

vi.mock('./pages/plan/BoardsPage', () => ({
  default: () => <div data-testid="boards-page">Boards</div>,
}));

vi.mock('./pages/plan/PicksPage', () => ({
  default: () => <div data-testid="picks-page">Picks</div>,
}));

vi.mock('./pages/plan/MinePage', () => ({
  default: () => <div data-testid="mine-page">Mine</div>,
}));

vi.mock('./pages/plan/PreferencesPage', () => ({
  default: () => <div data-testid="preferences-page">Preferences</div>,
}));

vi.mock('./pages/ChatPage', () => ({
  default: () => {
    if (chatPageShouldThrow.value) {
      throw new Error('chunk load failed');
    }
    return <div data-testid="chat-page">Chat</div>;
  },
}));

vi.mock('./pages/PortfolioPage', () => ({
  default: () => <div data-testid="portfolio-page">Portfolio</div>,
}));

vi.mock('./pages/DecisionSignalsPage', () => ({
  default: () => <div data-testid="decision-signals-page">Decision signals</div>,
}));

vi.mock('./pages/BacktestPage', () => ({
  default: () => <div data-testid="backtest-page">Backtest</div>,
}));

vi.mock('./pages/AlertsPage', () => ({
  default: () => <div data-testid="alerts-page">Alerts</div>,
}));

vi.mock('./pages/TokenUsagePage', () => ({
  default: () => <div data-testid="token-usage-page">Usage</div>,
}));

vi.mock('./pages/SettingsPage', () => ({
  default: () => <div data-testid="settings-page">Settings</div>,
}));

vi.mock('./pages/DataCenterPage', () => ({
  default: () => <div data-testid="data-center-page">Data center</div>,
}));

vi.mock('./pages/NotFoundPage', () => ({
  default: () => <div data-testid="not-found-page">Not Found</div>,
}));

vi.mock('./pages/LoginPage', () => ({
  default: () => <div data-testid="login-page">Login</div>,
}));

function makeAuthState(overrides: Partial<AuthState> = {}): AuthState {
  return {
    authMode: 'local',
    userCenter: null,
    currentUser: null,
    authEnabled: false,
    loggedIn: false,
    passwordSet: false,
    passwordChangeable: false,
    setupState: 'no_password',
    isLoading: false,
    loadError: null,
    login: vi.fn().mockResolvedValue({ success: true }),
    changePassword: vi.fn().mockResolvedValue({ success: true }),
    logout: vi.fn().mockResolvedValue(undefined),
    refreshStatus: vi.fn().mockResolvedValue(undefined),
    refreshCurrentUser: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  chatPageShouldThrow.value = false;
  currentUserState.isAdmin = true;
  window.history.pushState({}, '', '/');
  localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');
  vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState());
});

describe('App routing behavior', () => {
  it('shows loading fallback while auth status is initializing', () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({ isLoading: true }));

    const { container } = render(<App />);

    expect(container.querySelector('.border-t-cyan')).toBeInTheDocument();
  });

  it('redirects protected routes to login when auth is enabled but user is not logged in', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: false,
      setupState: 'enabled',
    }));
    window.history.pushState({}, '', '/portfolio');

    render(<App />);

    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
    expect(window.location.search).toBe('?redirect=%2Fportfolio');
  });

  it('renders the current route page after auth is ready', async () => {
    window.history.pushState({}, '', '/chat');

    render(<App />);

    expect(await screen.findByTestId('chat-page')).toBeInTheDocument();
    expect(setCurrentRoute).toHaveBeenCalledWith('/chat');
    expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it('preserves the query and fragment when redirecting a protected deep link to login', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: false,
      setupState: 'enabled',
    }));
    const destination = '/settings?category=system#desktop-version-info';
    window.history.pushState({}, '', destination);

    render(<App />);

    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
    expect(new URLSearchParams(window.location.search).get('redirect')).toBe(destination);
    expect(window.location.hash).toBe('');
  });

  it('uses the safe deep link when auth resolves before the login page can navigate', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: true,
      setupState: 'enabled',
    }));
    const destination = '/settings?category=system#desktop-version-info';
    window.history.pushState({}, '', `/login?redirect=${encodeURIComponent(destination)}`);

    render(<App />);

    expect(await screen.findByTestId('settings-page')).toBeInTheDocument();
    expect(window.location.pathname + window.location.search + window.location.hash).toBe(destination);
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it.each([
    'https://external.example/settings',
    '//external.example/settings',
    '/\\external.example/settings',
    '/%5cexternal.example/settings',
    '/%2fexternal.example/settings',
    '/..//external.example/settings',
    '/\t/external.example/settings',
    '/login?redirect=%2Fsettings',
    '/LOGIN/',
  ])('falls back to the default landing page for unsafe or looping login redirect %s', async (destination) => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: true,
      setupState: 'enabled',
    }));
    const origin = window.location.origin;
    window.history.pushState({}, '', `/login?redirect=${encodeURIComponent(destination)}`);

    render(<App />);

    expect(await screen.findByTestId('mine-page')).toBeInTheDocument();
    expect(window.location.origin).toBe(origin);
    expect(window.location.pathname + window.location.search + window.location.hash).toBe('/mine');
  });

  it('routes /usage to the token usage page after auth is ready', async () => {
    window.history.pushState({}, '', '/usage');

    render(<App />);

    expect(await screen.findByTestId('token-usage-page')).toBeInTheDocument();
    expect(setCurrentRoute).toHaveBeenCalledWith('/usage');
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it('routes /decision-signals to the AI signals page after auth is ready', async () => {
    window.history.pushState({}, '', '/decision-signals');

    render(<App />);

    expect(await screen.findByTestId('decision-signals-page')).toBeInTheDocument();
    expect(setCurrentRoute).toHaveBeenCalledWith('/decision-signals');
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
  });

  it('routes /data to the data capability page after auth is ready', async () => {
    window.history.pushState({}, '', '/data');

    render(<App />);

    expect(await screen.findByTestId('data-center-page')).toBeInTheDocument();
    expect(setCurrentRoute).toHaveBeenCalledWith('/data');
  });

  it('redirects authenticated login visits to the default landing page', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: true,
      setupState: 'enabled',
    }));
    window.history.pushState({}, '', '/login');

    render(<App />);

    expect(await screen.findByTestId('mine-page')).toBeInTheDocument();
    expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
  });

  it('redirects / to /mine inside the plan shell', async () => {
    window.history.pushState({}, '', '/');

    render(<App />);

    expect(await screen.findByTestId('mine-page')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/mine');
    expect(screen.queryByTestId('home-page')).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main');
    expect(screen.getByRole('link', { name: '我的股票' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryByRole('link', { name: '首页' })).not.toBeInTheDocument();
  });

  it.each([
    ['/boards', 'boards-page'],
    ['/picks', 'picks-page'],
    ['/preferences', 'preferences-page'],
  ])('renders %s inside the plan shell', async (path, testId) => {
    window.history.pushState({}, '', path);

    render(<App />);

    expect(await screen.findByTestId(testId)).toBeInTheDocument();
    expect(setCurrentRoute).toHaveBeenCalledWith(path);
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
  });

  it('keeps the old home page reachable at /home inside the legacy shell', async () => {
    window.history.pushState({}, '', '/home');

    render(<App />);

    expect(await screen.findByTestId('home-page')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '首页' })).toHaveAttribute('href', '/home');
    expect(screen.queryByTestId('mine-page')).not.toBeInTheDocument();
  });

  it('requires login for the plan routes when auth is enabled', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authEnabled: true,
      loggedIn: false,
      setupState: 'enabled',
    }));
    window.history.pushState({}, '', '/mine');

    render(<App />);

    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
    expect(window.location.search).toBe('?redirect=%2Fmine');
    expect(screen.queryByTestId('mine-page')).not.toBeInTheDocument();
  });

  it('keeps the shell mounted and resets the route boundary after page render errors', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    chatPageShouldThrow.value = true;
    window.history.pushState({}, '', '/chat');

    try {
      render(<App />);

      expect(await screen.findByRole('heading', { name: '页面加载失败' })).toBeInTheDocument();
      expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '重新加载页面' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '返回首页' })).toBeInTheDocument();

      chatPageShouldThrow.value = false;
      fireEvent.click(screen.getByRole('link', { name: '持仓' }));

      expect(await screen.findByTestId('portfolio-page')).toBeInTheDocument();
      expect(screen.queryByRole('heading', { name: '页面加载失败' })).not.toBeInTheDocument();
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe('App user_center mode', () => {
  const LOGIN_URL = 'https://user.example.test/login?return=https%3A%2F%2Fstock.example.test%2F';
  const LOGOUT_URL = 'https://user.example.test/logout?return=https%3A%2F%2Fstock.example.test%2F';

  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('shows the no-permission page with a logout link for a signed-in non-admin', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authMode: 'user_center',
      authEnabled: true,
      loggedIn: false,
      setupState: 'enabled',
      userCenter: {
        authenticated: true,
        authorized: false,
        username: 'Alice',
        role: 'user',
        loginUrl: LOGIN_URL,
        logoutUrl: LOGOUT_URL,
      },
    }));
    window.history.pushState({}, '', '/mine');

    render(<App />);

    expect(await screen.findByRole('heading', { name: '无权限' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '退出登录' })).toHaveAttribute('href', LOGOUT_URL);
    expect(screen.queryByTestId('mine-page')).not.toBeInTheDocument();
    expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
  });

  it('never renders the local login page for an unauthenticated user_center visitor', async () => {
    // A recent redirect stamp keeps the loop guard from navigating the test window.
    window.sessionStorage.setItem('dsa.userCenter.loginRedirectAt', String(Date.now()));
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authMode: 'user_center',
      authEnabled: true,
      loggedIn: false,
      setupState: 'enabled',
      userCenter: {
        authenticated: false,
        authorized: false,
        username: null,
        role: null,
        loginUrl: LOGIN_URL,
        logoutUrl: LOGOUT_URL,
      },
    }));
    window.history.pushState({}, '', '/portfolio');

    render(<App />);

    expect(await screen.findByRole('link', { name: '前往用户中心登录' })).toHaveAttribute('href', LOGIN_URL);
    expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
    expect(screen.queryByTestId('portfolio-page')).not.toBeInTheDocument();
  });

  it('renders the app for an authorized admin session', async () => {
    vi.mocked(AuthContext.useAuth).mockReturnValue(makeAuthState({
      authMode: 'user_center',
      authEnabled: true,
      loggedIn: true,
      setupState: 'enabled',
      userCenter: {
        authenticated: true,
        authorized: true,
        username: 'Darren',
        role: 'admin',
        loginUrl: LOGIN_URL,
        logoutUrl: LOGOUT_URL,
      },
    }));
    window.history.pushState({}, '', '/mine');

    render(<App />);

    expect(await screen.findByTestId('mine-page')).toBeInTheDocument();
  });
});

describe('App routing for regular users', () => {
  it('sends a regular user from the system settings to their personal settings', async () => {
    currentUserState.isAdmin = false;
    window.history.pushState({}, '', '/settings');

    render(<App />);

    expect(await screen.findByTestId('personal-settings-page')).toBeInTheDocument();
    expect(screen.queryByTestId('settings-page')).not.toBeInTheDocument();
    expect(window.location.pathname).toBe('/settings/personal');
  });

  it('sends a regular user from the plan preferences to their personal settings', async () => {
    currentUserState.isAdmin = false;
    window.history.pushState({}, '', '/preferences');

    render(<App />);

    expect(await screen.findByTestId('personal-settings-page')).toBeInTheDocument();
    expect(window.location.pathname).toBe('/settings/personal');
  });

  it('replaces the screening pages with an administrator-permission notice', async () => {
    currentUserState.isAdmin = false;
    window.history.pushState({}, '', '/picks');

    render(<App />);

    expect(await screen.findByRole('heading', { name: '需要管理员权限' })).toBeInTheDocument();
    expect(screen.queryByTestId('picks-page')).not.toBeInTheDocument();
  });

  it('serves the personal settings route to everyone', async () => {
    window.history.pushState({}, '', '/settings/personal');

    render(<App />);

    expect(await screen.findByTestId('personal-settings-page')).toBeInTheDocument();
  });

  it('keeps the system settings for an administrator', async () => {
    window.history.pushState({}, '', '/settings');

    render(<App />);

    expect(await screen.findByTestId('settings-page')).toBeInTheDocument();
  });
});
