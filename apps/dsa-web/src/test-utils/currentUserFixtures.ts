import { vi } from 'vitest';
import type { AuthContextValue } from '../contexts/authState';
import type { CurrentUser } from '../types/me';

/** A signed-in regular user of a multi-user (AUTH_MODE=user_center) deployment. */
export const regularUser: CurrentUser = {
  userId: 'u-alice',
  handle: 'alice',
  displayName: 'Alice',
  role: 'user',
  isAdmin: false,
  authMode: 'user_center',
  modelKeyConfigured: true,
};

/** The administrator of a multi-user deployment. */
export const adminUser: CurrentUser = {
  userId: 'u-admin',
  handle: 'root',
  displayName: 'Root',
  role: 'admin',
  isAdmin: true,
  authMode: 'user_center',
  modelKeyConfigured: true,
};

export function makeAuthValue(
  currentUser: CurrentUser | null,
  overrides: Partial<AuthContextValue> = {},
): AuthContextValue {
  return {
    authMode: currentUser ? 'user_center' : 'local',
    userCenter: null,
    currentUser,
    authEnabled: false,
    loggedIn: true,
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
