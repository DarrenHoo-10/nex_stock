import { useCallback, useEffect, useRef, useState } from 'react';
import { getParsedApiError } from '../../../api/error';
import type { ParsedApiError } from '../../../api/error';
import { systemConfigApi, SystemConfigConflictError, SystemConfigValidationError } from '../../../api/systemConfig';
import type { ConfigValidationIssue, SystemConfigItem } from '../../../types/systemConfig';

/** Read-only view over GET /system/config. Never exposes more than the lookups below. */
export interface ConfigView {
  version: string;
  /** LiteLLM provider prefixes reported by the backend (used to normalise `provider/model` ids). */
  providers: string[];
  items: SystemConfigItem[];
  get: (key: string) => SystemConfigItem | undefined;
  /** Displayed value; '' when the key is not present. */
  value: (key: string) => string;
  /** Schema default (`defaultValue`) when the key is registered. */
  defaultOf: (key: string) => string | undefined;
  /** The key is saved in the config and not empty (works for masked secrets without reading them). */
  isSet: (key: string) => boolean;
}

export type PreferencesConfigState =
  | { status: 'loading' }
  | { status: 'error'; error: ParsedApiError }
  | { status: 'ready'; view: ConfigView };

export type SaveOutcome =
  | { ok: true }
  | { ok: false; kind: 'validation'; message: string; issues: ConfigValidationIssue[] }
  | { ok: false; kind: 'conflict'; message: string }
  | { ok: false; kind: 'error'; message: string };

export function buildConfigView(
  items: SystemConfigItem[],
  version: string,
  providers: string[] = [],
): ConfigView {
  const byKey = new Map(items.map((item) => [item.key, item]));
  return {
    version,
    providers,
    items,
    get: (key) => byKey.get(key),
    value: (key) => byKey.get(key)?.value ?? '',
    defaultOf: (key) => {
      const value = byKey.get(key)?.schema?.defaultValue;
      return value === null || value === undefined ? undefined : String(value);
    },
    isSet: (key) => {
      const item = byKey.get(key);
      return Boolean(item && item.rawValueExists && item.value.trim() !== '');
    },
  };
}

/**
 * Loads the system config once for the settings page and saves changed keys with the config version
 * (400 -> field issues, 409 -> `conflict`; the caller offers a reload). Keys that the backend schema does not
 * know yet are treated as plain values.
 */
export function usePreferencesConfig() {
  const [state, setState] = useState<PreferencesConfigState>({ status: 'loading' });
  const viewRef = useRef<ConfigView | null>(null);

  const fetchConfig = useCallback(async (): Promise<PreferencesConfigState> => {
    try {
      const response = await systemConfigApi.getConfig(true);
      const view = buildConfigView(response.items, response.configVersion, response.llmModelProviders ?? []);
      viewRef.current = view;
      return { status: 'ready', view };
    } catch (error) {
      return { status: 'error', error: getParsedApiError(error) };
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetchConfig().then((next) => {
      if (!cancelled) {
        setState(next);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [fetchConfig]);

  const reload = useCallback(async () => {
    setState({ status: 'loading' });
    setState(await fetchConfig());
  }, [fetchConfig]);

  const save = useCallback(
    async (items: Array<{ key: string; value: string }>): Promise<SaveOutcome> => {
      const view = viewRef.current;
      if (!view) {
        return { ok: false, kind: 'error', message: '配置还没有加载完成。' };
      }
      try {
        await systemConfigApi.update({ configVersion: view.version, items, reloadNow: true });
      } catch (error) {
        if (error instanceof SystemConfigValidationError) {
          return { ok: false, kind: 'validation', message: error.message, issues: error.issues };
        }
        if (error instanceof SystemConfigConflictError) {
          return { ok: false, kind: 'conflict', message: error.message };
        }
        return { ok: false, kind: 'error', message: getParsedApiError(error).message };
      }
      setState(await fetchConfig());
      return { ok: true };
    },
    [fetchConfig],
  );

  return { state, reload, save };
}
