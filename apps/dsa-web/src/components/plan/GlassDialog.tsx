import type React from 'react';
import { useId, useRef } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import { PLAN_EXIT, PLAN_INSTANT, PLAN_SPRING } from './motion/springs';
import { usePlanReducedMotion } from './motion/usePlanMotion';
import { PlanIcon } from './PlanIcon';
import { PlanPortal } from './PlanPortal';
import { useOverlayFocus, useScrollLock } from './useOverlayBehavior';

export interface GlassDialogProps {
  open: boolean;
  onClose: () => void;
  /** Visible heading; also the dialog's accessible name via `aria-labelledby`. */
  title: React.ReactNode;
  children: React.ReactNode;
  /** Action row under the body (buttons). */
  footer?: React.ReactNode;
  /** Max width in px or any CSS length, default 760. */
  width?: number | string;
  /** Close on backdrop click, default true. */
  closeOnBackdrop?: boolean;
  /** Element to focus on open; default is the first focusable element in the body. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
}

const DialogSheet: React.FC<Omit<GlassDialogProps, 'open'>> = ({
  onClose,
  title,
  children,
  footer,
  width,
  closeOnBackdrop = true,
  initialFocusRef,
}) => {
  const { t } = useUiLanguage();
  const reduced = usePlanReducedMotion();
  const titleId = useId();
  const sheetRef = useRef<HTMLDivElement>(null);
  const transition = reduced ? PLAN_INSTANT : PLAN_SPRING;
  const widthStyle =
    width === undefined
      ? undefined
      : ({ '--dlg-w': typeof width === 'number' ? `${width}px` : width } as React.CSSProperties);

  useScrollLock(true);
  useOverlayFocus(sheetRef, { onEscape: onClose, initialFocusRef, skipFirstFocusable: true });

  return (
    <motion.div
      className="dlg-scrim"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0, transition: { duration: reduced ? 0 : 0.16 } }}
      transition={{ duration: reduced ? 0 : 0.25 }}
      onMouseDown={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <motion.div
        ref={sheetRef}
        className="dlg"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={widthStyle}
        initial={{ opacity: 0, y: 24, scale: 0.97 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.98, transition: reduced ? PLAN_INSTANT : PLAN_EXIT }}
        transition={transition}
      >
        <div className="dlg-head">
          <h2 id={titleId}>{title}</h2>
          <button type="button" className="x" aria-label={t('common.close')} onClick={onClose}>
            <PlanIcon name="close" size={16} />
          </button>
        </div>
        <div className="dlg-body">{children}</div>
        {footer ? <div className="dlg-foot">{footer}</div> : null}
      </motion.div>
    </motion.div>
  );
};

/**
 * Centered glass sheet: spring in, Escape/backdrop close, Tab trap, initial focus and focus return,
 * `aria-modal`, and the title wired through `aria-labelledby`. Portaled into a `.plan-shell` wrapper.
 */
export const GlassDialog: React.FC<GlassDialogProps> = ({ open, ...sheetProps }) => (
  <PlanPortal>
    <AnimatePresence>{open ? <DialogSheet key="dialog" {...sheetProps} /> : null}</AnimatePresence>
  </PlanPortal>
);
