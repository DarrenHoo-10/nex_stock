import type React from 'react';
import { useId } from 'react';
import { motion } from 'motion/react';
import { PLAN_SPRING } from './springs';
import { usePlanReducedMotion } from './usePlanMotion';

export interface SegmentedOption<T extends string> {
  value: T;
  label: React.ReactNode;
}

export interface SegmentedControlProps<T extends string> {
  options: ReadonlyArray<SegmentedOption<T>>;
  value: T;
  onChange: (value: T) => void;
  /** Accessible name of the group (required: the control has no visible label). */
  ariaLabel: string;
  className?: string;
}

/** Glass segmented control; the selected thumb slides between options via a shared `layoutId`. */
export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
  className,
}: SegmentedControlProps<T>) {
  const thumbId = useId();
  const reduced = usePlanReducedMotion();
  return (
    <div className={className ? `seg ${className}` : 'seg'} role="group" aria-label={ariaLabel}>
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
          >
            {selected ? (
              <motion.span
                className="thumb"
                layoutId={`seg-thumb-${thumbId}`}
                transition={reduced ? { duration: 0 } : PLAN_SPRING}
              />
            ) : null}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
