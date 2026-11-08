import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  UiLanguageProvider,
} from '../UiLanguageContext';
import {
  getRuntimeInitialLanguage,
  persistUiLanguage,
  resolveInitialUiLanguage,
  UI_LANGUAGE_STORAGE_KEY,
} from '../../utils/uiLanguage';
import { UiLanguageToggle } from '../../components/i18n/UiLanguageToggle';

function createStorage(value: string | null): Storage {
  const store = new Map<string, string>();
  if (value !== null) {
    store.set(UI_LANGUAGE_STORAGE_KEY, value);
  }

  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, nextValue: string) => {
      store.set(key, nextValue);
    },
  };
}

describe('UiLanguageContext', () => {
  it('uses an explicit stored choice and ignores the browser language', () => {
    expect(resolveInitialUiLanguage({ storage: createStorage('zh') })).toBe('zh');
    expect(resolveInitialUiLanguage({ storage: createStorage('en') })).toBe('en');
  });

  it('defaults to Chinese without a valid stored choice, whatever the browser language', () => {
    const original = Object.getOwnPropertyDescriptor(window.navigator, 'language');
    Object.defineProperty(window.navigator, 'language', { configurable: true, get: () => 'en-US' });
    try {
      expect(resolveInitialUiLanguage({ storage: createStorage('fr') })).toBe('zh');
      expect(resolveInitialUiLanguage({ storage: createStorage(null) })).toBe('zh');
      expect(resolveInitialUiLanguage()).toBe('zh');
    } finally {
      if (original) {
        Object.defineProperty(window.navigator, 'language', original);
      }
    }
  });

  it('defaults to Chinese if storage getItem throws', () => {
    const throwingStorage = createStorage('en');
    throwingStorage.getItem = () => {
      throw new Error('Storage getItem disabled');
    };

    expect(resolveInitialUiLanguage({ storage: throwingStorage })).toBe('zh');
  });

  it('persists language preference via storage in a safe, non-throwing path', () => {
    const throwingStorage = createStorage('zh');
    throwingStorage.setItem = () => {
      throw new Error('Storage setItem disabled');
    };

    expect(() => persistUiLanguage(throwingStorage, 'en')).not.toThrow();
  });

  it('falls back safely when the localStorage accessor itself throws', () => {
    const originalDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get: () => {
        throw new Error('localStorage disabled');
      },
    });

    try {
      expect(getRuntimeInitialLanguage()).toBe('zh');
    } finally {
      if (originalDescriptor) {
        Object.defineProperty(window, 'localStorage', originalDescriptor);
      }
    }
  });

  it('switches UI language immediately and persists the explicit choice', () => {
    localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, 'zh');

    render(
      <UiLanguageProvider>
        <UiLanguageToggle />
      </UiLanguageProvider>
    );

    const toggle = screen.getByRole('button', { name: '切换界面语言' });
    expect(screen.getByText('界面语言')).toBeInTheDocument();

    fireEvent.click(toggle);

    expect(localStorage.getItem(UI_LANGUAGE_STORAGE_KEY)).toBe('en');
    expect(screen.getByRole('button', { name: 'Switch UI language' })).toBeInTheDocument();
    expect(screen.getByText('English')).toBeInTheDocument();
  });
});
