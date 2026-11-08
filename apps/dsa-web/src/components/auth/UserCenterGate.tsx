import type React from 'react';
import { useEffect, useState } from 'react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UserCenterSession } from '../../contexts/AuthContext';
import { redirectToUserCenter } from '../../utils/userCenterSession';

const REDIRECT_STAMP_KEY = 'dsa.userCenter.loginRedirectAt';
/** A second automatic redirect within this window means the login did not stick (loop guard). */
const REDIRECT_LOOP_WINDOW_MS = 15_000;

function readRedirectStamp(): number {
  try {
    return Number(window.sessionStorage.getItem(REDIRECT_STAMP_KEY)) || 0;
  } catch {
    return 0;
  }
}

function writeRedirectStamp(): void {
  try {
    window.sessionStorage.setItem(REDIRECT_STAMP_KEY, String(Date.now()));
  } catch {
    // Storage may be blocked; the guard is best-effort.
  }
}

const GateFrame: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-base px-4 text-center">
    {children}
  </div>
);

/**
 * Not signed in (user_center mode): send the browser to the user-center login page.
 * Falls back to a manual link when we were already redirected moments ago, so a
 * cookie that never sticks cannot cause an endless redirect loop.
 */
export const UserCenterLoginRedirect: React.FC<{ loginUrl: string }> = ({ loginUrl }) => {
  const { t } = useUiLanguage();
  const [looping] = useState(() => Date.now() - readRedirectStamp() < REDIRECT_LOOP_WINDOW_MS);

  useEffect(() => {
    if (looping) {
      return;
    }
    writeRedirectStamp();
    redirectToUserCenter(loginUrl);
  }, [looping, loginUrl]);

  return (
    <GateFrame>
      <p className="text-sm text-secondary-text">
        {looping ? t('auth.userCenter.redirectLoop') : t('auth.userCenter.redirecting')}
      </p>
      <a className="btn-primary" href={loginUrl}>
        {t('auth.userCenter.loginLink')}
      </a>
    </GateFrame>
  );
};

/** Signed in but not an administrator (user_center mode). */
export const UserCenterForbidden: React.FC<{ session: UserCenterSession }> = ({ session }) => {
  const { t } = useUiLanguage();
  return (
    <GateFrame>
      <h1 className="text-xl font-semibold text-foreground">{t('auth.userCenter.forbiddenTitle')}</h1>
      <p className="max-w-md text-sm text-secondary-text">
        {t('auth.userCenter.forbiddenMessage', { name: session.username || session.role || '-' })}
      </p>
      <a className="btn-primary" href={session.logoutUrl}>
        {t('auth.userCenter.forbiddenLogout')}
      </a>
    </GateFrame>
  );
};
