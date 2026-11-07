import type React from 'react';
import { useEffect } from 'react';
import { animate, motion, useMotionValue, useTransform } from 'motion/react';
import { formatCount } from './formatCount';
import { PLAN_COUNT_EASE } from './springs';
import { usePlanReducedMotion } from './usePlanMotion';

export interface CountUpProps {
  /** Target value. Non-finite values render a dash. */
  value: number | null | undefined;
  /** Fixed decimals, default 0. */
  decimals?: number;
  /** Prefix positive numbers with "+". */
  sign?: boolean;
  /** Rendered after the number in a `<small>`, e.g. "%" or " 股". */
  suffix?: string;
  /** Animate from 0 on first mount (page change). Default: first render shows the value directly. */
  animateOnMount?: boolean;
  /** Seconds, default 0.76. */
  duration?: number;
  className?: string;
}

/**
 * Number that rolls from its previous value to the new one. The final value is always committed:
 * under reduced motion, for non-animated updates, and by a timer fallback if frames are throttled.
 */
export const CountUp: React.FC<CountUpProps> = ({
  value,
  decimals = 0,
  sign = false,
  suffix,
  animateOnMount = false,
  duration = 0.76,
  className,
}) => {
  const reduced = usePlanReducedMotion();
  const finite = typeof value === 'number' && Number.isFinite(value);
  const target = finite ? value : 0;
  // Under reduced motion start at the target so the first frame already shows the final value.
  const motionValue = useMotionValue(animateOnMount && !reduced ? 0 : target);
  const text = useTransform(motionValue, (latest) => formatCount(latest, decimals, sign));

  useEffect(() => {
    if (reduced || !finite) {
      motionValue.set(target);
      return undefined;
    }
    const controls = animate(motionValue, target, { duration, ease: PLAN_COUNT_EASE });
    // Frames can be throttled (background tab); never leave a stale intermediate value on screen.
    const fallback = window.setTimeout(() => motionValue.set(target), duration * 1000 + 150);
    return () => {
      controls.stop();
      window.clearTimeout(fallback);
    };
  }, [reduced, finite, target, duration, motionValue]);

  if (!finite) {
    return <span className={className}>—</span>;
  }

  return (
    <>
      <motion.span className={className ? `num ${className}` : 'num'} data-count={target}>
        {text}
      </motion.span>
      {suffix ? <small>{suffix}</small> : null}
    </>
  );
};
