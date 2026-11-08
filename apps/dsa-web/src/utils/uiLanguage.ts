import type { UiLanguage } from '../i18n/uiText';

export const UI_LANGUAGE_STORAGE_KEY = 'dsa.uiLanguage';

export function normalizeUiLanguage(value?: string | null): UiLanguage | null {
  if (value === 'zh' || value === 'en') {
    return value;
  }
  return null;
}

function getStoredUiLanguage(storage?: Storage | null): UiLanguage | null {
  if (!storage) {
    return null;
  }

  try {
    return normalizeUiLanguage(storage.getItem(UI_LANGUAGE_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function getUiLanguageStorage(): Storage | null {
  if (typeof window === 'undefined') {
    return null;
  }

  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function persistUiLanguage(storage: Storage | null, language: UiLanguage): void {
  if (!storage) {
    return;
  }

  try {
    storage.setItem(UI_LANGUAGE_STORAGE_KEY, language);
  } catch {
    // Ignore storage failures; in-memory language still updates.
  }
}

/** Default UI language. The browser language is not consulted: the product UI is Chinese unless the user switches. */
export const DEFAULT_UI_LANGUAGE: UiLanguage = 'zh';

export function resolveInitialUiLanguage({
  storage,
}: {
  storage?: Storage | null;
} = {}): UiLanguage {
  return getStoredUiLanguage(storage) ?? DEFAULT_UI_LANGUAGE;
}

export function getRuntimeInitialLanguage(): UiLanguage {
  if (typeof window === 'undefined') {
    return DEFAULT_UI_LANGUAGE;
  }

  return resolveInitialUiLanguage({ storage: getUiLanguageStorage() });
}
