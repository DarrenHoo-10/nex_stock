import type React from 'react';
import { lazy, useEffect, useMemo } from 'react';
import {
  Navigate,
  Outlet,
  createBrowserRouter,
  useLocation,
  RouterProvider,
} from 'react-router-dom';
import { ApiErrorAlert, Shell } from './components/common';
import { UserCenterForbidden, UserCenterLoginRedirect } from './components/auth/UserCenterGate';
import { PlanShell } from './components/plan/PlanShell';
import {
  PageLoadingFallback,
  RouteOutletBoundary,
  StandaloneRouteBoundary,
} from './components/layout/RouteBoundary';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { UiLanguageProvider, useUiLanguage } from './contexts/UiLanguageContext';
import { useAgentChatStore } from './stores/agentChatStore';
import { resolveLoginRedirect } from './utils/loginRedirect';
import './App.css';

const HomePage = lazy(() => import('./pages/HomePage'));
const BacktestPage = lazy(() => import('./pages/BacktestPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const LoginPage = lazy(() => import('./pages/LoginPage'));
const NotFoundPage = lazy(() => import('./pages/NotFoundPage'));
const ChatPage = lazy(() => import('./pages/ChatPage'));
const PortfolioPage = lazy(() => import('./pages/PortfolioPage'));
const DecisionSignalsPage = lazy(() => import('./pages/DecisionSignalsPage'));
const AlertsPage = lazy(() => import('./pages/AlertsPage'));
const TokenUsagePage = lazy(() => import('./pages/TokenUsagePage'));
const StockScreeningPage = lazy(() => import('./pages/StockScreeningPage'));
const DataCenterPage = lazy(() => import('./pages/DataCenterPage'));
const ClosingAnalysisPage = lazy(() => import('./pages/plan/ClosingAnalysisPage'));
const BoardsPage = lazy(() => import('./pages/plan/BoardsPage'));
const PicksPage = lazy(() => import('./pages/plan/PicksPage'));
const MinePage = lazy(() => import('./pages/plan/MinePage'));
const PreferencesPage = lazy(() => import('./pages/plan/PreferencesPage'));
const AssistantPage = lazy(() => import('./pages/plan/AssistantPage'));

const AppContent: React.FC = () => {
  const location = useLocation();
  const { authMode, userCenter, authEnabled, loggedIn, isLoading, loadError, refreshStatus } = useAuth();
  const { t } = useUiLanguage();

  useEffect(() => {
    useAgentChatStore.getState().setCurrentRoute(location.pathname);
  }, [location.pathname]);

  if (isLoading) {
    return <PageLoadingFallback />;
  }

  if (loadError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4">
        <div className="w-full max-w-lg">
          <ApiErrorAlert error={loadError} />
        </div>
        <button
          type="button"
          className="btn-primary"
          onClick={() => void refreshStatus()}
        >
          {t('common.retry')}
        </button>
      </div>
    );
  }

  if (authMode === 'user_center' && userCenter && !loggedIn) {
    // Login belongs to the user center: there is no local /login page in this mode.
    return userCenter.authenticated
      ? <UserCenterForbidden session={userCenter} />
      : <UserCenterLoginRedirect loginUrl={userCenter.loginUrl} />;
  }

  if (authEnabled && !loggedIn) {
    if (location.pathname === '/login') {
      return (
        <StandaloneRouteBoundary>
          <LoginPage />
        </StandaloneRouteBoundary>
      );
    }
    const redirect = encodeURIComponent(location.pathname + location.search + location.hash);
    return <Navigate to={`/login?redirect=${redirect}`} replace />;
  }

  if (location.pathname === '/login') {
    // Auth refresh may render this boundary before LoginPage's submit resolves.
    // Both paths must choose the same validated destination.
    const redirect = resolveLoginRedirect(new URLSearchParams(location.search).get('redirect'));
    return <Navigate to={redirect} replace />;
  }

  // Auth gating wraps both layouts: the plan shell and the legacy shell render below this boundary.
  return <Outlet />;
};

const LegacyShellLayout: React.FC = () => (
  <Shell>
    <RouteOutletBoundary />
  </Shell>
);

const planRouteChildren = [
  { path: '/closing-analysis', element: <ClosingAnalysisPage /> },
  { path: '/boards', element: <BoardsPage /> },
  { path: '/picks', element: <PicksPage /> },
  { path: '/mine', element: <MinePage /> },
  { path: '/preferences', element: <PreferencesPage /> },
  { path: '/assistant', element: <AssistantPage /> },
];

const legacyRouteChildren = [
  { path: '/home', element: <HomePage /> },
  { path: '/chat', element: <ChatPage /> },
  { path: '/portfolio', element: <PortfolioPage /> },
  { path: '/decision-signals', element: <DecisionSignalsPage /> },
  { path: '/screening', element: <StockScreeningPage /> },
  { path: '/data', element: <DataCenterPage /> },
  { path: '/backtest', element: <BacktestPage /> },
  { path: '/alerts', element: <AlertsPage /> },
  { path: '/usage', element: <TokenUsagePage /> },
  { path: '/settings', element: <SettingsPage /> },
  { path: '*', element: <NotFoundPage /> },
];

const routeChildren = [
  { path: '/', element: <Navigate to="/mine" replace /> },
  { element: <PlanShell />, children: planRouteChildren },
  { element: <LegacyShellLayout />, children: legacyRouteChildren },
];

const App: React.FC = () => {
  // Construct the data router lazily on first mount. `createBrowserRouter`
  // reads `window.location` at construction time, so for it to honour the
  // current URL (especially under tests that `window.history.pushState(...)`
  // before rendering, and to react when navigating the same module instance
  // across different entry URLs) it must be built inside the React tree, not
  // at module import time.
  const router = useMemo(
    () =>
      createBrowserRouter([
        {
          element: <AppContent />,
          children: routeChildren,
        },
      ]),
    [],
  );

  return (
    <UiLanguageProvider>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </UiLanguageProvider>
  );
};

export default App;
