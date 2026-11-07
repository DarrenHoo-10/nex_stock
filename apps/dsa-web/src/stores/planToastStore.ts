import { create } from 'zustand';

/** Default visible time of a plan toast (prototype: 2.4s). */
export const PLAN_TOAST_DURATION_MS = 2400;

export interface PlanToastItem {
  id: number;
  message: string;
}

interface PlanToastState {
  /** Only one toast is shown at a time; a new one replaces the current one. */
  toast: PlanToastItem | null;
  show: (message: string, durationMs?: number) => void;
  dismiss: () => void;
}

let nextToastId = 1;
let dismissTimer: ReturnType<typeof setTimeout> | null = null;

function clearDismissTimer() {
  if (dismissTimer !== null) {
    clearTimeout(dismissTimer);
    dismissTimer = null;
  }
}

export const usePlanToastStore = create<PlanToastState>((set) => ({
  toast: null,
  show: (message, durationMs = PLAN_TOAST_DURATION_MS) => {
    clearDismissTimer();
    const id = nextToastId++;
    set({ toast: { id, message } });
    dismissTimer = setTimeout(() => {
      dismissTimer = null;
      set((state) => (state.toast?.id === id ? { toast: null } : state));
    }, durationMs);
  },
  dismiss: () => {
    clearDismissTimer();
    set({ toast: null });
  },
}));

/** Show a toast from anywhere (outside React too). */
export function showPlanToast(message: string, durationMs?: number): void {
  usePlanToastStore.getState().show(message, durationMs);
}
