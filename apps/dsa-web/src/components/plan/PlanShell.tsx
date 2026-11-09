import type React from 'react';
import { useEffect, useRef } from 'react';
import { LayoutGroup, MotionConfig, motion } from 'motion/react';
import { NavLink, useLocation } from 'react-router-dom';
import { useAuth } from '../../contexts/AuthContext';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import { RouteOutletBoundary } from '../layout/RouteBoundary';
import { PLAN_INSTANT, PLAN_SPRING } from './motion/springs';
import { usePlanReducedMotion } from './motion/usePlanMotion';
import { PlanIcon } from './PlanIcon';
import type { PlanIconName } from './PlanIcon';
import { PlanToast } from './PlanToast';
import { AssistantDrawerHost } from './assistant/AssistantDrawerHost';
import '../../styles/plan-glass.css';

type OrnamentItem = {
  to: string;
  icon: PlanIconName;
  /** Short visible label. */
  shortKey: UiTextKey;
  /** Full accessible name. */
  labelKey: UiTextKey;
};

const MAIN_ITEMS: OrnamentItem[] = [
  { to: '/boards', icon: 'boards', shortKey: 'plan.nav.boards', labelKey: 'layout.nav.boards' },
  { to: '/picks', icon: 'picks', shortKey: 'plan.nav.picks', labelKey: 'layout.nav.picks' },
  { to: '/mine', icon: 'mine', shortKey: 'plan.nav.mine', labelKey: 'layout.nav.mine' },
  { to: '/closing-analysis', icon: 'closing', shortKey: 'plan.nav.closing', labelKey: 'layout.nav.closingAnalysis' },
  { to: '/assistant', icon: 'chat', shortKey: 'plan.nav.assistant', labelKey: 'layout.nav.assistant' },
];

const SETTINGS_ITEM: OrnamentItem = {
  to: '/preferences',
  icon: 'settings',
  shortKey: 'plan.nav.preferences',
  labelKey: 'plan.nav.preferences',
};

const OrnamentLink: React.FC<{ item: OrnamentItem; reduced: boolean }> = ({ item, reduced }) => {
  const { t } = useUiLanguage();
  return (
    <NavLink to={item.to} className="orn-btn" aria-label={t(item.labelKey)}>
      {({ isActive }) => (
        <>
          {isActive ? (
            <motion.span
              className="pill"
              layoutId="plan-nav-pill"
              transition={reduced ? PLAN_INSTANT : PLAN_SPRING}
            />
          ) : null}
          <PlanIcon name={item.icon} />
          <span aria-hidden="true">{t(item.shortKey)}</span>
        </>
      )}
    </NavLink>
  );
};

/**
 * Routed layout for the next-day plan pages: ambient glass background, floating vertical ornament nav
 * (bottom bar at <=860px), skip link, `<main id="main">` and the toast renderer. Dark glass only; all styles
 * are scoped under `.plan-shell` (see styles/plan-glass.css).
 */
export const PlanShell: React.FC = () => {
  const { t } = useUiLanguage();
  const { authMode, userCenter } = useAuth();
  const location = useLocation();
  const reduced = usePlanReducedMotion();
  const mainRef = useRef<HTMLElement>(null);
  const logoutUrl = authMode === 'user_center' ? userCenter?.logoutUrl : undefined;

  // Move focus to <main> and reset scroll after a page change (not on first mount).
  const lastPathRef = useRef(location.pathname);
  useEffect(() => {
    if (lastPathRef.current === location.pathname) {
      return;
    }
    lastPathRef.current = location.pathname;
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
  }, [location.pathname, reduced]);

  // Soft spotlight that follows the pointer over glass windows.
  const handlePointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== 'mouse') {
      return;
    }
    const target = event.target as Element | null;
    const win = target?.closest<HTMLElement>('.win');
    if (!win) {
      return;
    }
    const rect = win.getBoundingClientRect();
    win.style.setProperty('--mx', `${event.clientX - rect.left}px`);
    win.style.setProperty('--my', `${event.clientY - rect.top}px`);
  };

  return (
    <MotionConfig reducedMotion="user" transition={PLAN_SPRING}>
      <div className="plan-shell plan-page" onPointerMove={handlePointerMove}>
        <div className="plan-bg" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </div>
        <a
          className="skiplink"
          href="#main"
          onClick={(event) => {
            event.preventDefault();
            mainRef.current?.focus();
          }}
        >
          {t('plan.skipToMain')}
        </a>

        <LayoutGroup id="plan-nav">
          <nav className="ornament glass" aria-label={t('layout.mainNav')}>
            {MAIN_ITEMS.map((item) => (
              <OrnamentLink key={item.to} item={item} reduced={reduced} />
            ))}
            <span className="orn-sep" aria-hidden="true" />
            <OrnamentLink item={SETTINGS_ITEM} reduced={reduced} />
            {logoutUrl ? (
              <a className="orn-btn" href={logoutUrl} aria-label={t('layout.logout')}>
                <PlanIcon name="logout" />
                <span aria-hidden="true">{t('layout.logout')}</span>
              </a>
            ) : null}
          </nav>
        </LayoutGroup>

        <div className="app">
          <main id="main" ref={mainRef} tabIndex={-1}>
            <RouteOutletBoundary />
          </main>
        </div>

        <AssistantDrawerHost />
        <PlanToast />
      </div>
    </MotionConfig>
  );
};
