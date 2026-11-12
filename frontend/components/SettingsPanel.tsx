'use client';

import { useCallback, useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useRequireAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type { SettingsResponse } from '@/lib/types';

export function SettingsPanel() {
  const { t, locale, setLocale } = useI18n();
  const auth = useRequireAuth();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [test, setTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [embeddingTest, setEmbeddingTest] = useState<{ ok: boolean; message: string } | null>(null);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');

  const load = useCallback(async () => {
    try {
      setSettings(await api.settings());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const deepseek = settings?.providers.find((item) => item.name === 'deepseek') ?? null;

  const runTest = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.testProvider('deepseek');
      setTest({ ok: result.ok, message: result.message });
    } catch {
      setTest({ ok: false, message: t('settings.testFailed') });
    } finally {
      setBusy(false);
    }
  };

  const runEmbeddingTest = async () => {
    setBusy(true);
    try {
      const result = await api.testEmbeddings('local');
      setEmbeddingTest({
        ok: result.ok,
        message: result.ok
          ? `${result.model} · ${result.dimension} dims`
          : result.message || t('settings.testFailed'),
      });
    } catch (err) {
      setEmbeddingTest({ ok: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  };

  const savePassword = async () => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await api.changePassword(currentPassword, newPassword);
      setCurrentPassword('');
      setNewPassword('');
      setNotice(t('settings.passwordChanged'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading || auth.loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        {t('settings.loading')}
      </div>
    );
  }

  if (!auth.user) {
    return null;
  }

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-3xl flex-col gap-4 px-4 py-6">
      <header className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-base font-semibold">{t('settings.title')}</h1>
          <p className="text-[11px] text-slate-500 dark:text-zinc-400">
            {auth.user.display_name || auth.user.username} · @{auth.user.username} ·{' '}
            {auth.user.is_admin ? t('admin.roleAdmin') : t('admin.roleUser')}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {auth.user.is_admin ? (
            <a className="btn" href="/admin">
              {t('app.admin')}
            </a>
          ) : null}
          <a className="btn" href="/">
            {t('common.back')}
          </a>
        </div>
      </header>

      {error ? (
        <div className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
          {notice}
        </div>
      ) : null}

      {settings?.secret_key_insecure ? (
        <div className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          {t('settings.secretWarning')}
        </div>
      ) : null}
      {settings?.admin_password_is_default ? (
        <div className="rounded-lg bg-amber-50 px-3 py-2 text-[11px] text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          {t('settings.defaultPasswordWarning')}
        </div>
      ) : null}

      <section className="panel rounded-xl p-4">
        <h2 className="mb-1 text-sm font-semibold">{t('settings.deepseekTitle')}</h2>
        <p className="mb-3 text-[11px] text-slate-500 dark:text-zinc-400">
          {t('settings.deepseekNote')}
        </p>
        <dl className="grid gap-2 text-xs sm:grid-cols-2">
          <div>
            <dt className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('settings.keyStatus')}
            </dt>
            <dd className="font-medium">
              {auth.user.deepseek_configured ? (
                <>
                  ✓ {t('settings.keyAssigned')}{' '}
                  <span className="font-mono text-[11px]">{auth.user.deepseek_key_masked}</span>
                </>
              ) : (
                <span className="text-amber-600 dark:text-amber-400">
                  {t('settings.keyMissing')}
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('settings.model')}
            </dt>
            <dd className="font-medium">{settings?.default_model || deepseek?.default_model}</dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('settings.embeddingProvider')}
            </dt>
            <dd className="font-medium">
              {t('settings.embeddingLocal')}
              {settings?.embedding_providers[0]?.configured ? '' : ` (${t('admin.noKey')})`}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('settings.maxUpload')}
            </dt>
            <dd className="font-medium">{settings?.max_upload_mb ?? 0} MB</dd>
          </div>
        </dl>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button type="button" className="btn" disabled={busy} onClick={() => void runTest()}>
            {busy ? t('settings.checking') : t('settings.checkDeepseek')}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => void runEmbeddingTest()}>
            {t('settings.testEmbeddings')}
          </button>
        </div>
        {test ? (
          <p
            className={`mt-2 text-[11px] ${
              test.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400'
            }`}
          >
            {test.ok ? '✓' : '✕'} {test.message}
          </p>
        ) : null}
        {embeddingTest ? (
          <p
            className={`mt-1 text-[11px] ${
              embeddingTest.ok
                ? 'text-emerald-600 dark:text-emerald-400'
                : 'text-rose-600 dark:text-rose-400'
            }`}
          >
            {embeddingTest.ok ? '✓' : '✕'} {embeddingTest.message}
          </p>
        ) : null}
      </section>

      <section className="panel rounded-xl p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('settings.passwordTitle')}</h2>
        <div className="grid gap-2 sm:grid-cols-2">
          <input
            className="input"
            type="password"
            placeholder={t('settings.currentPassword')}
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
            autoComplete="current-password"
          />
          <input
            className="input"
            type="password"
            placeholder={t('settings.newPassword')}
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            autoComplete="new-password"
          />
        </div>
        <button
          type="button"
          className="btn btn-primary mt-3"
          disabled={busy || !currentPassword || newPassword.length < 6}
          onClick={() => void savePassword()}
        >
          {t('settings.changePassword')}
        </button>
      </section>

      <section className="panel rounded-xl p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('settings.interfaceTitle')}</h2>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-slate-500 dark:text-zinc-400">{t('settings.language')}</span>
          <button
            type="button"
            className={`btn ${locale === 'ru' ? 'btn-primary' : ''}`}
            onClick={() => setLocale('ru')}
          >
            {t('settings.languageRu')}
          </button>
          <button
            type="button"
            className={`btn ${locale === 'en' ? 'btn-primary' : ''}`}
            onClick={() => setLocale('en')}
          >
            {t('settings.languageEn')}
          </button>
        </div>
      </section>

      <div className="flex justify-end">
        <button type="button" className="btn" onClick={() => void auth.logout()}>
          {t('app.logout')}
        </button>
      </div>
    </div>
  );
}

