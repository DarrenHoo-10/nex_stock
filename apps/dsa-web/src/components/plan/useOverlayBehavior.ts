import { useEffect } from 'react';
import type { RefObject } from 'react';

let scrollLocks = 0;
let previousOverflow = '';

/** Locks body scrolling while `active`; nested overlays share a counter. */
export function useScrollLock(active: boolean): void {
  useEffect(() => {
    if (!active) {
      return undefined;
    }
    if (scrollLocks === 0) {
      previousOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
    }
    scrollLocks += 1;
    return () => {
      scrollLocks -= 1;
      if (scrollLocks === 0) {
        document.body.style.overflow = previousOverflow;
      }
    };
  }, [active]);
}

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.hasAttribute('hidden') && element.getClientRects().length > 0,
  );
}

interface OverlayFocusOptions {
  onEscape: () => void;
  /** Element to focus first; defaults to the first focusable element, then the container. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Prefer the second focusable element (skips the header close button) when there is one. */
  skipFirstFocusable?: boolean;
}

/**
 * Focus management for dialogs and drawers: moves focus in on mount, traps Tab inside the container,
 * closes on Escape and returns focus to the previously focused element on unmount.
 */
export function useOverlayFocus(containerRef: RefObject<HTMLElement | null>, options: OverlayFocusOptions): void {
  const { onEscape, initialFocusRef, skipFirstFocusable = false } = options;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return undefined;
    }
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const items = focusableIn(container);
    const first = skipFirstFocusable && items.length > 1 ? items[1] : items[0];
    (initialFocusRef?.current ?? first ?? container).focus({ preventScroll: true });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onEscape();
        return;
      }
      if (event.key !== 'Tab') {
        return;
      }
      const focusable = focusableIn(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const firstItem = focusable[0];
      const lastItem = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === firstItem || active === container)) {
        event.preventDefault();
        lastItem.focus();
      } else if (!event.shiftKey && active === lastItem) {
        event.preventDefault();
        firstItem.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown, true);
    return () => {
      document.removeEventListener('keydown', handleKeyDown, true);
      if (opener?.isConnected) {
        opener.focus({ preventScroll: true });
      }
    };
    // Focus is moved once per mount; later prop changes must not steal focus.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
