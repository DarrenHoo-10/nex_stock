import { create } from 'zustand';
import type { Horizon } from '../types/dailyPlan';

/**
 * What the assistant drawer is looking at. Pages pass it to `openAssistant(context)`; the drawer sends it as
 * `context.daily_plan` (snake_case) to /agent/chat/stream. Names are display-only (the context chip).
 */
export interface AssistantContext {
  kind: 'board' | 'stock' | 'none';
  boardId?: string | null;
  boardName?: string | null;
  stockCode?: string | null;
  stockName?: string | null;
  horizon?: Horizon | null;
  /** The stock is a holding (picks the suggested questions). */
  held?: boolean;
  /** YYYY-MM-DD; defaults to the latest run's trade date. */
  tradeDate?: string | null;
}

export const ASSISTANT_NO_CONTEXT: AssistantContext = { kind: 'none' };

/**
 * UI flags shared across the plan shell. The shell only renders the buttons; the assistant drawer is mounted
 * once by PlanShell (components/plan/assistant) and the run-status dialog is built by the pages/features
 * that own it. Both read these flags.
 */
interface PlanUiState {
  /** Assistant drawer is open (the header button sets `aria-expanded` from this). */
  assistantOpen: boolean;
  /** What the assistant is looking at; `{ kind: 'none' }` when opened without context. */
  assistantContext: AssistantContext;
  /** Key of the context the current assistant conversation belongs to (drawer-internal bookkeeping). */
  assistantSessionKey: string | null;
  /** Run-status dialog is open (the header capsule opens it). */
  runDialogOpen: boolean;
  /** Bumped by `refreshRun()`; the run-status capsule reloads when it changes. */
  runRefreshToken: number;
  /** Opening (closed -> open) without `openAssistant` resets the context to none. */
  setAssistantOpen: (open: boolean) => void;
  toggleAssistant: () => void;
  /**
   * Open the assistant drawer, optionally with page context (board / stock / horizon). Without an argument the
   * context is `{ kind: 'none' }`. Call this from every 「问助手」 button.
   */
  openAssistant: (context?: AssistantContext) => void;
  /** Change the context while the drawer is open (the chip's remove button uses `{ kind: 'none' }`). */
  setAssistantContext: (context: AssistantContext) => void;
  setAssistantSessionKey: (key: string | null) => void;
  setRunDialogOpen: (open: boolean) => void;
  /** Ask the run-status capsule to reload (call after triggering a manual run). */
  refreshRun: () => void;
}

export const usePlanUiStore = create<PlanUiState>((set) => ({
  assistantOpen: false,
  assistantContext: ASSISTANT_NO_CONTEXT,
  assistantSessionKey: null,
  runDialogOpen: false,
  runRefreshToken: 0,
  setAssistantOpen: (open) =>
    set((state) => ({
      assistantOpen: open,
      ...(open && !state.assistantOpen ? { assistantContext: ASSISTANT_NO_CONTEXT } : {}),
    })),
  toggleAssistant: () =>
    set((state) => ({
      assistantOpen: !state.assistantOpen,
      ...(!state.assistantOpen ? { assistantContext: ASSISTANT_NO_CONTEXT } : {}),
    })),
  openAssistant: (context) =>
    set({ assistantOpen: true, assistantContext: context ?? ASSISTANT_NO_CONTEXT }),
  setAssistantContext: (context) => set({ assistantContext: context }),
  setAssistantSessionKey: (key) => set({ assistantSessionKey: key }),
  setRunDialogOpen: (open) => set({ runDialogOpen: open }),
  refreshRun: () => set((state) => ({ runRefreshToken: state.runRefreshToken + 1 })),
}));
