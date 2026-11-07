import type React from 'react';
import { createPortal } from 'react-dom';

/**
 * Renders overlays into <body> inside their own `.plan-shell` wrapper so the scoped glass styles apply.
 * Portaling also escapes ancestors with `backdrop-filter`/`transform`, which would otherwise become the
 * containing block of `position: fixed` overlays.
 */
export const PlanPortal: React.FC<{ children: React.ReactNode }> = ({ children }) =>
  createPortal(<div className="plan-shell plan-portal">{children}</div>, document.body);
