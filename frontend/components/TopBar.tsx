'use client';

import { useEffect, useState } from 'react';

import { useAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type { HealthResponse } from '@/lib/types';

import { ContextMenu } from './ContextMenu';

interface TopBarProps {
  health: HealthResponse | null;
  onRefreshHealth: () => void;
  activeProvider: string | null;
  activeModel: string | null;
  indexSummary?: string;
  projectName?: string | null;
  viewingOwner?: string | null;
  onOpenAdmin: () => void;
  onOpenSettings: () => void;
}

export function TopBar({
  health,
  onRefreshHealth,
  activeProvider,
  activeModel,
  indexSummary,
  projectName,
  viewingOwner,
  onOpenAdmin,
  onOpenSettings,
}: TopBarProps) {
  const { locale, setLocale, t } = useI18n();
  const { user, logout } = useAuth();
  const [dark, setDark] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);

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
  const dot =
    status === 'ok'
      ? 'bg-emerald-500'
      : status === 'degraded'
        ? 'bg-amber-500'
        : status === 'error'
          ? 'bg-rose-500'
          : 'bg-slate-400';
  const statusHint = [
    t(`status.${status}`),
    health ? `v${health.version}` : null,
    activeProvider ? `${activeProvider}${activeModel ? ` · ${activeModel}` : ''}` : null,
    indexSummary || null,
  ]
    .filter(Boolean)
    .join(' · ');

  const openMenu = (event: React.MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setMenu({ x: rect.right - 210, y: rect.bottom + 6 });
  };

  const initials = (user?.display_name || user?.username || '?').trim().charAt(0).toUpperCase();

  return (
    <header className="flex h-12 shrink-0 items-center gap-2 px-2">
      <div className="flex items-center gap-2 rounded-xl px-1.5 py-1">
        <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-xs font-bold text-white shadow-lg shadow-indigo-500/20">
          EW
        </div>
        <div className="hidden leading-tight sm:block">
          <div className="text-sm font-semibold tracking-tight">Element Workspace</div>
          <div className="text-[10px] text-slate-500 dark:text-zinc-400">{t('app.subtitle')}</div>
        </div>
      </div>

      <button
        type="button"
        onClick={onRefreshHealth}
        title={statusHint || t('app.healthRecheck')}
        className="icon-btn ml-1"
        aria-label={t('app.healthRecheck')}
      >
        <span className={`status-dot ${dot}`} />
      </button>

      {projectName ? (
        <span className="chip max-w-[38vw] truncate" title={t('app.activeProjectTip')}>
          📁 {projectName}
        </span>
      ) : null}
      {viewingOwner ? (
        <button
          type="button"
          className="chip text-amber-600 dark:text-amber-400"
          title={t('app.viewingOtherUser')}
          onClick={() => {
            window.location.href = '/';
          }}
        >
          👁 {t('app.viewingOtherUser')}
        </button>
      ) : null}

      <div className="ml-auto flex items-center gap-1">
        <button
          type="button"
          className="icon-btn text-[10px] font-semibold"
          onClick={() => setLocale(locale === 'ru' ? 'en' : 'ru')}
          title={t('app.languageTip')}
        >
          {locale === 'ru' ? 'RU' : 'EN'}
        </button>
        <button type="button" className="icon-btn" onClick={toggleTheme} title={t('app.theme')}>
          {dark ? '☀' : '🌙'}
        </button>
        {user ? (
          <button
            type="button"
            onClick={openMenu}
            className="ml-1 flex h-8 items-center gap-2 rounded-full border border-white/20 bg-white/40 px-1.5 pr-2 text-xs backdrop-blur transition hover:bg-white/60 dark:border-white/10 dark:bg-white/5 dark:hover:bg-white/10"
            title={user.display_name || user.username}
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-900 text-[10px] font-semibold text-white dark:bg-white dark:text-slate-900">
              {initials}
            </span>
            <span className="hidden max-w-[130px] truncate sm:block">{user.display_name || user.username}</span>
            <span className="text-[9px] text-slate-400">▾</span>
          </button>
        ) : null}
      </div>

      <ContextMenu
        open={menu !== null}
        x={menu?.x ?? 0}
        y={menu?.y ?? 0}
        onClose={() => setMenu(null)}
        header={user ? `${user.display_name || user.username} · @${user.username}` : undefined}
        items={[
          { key: 'health', label: t('menu.health'), icon: '↻', onSelect: onRefreshHealth },
          { key: 'settings', label: t('menu.settings'), icon: '⚙', onSelect: onOpenSettings },
          ...(user?.is_admin
            ? [{ key: 'admin', label: t('menu.admin'), icon: '👥', onSelect: onOpenAdmin }]
            : []),
          { key: 'divider', separator: true },
          { key: 'logout', label: t('menu.logout'), icon: '↪', danger: true, onSelect: () => void logout() },
        ]}
      />
    </header>
  );
}
