import type React from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { usePlanToastStore } from '../../stores/planToastStore';
import { PLAN_EXIT, PLAN_INSTANT, PLAN_SPRING } from './motion/springs';
import { usePlanReducedMotion } from './motion/usePlanMotion';
import { PlanPortal } from './PlanPortal';

/**
 * Renders the current plan toast (see `usePlanToastStore` / `showPlanToast`). The `role="status"` region is
 * always mounted so assistive tech announces the text when it appears. Mounted once by PlanShell.
 */
export const PlanToast: React.FC = () => {
  const toast = usePlanToastStore((state) => state.toast);
  const reduced = usePlanReducedMotion();
  return (
    <PlanPortal>
      <div className="toast-region" role="status" aria-live="polite">
        <AnimatePresence mode="wait">
          {toast ? (
            <motion.div
              key={toast.id}
              className="toast"
              initial={{ opacity: 0, y: 16, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98, transition: reduced ? PLAN_INSTANT : PLAN_EXIT }}
              transition={reduced ? PLAN_INSTANT : PLAN_SPRING}
            >
              {toast.message}
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </PlanPortal>
  );
};
