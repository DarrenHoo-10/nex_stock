import type { Transition } from 'motion/react';

/**
 * Shared spring for the plan shell. Matches the prototype's `linear()` spring curve:
 * ~0.5s visual duration with a slight (~3%) overshoot.
 */
export const PLAN_SPRING: Transition = { type: 'spring', visualDuration: 0.5, bounce: 0.25 };

/** Slightly longer spring used for windows entering the page. */
export const PLAN_SPRING_SLOW: Transition = { type: 'spring', visualDuration: 0.65, bounce: 0.2 };

/** Quick exit for overlays: leaving should feel snappier than entering. */
export const PLAN_EXIT: Transition = { duration: 0.16, ease: 'easeIn' };

/** Used instead of any spring when the user prefers reduced motion. */
export const PLAN_INSTANT: Transition = { duration: 0 };

/** Ease-out used by number count-up (prototype: 1 - (1 - t)^4). */
export const PLAN_COUNT_EASE: [number, number, number, number] = [0.25, 1, 0.5, 1];

/** Default stagger step in seconds (prototype: 45ms). */
export const PLAN_STAGGER_GAP = 0.045;
