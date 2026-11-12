'use client';

import { useEffect, useState } from 'react';

import { useAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type { HealthResponse } from '@/lib/types';

interface TopBarProps {
  health: HealthResponse | null;
  onRefreshHealth: () => void;
  activeProvider: string | null;
  activeModel: string | null;
  indexSummary?: string;
  projectName?: string | null;
  viewingOwner?: string | null;
}

export function TopBar({
  health,
  onRefreshHealth,
  activeProvider,
  activeModel,
  indexSummary,
  projectName,
  viewingOwner,
}: TopBarProps) {
  const { locale, setLocale, t } = useI18n();
  const { user, logout } = useAuth();
  const [dark, setDark] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.classList.contains('dark'));
  }, []);

  const toggleTheme = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle('dark', next);
    try {
      localStorage.setItem('afw-theme', next ? 'dark' : 'light');
    } catch {
      /* storage may be blocked */
    }
  };

  const status = health?.status ?? 'unknown';
  const statusLabel = t(`status.${status}`);
  const dot =
    status === 'ok'
      ? 'bg-emerald-500'
      : status === 'degraded'
        ? 'bg-amber-500'
        : status === 'error'
          ? 'bg-rose-500'
          : 'bg-slate-400';

  const providerLabel = activeProvider
    ? `${activeProvider}${activeModel ? ` · ${activeModel}` : ''}`
    : health?.providers.configured.length
      ? t('app.defaultProvider', {
          provider: health.providers.default_provider,
          model: health.providers.default_model,
        })
      : t('app.noProvider');

  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b px-3">
      <div className="flex items-center gap-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 text-sm font-bold text-white">
          EW
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">Element Workspace</div>
          <div className="text-[10px] text-slate-500 dark:text-zinc-400">{t('app.subtitle')}</div>
        </div>
      </div>

      <div className="ml-2 hidden items-center gap-2 md:flex">
        <button
          type="button"
          onClick={onRefreshHealth}
          title={t('app.healthRecheck')}
          className="chip"
        >
          <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
          {statusLabel}
          {health ? <span className="opacity-60">v{health.version}</span> : null}
        </button>
        <span className="chip" title={t('app.activeProvider')}>
          ⚡ {providerLabel}
        </span>
        {projectName ? (
          <span className="chip" title={t('app.activeProjectTip')}>
            📁 {projectName}
          </span>
        ) : null}
        {viewingOwner ? (
          <a className="chip text-amber-600 dark:text-amber-400" href="/" title={t('app.viewingOtherUser')}>
            👁 {t('app.viewingOtherUser')}
          </a>
        ) : null}
        {indexSummary ? <span className="chip">🗂 {indexSummary}</span> : null}
        {health?.providers.embedding_configured.length === 0 ? (
          <span className="chip text-amber-600 dark:text-amber-400" title="RAG disabled">
            {t('app.ragOff')}
          </span>
        ) : null}
      </div>

      <div className="ml-auto flex items-center gap-1.5">
        {user?.is_admin ? (
          <a className="btn btn-ghost" href="/admin" title={t('admin.title')}>
            {t('app.admin')}
          </a>
        ) : null}
        <a className="btn btn-ghost" href="/settings" title={t('app.settings')}>
          {t('app.settings')}
        </a>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={() => setLocale(locale === 'ru' ? 'en' : 'ru')}
          title={t('app.languageTip')}
        >
          {locale === 'ru' ? 'RU' : 'EN'}
        </button>
        <button type="button" className="btn btn-ghost" onClick={toggleTheme} title={t('app.theme')}>
          {dark ? '☀' : '🌙'}
        </button>
        {user ? (
          <div className="ml-1 flex items-center gap-1.5 border-l pl-2">
            <span className="chip" title={user.username}>
              {user.is_admin ? '👑' : '👤'} {user.display_name || user.username}
            </span>
            <button type="button" className="btn btn-ghost" onClick={() => void logout()}>
              {t('app.logout')}
            </button>
          </div>
        ) : null}
      </div>
    </header>
  );
}
