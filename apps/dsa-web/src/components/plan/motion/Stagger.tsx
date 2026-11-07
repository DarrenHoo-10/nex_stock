import type React from 'react';
import { Children } from 'react';
import { motion } from 'motion/react';
import { PLAN_SPRING, PLAN_STAGGER_GAP } from './springs';
import { usePlanReducedMotion } from './usePlanMotion';

type StaggerTag = 'div' | 'li' | 'section' | 'ul' | 'ol' | 'article' | 'header' | 'span' | 'p';

const MOTION_TAGS: Record<StaggerTag, React.ElementType> = {
  div: motion.div,
  li: motion.li,
  section: motion.section,
  ul: motion.ul,
  ol: motion.ol,
  article: motion.article,
  header: motion.header,
  span: motion.span,
  p: motion.p,
};

export interface StaggerItemProps {
  /** Position in the sequence; delay = min(index, max) * gap. */
  index?: number;
  /** Seconds between items, default 0.045. */
  gap?: number;
  /** Items beyond this index share the last delay, default 8. */
  max?: number;
  /** Initial vertical offset in px, default 10. */
  y?: number;
  as?: StaggerTag;
  className?: string;
  children?: React.ReactNode;
}

/** One element that fades and rises in (opacity + small y) after `index * gap` seconds. */
export const StaggerItem: React.FC<StaggerItemProps> = ({
  index = 0,
  gap = PLAN_STAGGER_GAP,
  max = 8,
  y = 10,
  as = 'div',
  className,
  children,
}) => {
  const reduced = usePlanReducedMotion();
  const Component = MOTION_TAGS[as];
  if (reduced) {
    return <Component className={className}>{children}</Component>;
  }
  return (
    <Component
      className={className}
      initial={{ opacity: 0, y }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ ...PLAN_SPRING, delay: Math.min(index, max) * gap }}
    >
      {children}
    </Component>
  );
};

export interface StaggerProps extends Omit<StaggerItemProps, 'index' | 'as'> {
  /** Container element (plain, not animated). Default "div". */
  as?: 'div' | 'ul' | 'ol' | 'section';
  /** Element wrapping each child. Use "li" with as="ul". Default "div". */
  itemAs?: StaggerTag;
  itemClassName?: string;
}

/**
 * Entrance helper: wraps each direct child in a StaggerItem so they appear one after another.
 * Re-mount (change `key`) to replay, e.g. when the selected stock changes.
 */
export const Stagger: React.FC<StaggerProps> = ({
  as = 'div',
  itemAs = 'div',
  itemClassName,
  className,
  gap,
  max,
  y,
  children,
}) => {
  const Container = as;
  return (
    <Container className={className}>
      {Children.toArray(children).map((child, index) => (
        <StaggerItem key={index} index={index} gap={gap} max={max} y={y} as={itemAs} className={itemClassName}>
          {child}
        </StaggerItem>
      ))}
    </Container>
  );
};
