import { useOptionalAuth } from '../contexts/authState';
import type { CurrentUser } from '../types/me';

export type CurrentUserView = {
  /** The `/me` payload; null in local mode, outside an `AuthProvider`, or while signed out. */
  user: CurrentUser | null;
  /**
   * Whether the process-level (admin-only) pages and actions are available.
   * Local mode, or no user loaded at all, is the single-user deployment: the user is the administrator.
   */
  isAdmin: boolean;
  /** False only for a loaded user that has no usable model of their own; everyone else never sees the prompts. */
  modelKeyConfigured: boolean;
  displayName: string;
  /** True in user_center mode, where more than one person can sign in. */
  isMultiUser: boolean;
  refresh: () => Promise<void>;
};

const noop = async () => undefined;

/**
 * Who the app is acting for (docs/design/multi-user.md section 10). Reads the `/me` result the auth provider
 * fetched once at app start; there is no second source. Local mode keeps behaving exactly as before:
 * administrator, model key assumed present, no user label.
 */
export function useCurrentUser(): CurrentUserView {
  const auth = useOptionalAuth();
  const user = auth?.currentUser ?? null;
  if (!user) {
    return { user: null, isAdmin: true, modelKeyConfigured: true, displayName: '', isMultiUser: false, refresh: noop };
  }
  return {
    user,
    isAdmin: user.isAdmin,
    modelKeyConfigured: user.modelKeyConfigured,
    displayName: user.displayName || user.handle || user.userId,
    isMultiUser: user.authMode === 'user_center',
    refresh: auth?.refreshCurrentUser ?? noop,
  };
}
