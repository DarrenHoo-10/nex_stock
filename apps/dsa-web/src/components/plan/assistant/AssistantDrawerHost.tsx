import type React from 'react';
import { lazy, Suspense, useState } from 'react';
import { usePlanUiStore } from '../../../stores/planUiStore';

const AssistantDrawer = lazy(() =>
  import('./AssistantDrawer').then((module) => ({ default: module.AssistantDrawer })),
);

/**
 * Mounted once by PlanShell. The drawer chunk (markdown renderer, chat store) loads on the first open and
 * stays mounted afterwards so the exit animation and the conversation survive closing.
 */
export const AssistantDrawerHost: React.FC = () => {
  const open = usePlanUiStore((state) => state.assistantOpen);
  const [mounted, setMounted] = useState(open);
  if (open && !mounted) {
    setMounted(true);
  }
  return mounted ? (
    <Suspense fallback={null}>
      <AssistantDrawer />
    </Suspense>
  ) : null;
};
