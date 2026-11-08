import { create } from 'zustand';
import type { ChatStreamRequest } from '../api/agent';

const STORAGE_KEY = 'dsa_assistant_prefs';

/** '' = follow the server configuration (AGENT_LITELLM_MODEL / AGENT_REASONING_EFFORT). */
export interface AssistantPrefs {
  model: string;
  reasoningEffort: string;
}

interface AssistantPrefsState extends AssistantPrefs {
  setModel: (model: string) => void;
  setReasoningEffort: (effort: string) => void;
}

function readPrefs(): AssistantPrefs {
  try {
    const raw = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null;
    const parsed = raw ? (JSON.parse(raw) as Partial<AssistantPrefs>) : {};
    return {
      model: typeof parsed.model === 'string' ? parsed.model : '',
      reasoningEffort: typeof parsed.reasoningEffort === 'string' ? parsed.reasoningEffort : '',
    };
  } catch {
    return { model: '', reasoningEffort: '' };
  }
}

function writePrefs(prefs: AssistantPrefs): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Private mode / quota: the choice still applies for this page load.
  }
}

/** Model and reasoning effort chosen for assistant chat; shared by the assistant page and drawer. */
export const useAssistantPrefsStore = create<AssistantPrefsState>((set, get) => ({
  ...readPrefs(),
  setModel: (model) => {
    set({ model });
    writePrefs({ model, reasoningEffort: get().reasoningEffort });
  },
  setReasoningEffort: (reasoningEffort) => {
    set({ reasoningEffort });
    writePrefs({ model: get().model, reasoningEffort });
  },
}));

/** Request fields for the current choice; empty choices are omitted so the server default applies. */
export function assistantRequestOverrides(prefs: AssistantPrefs): Pick<ChatStreamRequest, 'model' | 'reasoning_effort'> {
  return {
    ...(prefs.model ? { model: prefs.model } : {}),
    ...(prefs.reasoningEffort ? { reasoning_effort: prefs.reasoningEffort } : {}),
  };
}
