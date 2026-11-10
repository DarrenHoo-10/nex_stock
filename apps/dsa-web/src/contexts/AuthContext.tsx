import type React from 'react';
import { useCallback, useContext, useEffect, useState } from 'react';
import { createParsedApiError, getParsedApiError, type ParsedApiError } from '../api/error';
import { authApi, type AuthMode } from '../api/auth';
import { meApi } from '../api/me';
import type { CurrentUser } from '../types/me';
import { AuthContext, type AuthContextValue, type UserCenterSession } from './authState';
import { useStockPoolStore } from '../stores';
import { setUserCenterSession } from '../utils/userCenterSession';

export type { UserCenterSession } from './authState';

/** `/me` could not be read: fall back to what the user-center session itself says (role decides admin). */
function currentUserFromSession(session: { username: string | null; role: string | null }): CurrentUser {
  const name = session.username ?? '';
  return {
    userId: name,
    handle: name,
    displayName: name,
    role: session.role ?? 'user',
    isAdmin: session.role === 'admin',
    authMode: 'user_center',
    modelKeyConfigured: true,
  };
}

function extractLoginError(err: unknown): ParsedApiError {
  const parsed = getParsedApiError(err);
  if (parsed.status === 429) {
    return createParsedApiError({
      title: '登录尝试过于频繁',
      message: '尝试次数过多，请稍后再试。',
      rawMessage: parsed.rawMessage,
      status: parsed.status,
      category: parsed.category,
    });
  }
  return parsed;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [authMode, setAuthMode] = useState<AuthMode>('local');
  const [userCenter, setUserCenter] = useState<UserCenterSession | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [loggedIn, setLoggedIn] = useState(false);
  const [passwordSet, setPasswordSet] = useState(false);
  const [passwordChangeable, setPasswordChangeable] = useState(false);
  const [setupState, setSetupState] = useState<'enabled' | 'password_retained' | 'no_password'>('no_password');
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<ParsedApiError | null>(null);

  const fetchStatus = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const status = await authApi.getStatus();
      if (status.authMode === 'user_center') {
        // Login is owned by the user center: the session endpoint decides, not the status flags.
        const session = await authApi.getUserCenterSession();
        setUserCenterSession(session);
        setAuthMode('user_center');
        setUserCenter({
          authenticated: session.authenticated,
          authorized: session.authorized,
          username: session.username,
          role: session.role,
          loginUrl: session.loginUrl,
          logoutUrl: session.logoutUrl,
        });
        if (session.authorized) {
          // One /me per session load: it decides admin gating and the model-key prompts before the first page renders.
          try {
            setCurrentUser(await meApi.getMe());
          } catch {
            setCurrentUser(currentUserFromSession(session));
          }
        } else {
          setCurrentUser(null);
        }
        setAuthEnabled(true);
        setLoggedIn(session.authorized);
        setPasswordSet(false);
        setPasswordChangeable(false);
        setSetupState('enabled');
        if (!session.authorized) {
          useStockPoolStore.getState().resetDashboardState();
        }
        return;
      }
      setUserCenterSession(null);
      setAuthMode('local');
      setUserCenter(null);
      setCurrentUser(null);
      setAuthEnabled(status.authEnabled);
      setLoggedIn(status.loggedIn);
      setPasswordSet(status.passwordSet ?? false);
      setPasswordChangeable(status.passwordChangeable ?? false);
      setSetupState(status.setupState);
      if (status.authEnabled && !status.loggedIn) {
        useStockPoolStore.getState().resetDashboardState();
      }
    } catch (err) {
      setLoadError(getParsedApiError(err));
      setUserCenterSession(null);
      setAuthMode('local');
      setUserCenter(null);
      setCurrentUser(null);
      setAuthEnabled(false);
      setLoggedIn(false);
      setPasswordSet(false);
      setPasswordChangeable(false);
      setSetupState('no_password');
      useStockPoolStore.getState().resetDashboardState();
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  const refreshCurrentUser = useCallback(async () => {
    if (authMode !== 'user_center') {
      return;
    }
    try {
      setCurrentUser(await meApi.getMe());
    } catch {
      // Keep the last known user; the next page load re-reads it.
    }
  }, [authMode]);

  const login = useCallback(
    async (
      password: string,
      passwordConfirm?: string
    ): Promise<{ success: boolean; error?: ParsedApiError }> => {
      if (authMode === 'user_center') {
        // The local password endpoints are disabled in this mode.
        return {
          success: false,
          error: createParsedApiError({
            title: '登录方式不可用',
            message: '当前使用 NexorAI 用户中心登录，请通过用户中心登录。',
            rawMessage: 'auth_mode_user_center',
          }),
        };
      }
      try {
        await authApi.login(password, passwordConfirm);
        await fetchStatus();
        return { success: true };
      } catch (err: unknown) {
        return { success: false, error: extractLoginError(err) };
      }
    },
    [authMode, fetchStatus]
  );

  const changePassword = useCallback(
    async (
      currentPassword: string,
      newPassword: string,
      newPasswordConfirm: string
    ): Promise<{ success: boolean; error?: ParsedApiError }> => {
      try {
        await authApi.changePassword(currentPassword, newPassword, newPasswordConfirm);
        return { success: true };
      } catch (err: unknown) {
        return { success: false, error: getParsedApiError(err) };
      }
    },
    []
  );

  const logout = useCallback(async () => {
    if (authMode === 'user_center') {
      // The user center clears its own session, then returns the browser to PUBLIC_ORIGIN.
      if (userCenter?.logoutUrl) {
        window.location.assign(userCenter.logoutUrl);
      }
      return;
    }
    let logoutError: unknown = null;
    try {
      await authApi.logout();
    } catch (err) {
      logoutError = err;
    } finally {
      await fetchStatus();
    }

    if (logoutError && getParsedApiError(logoutError).status !== 401) {
      throw logoutError;
    }
  }, [authMode, userCenter, fetchStatus]);

  return (
    <AuthContext.Provider
      value={{
        authMode,
        userCenter,
        currentUser,
        authEnabled,
        loggedIn,
        passwordSet,
        passwordChangeable,
        setupState,
        isLoading,
        loadError,
        login,
        changePassword,
        logout,
        refreshStatus: fetchStatus,
        refreshCurrentUser,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components -- useAuth is a hook, co-located for context access
export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return ctx;
}
