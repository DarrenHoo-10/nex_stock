import type React from 'react';
import { AuthContext, type AuthContextValue } from '../contexts/authState';
import type { CurrentUser } from '../types/me';
import { makeAuthValue } from './currentUserFixtures';

/**
 * Provides the current user the way `AuthProvider` does after `/me`, without any network:
 * `null` is local mode (the single administrator, nothing multi-user is shown).
 */
export const CurrentUserProvider: React.FC<{
  user: CurrentUser | null;
  value?: Partial<AuthContextValue>;
  children: React.ReactNode;
}> = ({ user, value, children }) => (
  <AuthContext.Provider value={makeAuthValue(user, value)}>{children}</AuthContext.Provider>
);
