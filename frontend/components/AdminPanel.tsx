'use client';

import { useCallback, useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useRequireAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type { AdminOverview, AdminProject, UserInfo, UserRole } from '@/lib/types';

interface Draft {
  username: string;
  password: string;
  display_name: string;
  role: UserRole;
  deepseek_api_key: string;
  deepseek_model: string;
  qwen_api_key: string;
  qwen_model: string;
}

const EMPTY_DRAFT: Draft = {
  username: '',
  password: '',
  display_name: '',
  role: 'user',
  deepseek_api_key: '',
  deepseek_model: 'deepseek-chat',
  qwen_api_key: '',
  qwen_model: 'qwen-plus',
};


export function AdminPanel() {
  const { t } = useI18n();
  const auth = useRequireAuth();
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [projects, setProjects] = useState<AdminProject[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [editing, setEditing] = useState<UserInfo | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [userRows, projectRows, summary] = await Promise.all([
        api.adminUsers(),
        api.adminProjects(),
        api.adminOverview(),
      ]);
      setUsers(userRows);
      setProjects(projectRows);
      setOverview(summary);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (auth.user?.is_admin) void load();
  }, [auth.user, load]);

  const run = async (action: () => Promise<void>, message?: string) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await action();
      if (message) setNotice(message);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const createUser = () =>
    run(async () => {
      await api.adminCreateUser({
        username: draft.username.trim(),
        password: draft.password,
        display_name: draft.display_name.trim(),
        role: draft.role,
        deepseek_api_key: draft.deepseek_api_key.trim() || null,
        deepseek_model: draft.deepseek_model.trim() || null,
        qwen_api_key: draft.qwen_api_key.trim() || null,
        qwen_model: draft.qwen_model.trim() || null,
      });
      setDraft(EMPTY_DRAFT);
    }, t('admin.noticeUserCreated'));

  const startEdit = (user: UserInfo) => {
    setEditing(user);
    setEditDraft({
      username: user.username,
      password: '',
      display_name: user.display_name,
      role: user.role,
      deepseek_api_key: '',
      deepseek_model: user.deepseek_model || 'deepseek-chat',
      qwen_api_key: '',
      qwen_model: user.qwen_model || 'qwen-plus',
    });
  };

  const saveEdit = () =>
    run(async () => {
      if (!editing) return;
      await api.adminUpdateUser(editing.id, {
        display_name: editDraft.display_name.trim(),
        password: editDraft.password || undefined,
        role: editDraft.role,
        deepseek_model: editDraft.deepseek_model.trim() || null,
        qwen_model: editDraft.qwen_model.trim() || null,
        ...(editDraft.deepseek_api_key.trim()
          ? { deepseek_api_key: editDraft.deepseek_api_key.trim() }
          : {}),
        ...(editDraft.qwen_api_key.trim()
          ? { qwen_api_key: editDraft.qwen_api_key.trim() }
          : {}),
      });
      setEditing(null);
    }, t('admin.noticeUserSaved'));

  const removeUser = (user: UserInfo) => {
    if (!window.confirm(t('admin.confirmDelete', { username: user.username }))) return;
    void run(() => api.adminDeleteUser(user.id).then(() => undefined), t('admin.noticeUserDeleted'));
  };

  const toggleActive = (user: UserInfo) =>
    run(
      () => api.adminUpdateUser(user.id, { is_active: !user.is_active }).then(() => undefined),
      t('admin.noticeUserSaved'),
    );

  if (auth.loading || !auth.user) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
        {t('common.loading')}
      </div>
    );
  }

  if (!auth.user.is_admin) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-sm">
        <p>{t('admin.forbidden')}</p>
        <a className="btn" href="/">
          {t('common.back')}
        </a>
      </div>
    );
  }

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col gap-4 px-4 py-6">
      <header className="flex flex-wrap items-center gap-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-indigo-600 text-sm font-bold text-white">
          EW
        </div>
        <div className="leading-tight">
          <h1 className="text-base font-semibold">{t('admin.title')}</h1>
          <p className="text-[11px] text-slate-500 dark:text-zinc-400">{t('admin.subtitle')}</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button type="button" className="btn" onClick={() => void load()} disabled={busy}>
            {t('common.reload')}
          </button>
          <a className="btn" href="/">
            {t('common.back')}
          </a>
          <a className="btn" href="/settings">
            {t('app.settings')}
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

      {overview ? (
        <section className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 lg:grid-cols-7">
          {(
            [
              ['admin.stat.users', overview.users],
              ['admin.stat.active', overview.active_users],
              ['admin.stat.admins', overview.admins],
              ['admin.stat.noKey', overview.users_without_key],
              ['admin.stat.projects', overview.projects],
              ['admin.stat.files', overview.files],
              ['admin.stat.chunks', overview.chunks],
            ] as [string, number][]
          ).map(([key, value]) => (
            <div key={key} className="panel rounded-xl px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
                {t(key)}
              </div>
              <div className="text-lg font-semibold">{value}</div>
            </div>
          ))}
        </section>
      ) : null}

      <section className="panel rounded-xl p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('admin.createTitle')}</h2>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          <input
            className="input"
            placeholder={t('admin.username')}
            value={draft.username}
            onChange={(event) => setDraft({ ...draft, username: event.target.value })}
          />
          <input
            className="input"
            type="password"
            placeholder={t('admin.password')}
            value={draft.password}
            onChange={(event) => setDraft({ ...draft, password: event.target.value })}
          />
          <input
            className="input"
            placeholder={t('admin.displayName')}
            value={draft.display_name}
            onChange={(event) => setDraft({ ...draft, display_name: event.target.value })}
          />
          <select
            className="input"
            value={draft.role}
            onChange={(event) => setDraft({ ...draft, role: event.target.value as UserRole })}
          >
            <option value="user">{t('admin.roleUser')}</option>
            <option value="admin">{t('admin.roleAdmin')}</option>
          </select>
          <input
            className="input"
            placeholder={t('admin.deepseekKey')}
            value={draft.deepseek_api_key}
            onChange={(event) => setDraft({ ...draft, deepseek_api_key: event.target.value })}
          />
          <input
            className="input"
            placeholder={t('admin.deepseekModel')}
            value={draft.deepseek_model}
            onChange={(event) => setDraft({ ...draft, deepseek_model: event.target.value })}
          />
          <input
            className="input"
            placeholder={t('admin.qwenKey')}
            value={draft.qwen_api_key}
            onChange={(event) => setDraft({ ...draft, qwen_api_key: event.target.value })}
          />
          <input
            className="input"
            placeholder={t('admin.qwenModel')}
            value={draft.qwen_model}
            onChange={(event) => setDraft({ ...draft, qwen_model: event.target.value })}
          />
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || !draft.username.trim() || draft.password.length < 6}
            onClick={() => void createUser()}
          >
            {t('admin.createButton')}
          </button>
          <span className="text-[11px] text-slate-500 dark:text-zinc-400">{t('admin.keyHint')}</span>
        </div>
      </section>


      {editing ? (
        <section className="panel rounded-xl border-indigo-300 p-4">
          <h2 className="mb-3 text-sm font-semibold">
            {t('admin.editTitle', { username: editing.username })}
          </h2>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            <input
              className="input"
              placeholder={t('admin.displayName')}
              value={editDraft.display_name}
              onChange={(event) => setEditDraft({ ...editDraft, display_name: event.target.value })}
            />
            <input
              className="input"
              type="password"
              placeholder={t('admin.newPassword')}
              value={editDraft.password}
              onChange={(event) => setEditDraft({ ...editDraft, password: event.target.value })}
            />
            <select
              className="input"
              value={editDraft.role}
              onChange={(event) =>
                setEditDraft({ ...editDraft, role: event.target.value as UserRole })
              }
            >
              <option value="user">{t('admin.roleUser')}</option>
              <option value="admin">{t('admin.roleAdmin')}</option>
            </select>
            <input
              className="input"
              placeholder={
                editing.deepseek_configured
                  ? `${t('admin.deepseekKey')} (${editing.deepseek_key_masked})`
                  : t('admin.deepseekKey')
              }
              value={editDraft.deepseek_api_key}
              onChange={(event) =>
                setEditDraft({ ...editDraft, deepseek_api_key: event.target.value })
              }
            />
            <input
              className="input"
              placeholder={t('admin.deepseekModel')}
              value={editDraft.deepseek_model}
              onChange={(event) =>
                setEditDraft({ ...editDraft, deepseek_model: event.target.value })
              }
            />
            <input
              className="input"
              placeholder={
                editing.qwen_configured
                  ? `${t('admin.qwenKey')} (${editing.qwen_key_masked})`
                  : t('admin.qwenKey')
              }
              value={editDraft.qwen_api_key}
              onChange={(event) => setEditDraft({ ...editDraft, qwen_api_key: event.target.value })}
            />
            <input
              className="input"
              placeholder={t('admin.qwenModel')}
              value={editDraft.qwen_model}
              onChange={(event) => setEditDraft({ ...editDraft, qwen_model: event.target.value })}
            />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() => void saveEdit()}
            >
              {t('common.save')}
            </button>
            <button type="button" className="btn" onClick={() => setEditing(null)}>
              {t('common.cancel')}
            </button>
            {editing.deepseek_configured ? (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await api.adminUpdateUser(editing.id, { clear_deepseek_key: true });
                    setEditing(null);
                  }, t('admin.noticeUserSaved'))
                }
              >
                {t('admin.clearKey')}
              </button>
            ) : null}
            {editing.qwen_configured ? (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    await api.adminUpdateUser(editing.id, { clear_qwen_key: true });
                    setEditing(null);
                  }, t('admin.noticeUserSaved'))
                }
              >
                {t('admin.clearQwenKey')}
              </button>
            ) : null}
          </div>
        </section>
      ) : null}


      <section className="panel overflow-x-auto rounded-xl p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('admin.usersTitle')}</h2>
        <table className="w-full min-w-[860px] text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
            <tr>
              <th className="py-1">{t('admin.username')}</th>
              <th className="py-1">{t('admin.role')}</th>
              <th className="py-1">{t('admin.deepseekKey')}</th>
              <th className="py-1">{t('admin.qwenKey')}</th>
              <th className="py-1">{t('admin.projects')}</th>
              <th className="py-1">{t('admin.files')}</th>
              <th className="py-1">{t('admin.status')}</th>
              <th className="py-1 text-right">{t('admin.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id} className="border-t">
                <td className="py-1.5">
                  <div className="font-medium">{user.display_name || user.username}</div>
                  <div className="text-[10px] text-slate-500 dark:text-zinc-400">
                    @{user.username}
                  </div>
                </td>
                <td className="py-1.5">
                  {user.is_admin ? `👑 ${t('admin.roleAdmin')}` : t('admin.roleUser')}
                </td>
                <td className="py-1.5">
                  {user.deepseek_configured ? (
                    <span className="font-mono text-[11px]">{user.deepseek_key_masked}</span>
                  ) : (
                    <span className="text-amber-600 dark:text-amber-400">{t('admin.noKey')}</span>
                  )}
                  {user.deepseek_model ? (
                    <div className="text-[10px] text-slate-500 dark:text-zinc-400">
                      {user.deepseek_model}
                    </div>
                  ) : null}
                </td>
                <td className="py-1.5">
                  {user.qwen_configured ? (
                    <span className="font-mono text-[11px]">{user.qwen_key_masked}</span>
                  ) : (
                    <span className="text-amber-600 dark:text-amber-400">{t('admin.noKey')}</span>
                  )}
                  {user.qwen_model ? (
                    <div className="text-[10px] text-slate-500 dark:text-zinc-400">
                      {user.qwen_model}
                    </div>
                  ) : null}
                </td>
                <td className="py-1.5">{user.stats?.projects ?? 0}</td>
                <td className="py-1.5">{user.stats?.files ?? 0}</td>
                <td className="py-1.5">
                  {user.is_active ? t('admin.active') : t('admin.disabled')}
                </td>
                <td className="py-1.5 text-right">
                  <div className="flex justify-end gap-1">
                    <button type="button" className="btn" onClick={() => startEdit(user)}>
                      {t('admin.edit')}
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy || user.id === auth.user?.id}
                      onClick={() => void toggleActive(user)}
                    >
                      {user.is_active ? t('admin.disable') : t('admin.enable')}
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy || user.id === auth.user?.id}
                      onClick={() => removeUser(user)}
                    >
                      🗑
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>


      <section className="panel overflow-x-auto rounded-xl p-4">
        <h2 className="mb-3 text-sm font-semibold">{t('admin.projectsTitle')}</h2>
        {projects.length === 0 ? (
          <p className="text-xs text-slate-500 dark:text-zinc-400">{t('admin.noProjects')}</p>
        ) : (
          <table className="w-full min-w-[560px] text-left text-xs">
            <thead className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              <tr>
                <th className="py-1">{t('admin.projectName')}</th>
                <th className="py-1">{t('admin.owner')}</th>
                <th className="py-1">{t('admin.files')}</th>
                <th className="py-1">{t('admin.chunks')}</th>
                <th className="py-1 text-right">{t('admin.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {projects.map((project) => (
                <tr key={project.id} className="border-t">
                  <td className="py-1.5 font-medium">{project.name}</td>
                  <td className="py-1.5">{project.owner || '—'}</td>
                  <td className="py-1.5">{project.files}</td>
                  <td className="py-1.5">{project.chunks}</td>
                  <td className="py-1.5 text-right">
                    <a
                      className="btn"
                      href={`/?owner=${encodeURIComponent(project.owner_id || '')}`}
                      title={t('admin.openWorkspace')}
                    >
                      {t('admin.openWorkspace')}
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

