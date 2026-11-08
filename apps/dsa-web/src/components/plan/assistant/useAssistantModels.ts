import { useEffect, useState } from 'react';
import { agentApi } from '../../../api/agent';
import type { AgentModelsResponse } from '../../../api/agent';
import { useAssistantPrefsStore } from '../../../stores/assistantPrefsStore';

export type AssistantModelsState =
  | { status: 'loading' }
  | { status: 'ready'; data: AgentModelsResponse; routeModels: string[] }
  | { status: 'error' };

let cached: Promise<AgentModelsResponse> | null = null;

/** Test hook: forget the cached GET /agent/models result. */
export function resetAssistantModelsCache(): void {
  cached = null;
}

function routeModelsOf(data: AgentModelsResponse): string[] {
  const seen = new Set<string>();
  for (const item of data.models) {
    const name = item.route_model || item.model;
    if (name) {
      seen.add(name);
    }
  }
  return [...seen];
}

/**
 * Selectable assistant models (GET /agent/models, fetched once per page load). A saved model or effort
 * the server no longer offers is reset to "follow configuration", so stale choices never cause a 400.
 */
export function useAssistantModels(): AssistantModelsState {
  const [state, setState] = useState<AssistantModelsState>({ status: 'loading' });

  useEffect(() => {
    let alive = true;
    cached ??= agentApi.getModels();
    cached.then(
      (data) => {
        const routeModels = routeModelsOf(data);
        const prefs = useAssistantPrefsStore.getState();
        if (prefs.model && !routeModels.includes(prefs.model)) {
          prefs.setModel('');
        }
        const efforts = ['none', ...data.reasoning_effort_options];
        if (prefs.reasoningEffort && (routeModels.length === 0 || !efforts.includes(prefs.reasoningEffort))) {
          prefs.setReasoningEffort('');
        }
        if (alive) {
          setState({ status: 'ready', data, routeModels });
        }
      },
      () => {
        cached = null;
        if (alive) {
          setState({ status: 'error' });
        }
      },
    );
    return () => {
      alive = false;
    };
  }, []);

  return state;
}
