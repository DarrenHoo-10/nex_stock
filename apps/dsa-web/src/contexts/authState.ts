import { createContext, useContext } from 'react';
import type { ParsedApiError } from '../api/error';
import type { AuthMode } from '../api/auth';
import type { CurrentUser } from '../types/me';

/** Session facts from `GET /api/session` (AUTH_MODE=user_center). */
export type UserCenterSession = {
  /** The user center knows this browser session. */
  authenticated: boolean;
  /** The session also holds the required role (admin); only then is the app usable. */
  authorized: boolean;
  username: string | null;
  role: string | null;
  loginUrl: string;
  logoutUrl: string;
};

export type AuthContextValue = {
  authMode: AuthMode;
  /** Null outside user_center mode. */
  userCenter: UserCenterSession | null;
  /**
   * `GET /api/v1/me`, fetched once with the session (user_center mode only).
   * Null in local mode (the single user is the administrator, exactly as before multi-user) and while signed out.
   */
  currentUser: CurrentUser | null;
  authEnabled: boolean;
  loggedIn: boolean;
  passwordSet: boolean;
  passwordChangeable: boolean;
  setupState: 'enabled' | 'password_retained' | 'no_password';
  isLoading: boolean;
  loadError: ParsedApiError | null;
  login: (password: string, passwordConfirm?: string) => Promise<{ success: boolean; error?: ParsedApiError }>;
  changePassword: (
    currentPassword: string,
    newPassword: string,
    newPasswordConfirm: string
  ) => Promise<{ success: boolean; error?: ParsedApiError }>;
  logout: () => Promise<void>;
  refreshStatus: () => Promise<void>;
  /** Re-read `/me` (e.g. after saving a personal model key flips `modelKeyConfigured`). */
  refreshCurrentUser: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | null>(null);

/** The auth context, or null outside an `AuthProvider` (pages rendered in isolation behave like local mode). */
export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}
