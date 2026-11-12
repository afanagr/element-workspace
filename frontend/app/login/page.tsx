'use client';

import { useEffect, useState } from 'react';

import { useAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';

function nextTarget(): string {
  if (typeof window === 'undefined') return '/';
  const next = new URLSearchParams(window.location.search).get('next');
  if (!next || !next.startsWith('/') || next.startsWith('/login')) return '/';
  return next;
}

export default function LoginPage() {
  const { t } = useI18n();
  const { user, loading, login } = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loading && user) window.location.replace(nextTarget());
  }, [loading, user]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(username.trim(), password);
      window.location.replace(nextTarget());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border bg-[rgb(var(--surface))] p-6 shadow-xl"
      >
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600 text-sm font-bold text-white">
            EW
          </div>
          <div className="leading-tight">
            <div className="text-base font-semibold">Element Workspace</div>
            <div className="text-[11px] text-slate-500 dark:text-zinc-400">{t('login.subtitle')}</div>
          </div>
        </div>

        <label className="mb-1 block text-xs font-medium text-slate-600 dark:text-zinc-400">
          {t('login.username')}
        </label>
        <input
          className="input mb-3 w-full"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoComplete="username"
          autoFocus
          required
        />

        <label className="mb-1 block text-xs font-medium text-slate-600 dark:text-zinc-400">
          {t('login.password')}
        </label>
        <input
          className="input mb-4 w-full"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          required
        />

        {error ? (
          <div className="mb-3 rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
            {error}
          </div>
        ) : null}

        <button type="submit" className="btn btn-primary w-full" disabled={busy}>
          {busy ? t('common.working') : t('login.submit')}
        </button>

        <p className="mt-4 text-[11px] leading-relaxed text-slate-500 dark:text-zinc-400">
          {t('login.hint')}
        </p>
      </form>
    </div>
  );
}
