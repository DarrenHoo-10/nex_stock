import { useReducedMotion } from 'motion/react';
import type { Transition } from 'motion/react';
import { PLAN_INSTANT, PLAN_SPRING } from './springs';

/** True when the user prefers reduced motion. All plan animations must be gated on this. */
export function usePlanReducedMotion(): boolean {
  return useReducedMotion() ?? false;
}

/**
 * The shared spring, or an instant transition under reduced motion.
 * Pass overrides (e.g. `{ delay }`) to extend the spring; overrides are dropped when reduced.
 */
export function usePlanTransition(overrides?: Transition): Transition {
  const reduced = usePlanReducedMotion();
  if (reduced) {
    return PLAN_INSTANT;
  }
  return overrides ? { ...PLAN_SPRING, ...overrides } : PLAN_SPRING;
}
