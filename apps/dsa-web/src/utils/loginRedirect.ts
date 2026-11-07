/** Landing page after login (`/` redirects here too). */
export const DEFAULT_LOGIN_REDIRECT = '/mine';

/** Resolve the shared post-login destination without allowing external URLs or login loops. */
export function resolveLoginRedirect(rawRedirect: string | null): string {
  if (!rawRedirect?.startsWith('/') || rawRedirect.startsWith('//') || rawRedirect.includes('\\')) {
    return DEFAULT_LOGIN_REDIRECT;
  }

  try {
    const target = new URL(rawRedirect, window.location.origin);
    const pathname = decodeURIComponent(target.pathname);
    if (
      target.origin !== window.location.origin
      || pathname.startsWith('//')
      || pathname.includes('\\')
      || /^\/login\/?$/i.test(pathname)
    ) {
      return DEFAULT_LOGIN_REDIRECT;
    }
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return DEFAULT_LOGIN_REDIRECT;
  }
}
