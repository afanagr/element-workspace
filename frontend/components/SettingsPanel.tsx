'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { ProviderInfo, SettingsResponse } from '@/lib/types';

interface Draft {
  api_key: string;
  base_url: string;
  default_model: string;
}

interface TestResult {
  ok: boolean;
  message: string;
  latency_ms?: number;
}

export function SettingsPanel() {
  const { locale, setLocale, t } = useI18n();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: 'ok' | 'error' } | null>(null);
  const [embeddingTest, setEmbeddingTest] = useState<TestResult | null>(null);
  const [prefs, setPrefs] = useState({
    provider: '',
    model: '',
    embedding_provider: '',
    embedding_model: '',
  });

  const load = useCallback(async () => {
    const data = await api.settings();
    setSettings(data);
    setPrefs({
      provider: data.preferences.provider,
      model: data.preferences.model,
      embedding_provider: data.preferences.embedding_provider,
      embedding_model: data.preferences.embedding_model,
    });
    setDrafts((prev) => {
      const next: Record<string, Draft> = { ...prev };
      for (const provider of data.providers) {
        next[provider.name] = next[provider.name] ?? {
          api_key: '',
          base_url: provider.base_url ?? '',
          default_model: provider.default_model,
        };
      }
      return next;
    });
  }, []);

  useEffect(() => {
    void load().catch((error) => setNotice({ text: String(error), tone: 'error' }));
  }, [load]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 6000);
    return () => clearTimeout(timer);
  }, [notice]);

  const providers = useMemo(() => settings?.providers ?? [], [settings]);

  const embeddingLabel = (name: string) => {
    if (name === 'local') return t('settings.embeddingLocal');
    if (name === 'ollama') return t('settings.embeddingOllama');
    if (name === 'gemini') return 'Google Gemini';
    return 'OpenAI';
  };

  const updateDraft = (name: string, patch: Partial<Draft>) => {
    setDrafts((prev) => ({
      ...prev,
      [name]: { ...(prev[name] ?? { api_key: '', base_url: '', default_model: '' }), ...patch },
    }));
  };

  const save = async (provider: ProviderInfo) => {
    const draft = drafts[provider.name];
    if (!draft) return;
    setBusy(`save-${provider.name}`);
    try {
      await api.saveCredential({
        provider: provider.name,
        ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
        base_url: draft.base_url.trim(),
        default_model: draft.default_model.trim(),
      });
      updateDraft(provider.name, { api_key: '' });
      await load();
      setNotice({ text: t('settings.noticeSaved', { name: provider.label }), tone: 'ok' });
    } catch (error) {
      setNotice({ text: error instanceof Error ? error.message : String(error), tone: 'error' });
    } finally {
      setBusy(null);
    }
  };

  const connect = async (provider: ProviderInfo) => {
    setBusy(`test-${provider.name}`);
    try {
      const result = await api.testProvider(provider.name);
      setTests((prev) => ({ ...prev, [provider.name]: result }));
      setNotice({
        text: `${provider.label}: ${result.message}`,
        tone: result.ok ? 'ok' : 'error',
      });
    } catch (error) {
      setTests((prev) => ({
        ...prev,
        [provider.name]: { ok: false, message: String(error) },
      }));
    } finally {
      setBusy(null);
    }
  };

  const savePreferences = async () => {
    setBusy('prefs');
    try {
      const result = await api.savePreferences(prefs);
      setNotice({
        text: result.reindex_required ? t('settings.noticeReindex') : t('settings.noticePrefs'),
        tone: 'ok',
      });
      await load();
    } catch (error) {
      setNotice({ text: error instanceof Error ? error.message : String(error), tone: 'error' });
    } finally {
      setBusy(null);
    }
  };

  const testEmbeddings = async () => {
    setBusy('embeddings');
    setEmbeddingTest(null);
    try {
      const result = await api.testEmbeddings(
        prefs.embedding_provider || undefined,
        prefs.embedding_model || undefined,
      );
      setEmbeddingTest({
        ok: result.ok,
        message: result.ok
          ? `${result.provider}/${result.model} · ${result.dimension} dimensions`
          : result.message || 'Failed',
      });
    } catch (error) {
      setEmbeddingTest({ ok: false, message: error instanceof Error ? error.message : String(error) });
    } finally {
      setBusy(null);
    }
  };

  const statusDot = (provider: ProviderInfo) => {
    const test = tests[provider.name];
    if (test) return test.ok ? '🟢' : '🔴';
    return provider.configured ? '⚪' : '⚪';
  };

  if (!settings) {
    return <div className="p-6 text-sm text-slate-500">{t('settings.loading')}</div>;
  }

  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <a className="btn" href="/">
          {t('common.back')}
        </a>
        <h1 className="text-base font-semibold">{t('settings.title')}</h1>
        <span className="chip">
          {t('settings.providersCount', {
            count: providers.filter((item) => item.configured).length,
          })}
        </span>
        <label className="ml-auto flex items-center gap-1 text-[11px] text-slate-500 dark:text-zinc-400">
          {t('settings.language')}
          <select
            className="input w-24 py-1 text-xs"
            value={locale}
            onChange={(event) => setLocale(event.target.value === 'en' ? 'en' : 'ru')}
          >
            <option value="ru">{t('settings.languageRu')}</option>
            <option value="en">{t('settings.languageEn')}</option>
          </select>
        </label>
        <button type="button" className="btn" onClick={() => void load()}>
          {t('common.reload')}
        </button>
      </div>

      {settings.secret_key_insecure ? (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          {t('settings.secretWarning')}
        </div>
      ) : null}

      <section className="panel rounded-xl p-3">
        <div className="mb-2 text-sm font-semibold">{t('settings.defaults')}</div>
        <div className="grid gap-2 sm:grid-cols-2">
          <label className="text-[11px] text-slate-500 dark:text-zinc-400">
            {t('settings.chatProvider')}
            <select
              className="input mt-0.5"
              value={prefs.provider}
              onChange={(event) => {
                const name = event.target.value;
                const provider = providers.find((item) => item.name === name);
                setPrefs((prev) => ({
                  ...prev,
                  provider: name,
                  model: provider ? provider.default_model : '',
                }));
              }}
            >
              {providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.label}
                  {provider.configured ? '' : t('settings.noKey')}
                </option>
              ))}
            </select>
          </label>
          <label className="text-[11px] text-slate-500 dark:text-zinc-400">
            {t('settings.defaultModel')}
            <input
              className="input mt-0.5"
              list="settings-model-list"
              value={prefs.model}
              onChange={(event) => setPrefs((prev) => ({ ...prev, model: event.target.value }))}
            />
            <datalist id="settings-model-list">
              {(providers.find((item) => item.name === prefs.provider)?.models ?? []).map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </label>
          <label className="text-[11px] text-slate-500 dark:text-zinc-400">
            {t('settings.embeddingProvider')}
            <select
              className="input mt-0.5"
              value={prefs.embedding_provider}
              onChange={(event) => {
                const name = event.target.value;
                const embedded = settings.embedding_providers.find((item) => item.name === name);
                setPrefs((prev) => ({
                  ...prev,
                  embedding_provider: name,
                  embedding_model: embedded?.models[0] ?? '',
                }));
              }}
            >
              {settings.embedding_providers.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {embeddingLabel(provider.name)}
                  {provider.configured ? '' : t('settings.notConfiguredSuffix')}
                </option>
              ))}
            </select>
          </label>
          <label className="text-[11px] text-slate-500 dark:text-zinc-400">
            {t('settings.embeddingModel')}
            <input
              className="input mt-0.5"
              list="settings-embedding-list"
              value={prefs.embedding_model}
              onChange={(event) =>
                setPrefs((prev) => ({ ...prev, embedding_model: event.target.value }))
              }
            />
            <datalist id="settings-embedding-list">
              {(
                settings.embedding_providers.find((item) => item.name === prefs.embedding_provider)
                  ?.models ?? []
              ).map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </label>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy === 'prefs'}
            onClick={() => void savePreferences()}
          >
            {busy === 'prefs' ? t('settings.saving') : t('settings.saveDefaults')}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy === 'embeddings'}
            onClick={() => void testEmbeddings()}
          >
            {busy === 'embeddings' ? t('settings.testing') : t('settings.testEmbeddings')}
          </button>
          {embeddingTest ? (
            <span
              className={`text-[11px] ${embeddingTest.ok ? 'text-emerald-600' : 'text-rose-600'}`}
            >
              {embeddingTest.ok ? '✅' : '❌'} {embeddingTest.message}
            </span>
          ) : null}
        </div>
        <p className="mt-2 text-[10px] text-slate-500 dark:text-zinc-500">
          {t('settings.embeddingNote')}
        </p>
      </section>

      {notice ? (
        <div
          className={`rounded-lg border px-3 py-2 text-xs ${
            notice.tone === 'ok'
              ? 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200'
              : 'border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-200'
          }`}
        >
          {notice.text}
        </div>
      ) : null}

      <section className="space-y-2">
        <div className="text-sm font-semibold">{t('settings.providers')}</div>
        <p className="text-[11px] text-slate-500 dark:text-zinc-400">
          {t('settings.providersNote')}
        </p>

        {providers.map((provider) => {
          const draft = drafts[provider.name] ?? {
            api_key: '',
            base_url: provider.base_url ?? '',
            default_model: provider.default_model,
          };
          const test = tests[provider.name];
          return (
            <div key={provider.name} className="panel rounded-xl p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span title="connection status">{statusDot(provider)}</span>
                <span className="text-sm font-medium">{provider.label}</span>
                <span className="chip">{provider.source}</span>
                {provider.supports_vision ? <span className="chip">vision</span> : null}
                {provider.supports_tools ? <span className="chip">tools</span> : null}
                {provider.supports_embeddings ? <span className="chip">embeddings</span> : null}
                <span className="ml-auto text-[10px] text-slate-500">
                  {provider.configured
                    ? provider.api_key_masked || t('settings.configured')
                    : t('settings.notConfigured')}
                </span>
              </div>

              <div className="mt-2 grid gap-2 sm:grid-cols-3">
                <label className="text-[11px] text-slate-500 dark:text-zinc-400">
                  {t('settings.apiKey')}
                  <input
                    type="password"
                    autoComplete="off"
                    className="input mt-0.5"
                    placeholder={
                      provider.api_key_masked ||
                      (provider.local ? t('settings.notRequired') : 'sk-…')
                    }
                    value={draft.api_key}
                    onChange={(event) => updateDraft(provider.name, { api_key: event.target.value })}
                  />
                </label>
                <label className="text-[11px] text-slate-500 dark:text-zinc-400">
                  {t('settings.baseUrl')}
                  <input
                    className="input mt-0.5"
                    placeholder={provider.base_url ?? 'default'}
                    value={draft.base_url}
                    onChange={(event) => updateDraft(provider.name, { base_url: event.target.value })}
                  />
                </label>
                <label className="text-[11px] text-slate-500 dark:text-zinc-400">
                  {t('settings.defaultModel')}
                  <input
                    className="input mt-0.5"
                    value={draft.default_model}
                    onChange={(event) =>
                      updateDraft(provider.name, { default_model: event.target.value })
                    }
                  />
                </label>
              </div>

              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy === `save-${provider.name}`}
                  onClick={() => void save(provider)}
                >
                  {busy === `save-${provider.name}` ? t('settings.saving') : t('settings.save')}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={busy === `test-${provider.name}`}
                  onClick={() => void connect(provider)}
                >
                  {busy === `test-${provider.name}` ? t('settings.checking') : t('settings.check')}
                </button>
                {test ? (
                  <span className={`text-[11px] ${test.ok ? 'text-emerald-600' : 'text-rose-600'}`}>
                    {test.ok ? '✅' : '❌'} {test.message}
                    {typeof test.latency_ms === 'number'
                      ? ` · ${Math.round(test.latency_ms)} ms`
                      : ''}
                  </span>
                ) : null}
              </div>

              {provider.models.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-1">
                  {provider.models.slice(0, 8).map((name) => (
                    <button
                      key={name}
                      type="button"
                      className="chip hover:border-indigo-400"
                      onClick={() => updateDraft(provider.name, { default_model: name })}
                      title={t('settings.useAsDefaultTip')}
                    >
                      {name}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          );
        })}
      </section>

      <section className="panel rounded-xl p-3 text-[11px] text-slate-500 dark:text-zinc-400">
        <div className="mb-1 text-sm font-semibold text-[rgb(var(--text))]">
          {t('settings.authTitle')}
        </div>
        {settings.auth_enabled ? t('settings.authEnabled') : t('settings.authDisabled')}
      </section>
    </div>
  );
}
