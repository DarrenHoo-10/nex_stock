/**
 * Process-wide holder for the user-center session facts the HTTP layer needs
 * (AUTH_MODE=user_center). AuthContext writes it after `GET /api/session`;
 * the axios client and the streaming fetch read it synchronously.
 */

export const CSRF_HEADER = 'X-CSRF-Token';

const SAFE_METHODS = new Set(['get', 'head', 'options']);

let csrfToken: string | null = null;
let loginUrl: string | null = null;

export function setUserCenterSession(session: { csrfToken: string | null; loginUrl: string | null } | null): void {
  csrfToken = session?.csrfToken || null;
  loginUrl = session?.loginUrl || null;
}

export function getUserCenterLoginUrl(): string | null {
  return loginUrl;
}

/** Headers to merge into a request so non-GET calls carry the CSRF token. */
export function csrfHeadersFor(method: string | undefined): Record<string, string> {
  if (!csrfToken || SAFE_METHODS.has((method ?? 'get').toLowerCase())) {
    return {};
  }
  return { [CSRF_HEADER]: csrfToken };
}

/** Hard navigation to the user center (kept separate so tests can replace it). */
export function redirectToUserCenter(url: string): void {
  window.location.replace(url);
}
