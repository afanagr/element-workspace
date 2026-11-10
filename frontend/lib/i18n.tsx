'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { en } from './locales/en';
import { ru } from './locales/ru';

export type Locale = 'ru' | 'en';

const dictionaries: Record<Locale, Record<string, string>> = { ru, en };

interface I18nValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue | null>(null);

export const LOCALE_STORAGE_KEY = 'afw-locale';

export function I18nProvider({ children }: { children: React.ReactNode }) {
  // Russian is the default interface language; a stored choice wins.
  const [locale, setLocaleState] = useState<Locale>('ru');

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY);
      if (stored === 'ru' || stored === 'en') setLocaleState(stored);
    } catch {
      /* storage may be blocked */
    }
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    try {
      window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    } catch {
      /* ignore */
    }
  }, []);

  const t = useCallback(
    (key: string, params?: Record<string, string | number>): string => {
      const dictionary = dictionaries[locale] ?? ru;
      let value = dictionary[key] ?? dictionaries.en[key] ?? key;
      if (params) {
        for (const [name, replacement] of Object.entries(params)) {
          value = value.split(`{${name}}`).join(String(replacement));
        }
      }
      return value;
    },
    [locale],
  );

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const context = useContext(I18nContext);
  if (!context) throw new Error('useI18n must be used inside I18nProvider');
  return context;
}
