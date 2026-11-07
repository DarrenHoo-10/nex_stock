import type React from 'react';
import { useId, useRef } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PLAN_EXIT, PLAN_INSTANT, PLAN_SPRING } from './motion/springs';
import { usePlanReducedMotion } from './motion/usePlanMotion';
import { PlanIcon } from './PlanIcon';
import type { PlanIconName } from './PlanIcon';
import { PlanPortal } from './PlanPortal';
import { useOverlayFocus, useScrollLock } from './useOverlayBehavior';

export interface GlassDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Visible heading; also the drawer's accessible name. */
  title: React.ReactNode;
  /** Optional icon before the title. */
  icon?: PlanIconName;
  /** Content area (flex column; put your own scroller inside). */
  children: React.ReactNode;
  /** Fixed area under the content (e.g. composer). */
  footer?: React.ReactNode;
  /** Element id, default "assistant" (the header button's aria-controls points here). */
  id?: string;
  /** Width in px on desktop, default 410. Full screen at <=860px. */
  width?: number;
  initialFocusRef?: React.RefObject<HTMLElement | null>;
}

const OFFSCREEN = 'calc(100% + 32px)';

const DrawerPanel: React.FC<Omit<GlassDrawerProps, 'open'>> = ({
  onClose,
  title,
  icon,
  children,
  footer,
  id = 'assistant',
  width,
  initialFocusRef,
}) => {
  const { t } = useUiLanguage();
  const reduced = usePlanReducedMotion();
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const transition = reduced ? PLAN_INSTANT : { ...PLAN_SPRING, visualDuration: 0.6 };
  const widthStyle = width === undefined ? undefined : ({ '--drawer-w': `${width}px` } as React.CSSProperties);

  useScrollLock(true);
  useOverlayFocus(panelRef, { onEscape: onClose, initialFocusRef, skipFirstFocusable: true });

  return (
    <>
      <motion.div
        className="scrim"
        aria-hidden="true"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0, transition: { duration: reduced ? 0 : 0.2 } }}
        transition={{ duration: reduced ? 0 : 0.3 }}
        onMouseDown={onClose}
      />
      <motion.aside
        ref={panelRef}
        id={id}
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={widthStyle}
        initial={{ x: OFFSCREEN }}
        animate={{ x: 0 }}
        exit={{ x: OFFSCREEN, transition: reduced ? PLAN_INSTANT : { ...PLAN_EXIT, duration: 0.3 } }}
        transition={transition}
      >
        <header>
          <h2 id={titleId}>
            {icon ? <PlanIcon name={icon} size={18} /> : null}
            {title}
          </h2>
          <button type="button" className="close" aria-label={t('common.close')} onClick={onClose}>
            <PlanIcon name="close" size={16} />
          </button>
        </header>
        <div className="drawer-body">{children}</div>
        {footer ? <div className="drawer-foot">{footer}</div> : null}
      </motion.aside>
    </>
  );
};

/** Right-side glass drawer (full screen on mobile) with spring slide, Escape/scrim close and focus handling. */
export const GlassDrawer: React.FC<GlassDrawerProps> = ({ open, ...panelProps }) => (
  <PlanPortal>
    <AnimatePresence>{open ? <DrawerPanel key="drawer" {...panelProps} /> : null}</AnimatePresence>
  </PlanPortal>
);
