import type React from 'react';

/** Stroke icon paths from the prototype (24x24 viewBox). */
const ICON_PATHS = {
  boards: <path d="M5 20v-7M12 20V5M19 20v-10" />,
  picks: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </>
  ),
  mine: (
    <>
      <rect x="3.5" y="7.5" width="17" height="12" rx="3" />
      <path d="M9 7.5V6a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v1.5M3.5 13h17" />
    </>
  ),
  settings: (
    <>
      <path d="M4 7h9M17 7h3M4 17h3M11 17h9" />
      <circle cx="15" cy="7" r="2" />
      <circle cx="9" cy="17" r="2" />
    </>
  ),
  assist: <path d="M12 3.5l1.9 5 5.1 1.9-5.1 1.9-1.9 5-1.9-5-5.1-1.9 5.1-1.9z" />,
  more: (
    <>
      <circle cx="6" cy="12" r="1.2" />
      <circle cx="12" cy="12" r="1.2" />
      <circle cx="18" cy="12" r="1.2" />
    </>
  ),
  close: <path d="M6 6l12 12M18 6L6 18" />,
} satisfies Record<string, React.ReactNode>;

export type PlanIconName = keyof typeof ICON_PATHS;

/** Decorative icon (aria-hidden); always pair with a text label or an aria-label on the button. */
export const PlanIcon: React.FC<{ name: PlanIconName; size?: number }> = ({ name, size = 22 }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={1.7}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {ICON_PATHS[name]}
  </svg>
);
