import type React from 'react';
import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, LayoutGroup, MotionConfig, motion } from 'motion/react';
import { NavLink, useLocation } from 'react-router-dom';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiTextKey } from '../../i18n/uiText';
import { RouteOutletBoundary } from '../layout/RouteBoundary';
import { PLAN_EXIT, PLAN_INSTANT, PLAN_SPRING } from './motion/springs';
import { usePlanReducedMotion } from './motion/usePlanMotion';
import { PlanIcon } from './PlanIcon';
import type { PlanIconName } from './PlanIcon';
import { PlanPortal } from './PlanPortal';
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
];

const SETTINGS_ITEM: OrnamentItem = {
  to: '/preferences',
  icon: 'settings',
  shortKey: 'plan.nav.preferences',
  labelKey: 'plan.nav.preferences',
};

/** Existing pages, reachable from the 「更多」 popover. */
const MORE_ITEMS: Array<{ to: string; labelKey: UiTextKey }> = [
  { to: '/home', labelKey: 'plan.more.home' },
  { to: '/chat', labelKey: 'plan.more.chat' },
  { to: '/portfolio', labelKey: 'plan.more.portfolio' },
  { to: '/decision-signals', labelKey: 'plan.more.decisionSignals' },
  { to: '/screening', labelKey: 'plan.more.screening' },
  { to: '/backtest', labelKey: 'plan.more.backtest' },
  { to: '/alerts', labelKey: 'plan.more.alerts' },
  { to: '/data', labelKey: 'plan.more.data' },
  { to: '/usage', labelKey: 'plan.more.usage' },
  { to: '/settings', labelKey: 'plan.more.settings' },
];

const MORE_POPOVER_ID = 'plan-more-pages';

type MorePlacement = { pathname: string; left: number; bottom: number };

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
  const location = useLocation();
  const reduced = usePlanReducedMotion();
  const mainRef = useRef<HTMLElement>(null);
  const moreButtonRef = useRef<HTMLButtonElement>(null);
  const morePopoverRef = useRef<HTMLElement>(null);
  const [placement, setPlacement] = useState<MorePlacement | null>(null);
  // The popover closes by itself once the route changes.
  const moreOpen = placement !== null && placement.pathname === location.pathname;

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

  // Popover: outside click and Escape close it; focus moves to the first link on open.
  useEffect(() => {
    if (!moreOpen) {
      return undefined;
    }
    morePopoverRef.current?.querySelector<HTMLElement>('a')?.focus();
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && (morePopoverRef.current?.contains(target) || moreButtonRef.current?.contains(target))) {
        return;
      }
      setPlacement(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setPlacement(null);
        moreButtonRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [moreOpen]);

  const toggleMore = () => {
    if (moreOpen) {
      setPlacement(null);
      return;
    }
    const rect = moreButtonRef.current?.getBoundingClientRect();
    setPlacement({
      pathname: location.pathname,
      left: rect ? rect.right + 14 : 118,
      bottom: rect ? Math.max(12, window.innerHeight - rect.bottom) : 24,
    });
  };

  // Tab leaving either end of the popover returns focus to the button instead of the end of the page.
  const handlePopoverKeyDown = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Tab') {
      return;
    }
    const links = event.currentTarget.querySelectorAll<HTMLElement>('a');
    const first = links[0];
    const last = links[links.length - 1];
    if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault();
      setPlacement(null);
      moreButtonRef.current?.focus();
    }
  };

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

  const popoverStyle = placement
    ? ({ '--pop-left': `${placement.left}px`, '--pop-bottom': `${placement.bottom}px` } as React.CSSProperties)
    : undefined;

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
            <button
              ref={moreButtonRef}
              type="button"
              className="orn-btn"
              aria-label={t('plan.nav.more')}
              aria-haspopup="true"
              aria-expanded={moreOpen}
              aria-controls={MORE_POPOVER_ID}
              onClick={toggleMore}
            >
              <PlanIcon name="more" />
              <span aria-hidden="true">{t('plan.nav.more')}</span>
            </button>
          </nav>
        </LayoutGroup>

        <div className="app">
          <main id="main" ref={mainRef} tabIndex={-1}>
            <RouteOutletBoundary />
          </main>
        </div>

        <PlanPortal>
          <AnimatePresence>
            {moreOpen ? (
              <motion.nav
                key="more"
                ref={morePopoverRef}
                id={MORE_POPOVER_ID}
                className="more-pop"
                aria-label={t('plan.nav.moreTitle')}
                style={popoverStyle}
                initial={{ opacity: 0, scale: 0.94, y: 8 }}
                animate={{ opacity: 1, scale: 1, y: 0 }}
                exit={{ opacity: 0, scale: 0.96, y: 4, transition: reduced ? PLAN_INSTANT : PLAN_EXIT }}
                transition={reduced ? PLAN_INSTANT : PLAN_SPRING}
                onKeyDown={handlePopoverKeyDown}
              >
                <h2>{t('plan.nav.moreTitle')}</h2>
                <ul>
                  {MORE_ITEMS.map((item) => (
                    <li key={item.to}>
                      <NavLink to={item.to}>{t(item.labelKey)}</NavLink>
                    </li>
                  ))}
                </ul>
              </motion.nav>
            ) : null}
          </AnimatePresence>
        </PlanPortal>
        <AssistantDrawerHost />
        <PlanToast />
      </div>
    </MotionConfig>
  );
};
