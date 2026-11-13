'use client';

import { useCallback, useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useRequireAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type {
  AdminOverview,
  AdminProject,
  ModelConnectionPayload,
  UserCreatePayload,
  UserInfo,
  UserRole,
  UserUpdatePayload,
} from '@/lib/types';

import { ConfirmModal, Modal } from './Modal';

interface ConnectionDraft {
  name: string;
  base_url: string;
  key: string;
  model: string;
  clearKey: boolean;
  hasKey: boolean;
  keyMasked: string;
}

interface Draft {
  username: string;
  password: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  connections: ConnectionDraft[];
}

function emptyConnection(): ConnectionDraft {
  return {
    name: '',
    base_url: '',
    key: '',
    model: '',
    clearKey: false,
    hasKey: false,
    keyMasked: '',
  };
}

function emptyDraft(): Draft {
  return {
    username: '',
    password: '',
    display_name: '',
    role: 'user',
    is_active: true,
    connections: [emptyConnection()],
  };
}

function draftFromUser(user: UserInfo): Draft {
  const connections = user.connections.length
    ? user.connections.map((item) => ({
        name: item.name,
        base_url: item.base_url,
        key: '',
        model: item.model,
        clearKey: false,
        hasKey: item.has_key,
        keyMasked: item.key_masked,
      }))
    : [emptyConnection()];
  return {
    username: user.username,
    password: '',
    display_name: user.display_name,
    role: user.role,
    is_active: user.is_active,
    connections,
  };
}

function rowIsEmpty(row: ConnectionDraft): boolean {
  return !row.name.trim() && !row.base_url.trim() && !row.model.trim() && !row.key.trim();
}

function rowIsComplete(row: ConnectionDraft): boolean {
  return Boolean(row.name.trim() && row.base_url.trim() && row.model.trim());
}

export function AdminPanel() {
  const { t } = useI18n();
  const auth = useRequireAuth();
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [projects, setProjects] = useState<AdminProject[]>([]);
  const [modal, setModal] = useState<{ mode: 'create' | 'edit'; user?: UserInfo } | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [pendingDelete, setPendingDelete] = useState<UserInfo | null>(null);
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

  const openCreate = () => {
    setDraft(emptyDraft());
    setError(null);
    setNotice(null);
    setModal({ mode: 'create' });
  };

  const openEdit = (user: UserInfo) => {
    setDraft(draftFromUser(user));
    setError(null);
    setNotice(null);
    setModal({ mode: 'edit', user });
  };

  const patchConnection = (index: number, patch: Partial<ConnectionDraft>) => {
    setDraft((prev) => {
      const connections = prev.connections.map((row, itemIndex) => {
        if (itemIndex !== index) return row;
        const next = { ...row, ...patch };
        if (patch.key && patch.key.trim()) next.clearKey = false;
        if (patch.clearKey === true) next.key = '';
        return next;
      });
      return { ...prev, connections };
    });
  };

  const addConnection = () =>
    setDraft((prev) => ({ ...prev, connections: [...prev.connections, emptyConnection()] }));

  const removeConnection = (index: number) =>
    setDraft((prev) => {
      const connections = prev.connections.filter((_, itemIndex) => itemIndex !== index);
      return { ...prev, connections: connections.length ? connections : [emptyConnection()] };
    });

  const filledConnections = draft.connections.filter((row) => !rowIsEmpty(row));
  const connectionsValid =
    filledConnections.every(rowIsComplete) &&
    new Set(filledConnections.map((row) => row.name.trim().toLowerCase())).size ===
      filledConnections.length;
  const connectionsHint = !connectionsValid
    ? t('admin.connectionRequired')
    : t('admin.connectionsHint');

  const connectionPayloads = (): ModelConnectionPayload[] =>
    filledConnections.map((row) => ({
      name: row.name.trim(),
      base_url: row.base_url.trim(),
      model: row.model.trim(),
      ...(row.key.trim() ? { api_key: row.key.trim() } : {}),
      ...(row.clearKey ? { clear_key: true } : {}),
    }));

  const save = () =>
    run(
      async () => {
        if (!modal || !connectionsValid) return;
        if (modal.mode === 'create') {
          const payload: UserCreatePayload = {
            username: draft.username.trim(),
            password: draft.password,
            display_name: draft.display_name.trim(),
            role: draft.role,
            connections: connectionPayloads(),
          };
          await api.adminCreateUser(payload);
          setModal(null);
          return;
        }
        if (!modal.user) return;
        const payload: UserUpdatePayload = {
          display_name: draft.display_name.trim(),
          password: draft.password || undefined,
          role: draft.role,
          is_active: draft.is_active,
          connections: connectionPayloads(),
        };
        await api.adminUpdateUser(modal.user.id, payload);
        setModal(null);
      },
      modal?.mode === 'create' ? t('admin.noticeUserCreated') : t('admin.noticeUserSaved'),
    );

  const confirmDelete = () => {
    const user = pendingDelete;
    if (!user) return;
    setPendingDelete(null);
    void run(
      () => api.adminDeleteUser(user.id).then(() => undefined),
      t('admin.noticeUserDeleted'),
    );
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
            <div key={key} className="panel rounded-lg px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
                {t(key)}
              </div>
              <div className="text-sm font-semibold">{value}</div>
            </div>
          ))}
        </section>
      ) : null}

      <section className="panel overflow-x-auto rounded-xl p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">{t('admin.usersTitle')}</h2>
          <span className="text-[11px] text-slate-500 dark:text-zinc-400">
            {t('admin.usersCount', { count: users.length })}
          </span>
          <button type="button" className="btn btn-primary ml-auto" onClick={openCreate}>
            {t('admin.newUser')}
          </button>
        </div>
        <table className="w-full min-w-[900px] text-left text-xs">
          <thead className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
            <tr>
              <th className="py-1">{t('admin.username')}</th>
              <th className="py-1">{t('admin.role')}</th>
              <th className="py-1">{t('admin.providersCol')}</th>
              <th className="py-1">{t('admin.projects')}</th>
              <th className="py-1">{t('admin.files')}</th>
              <th className="py-1">{t('admin.status')}</th>
              <th className="py-1 text-right">{t('admin.actions')}</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id} className="border-t align-top">
                <td className="py-2">
                  <div className="font-medium">{user.display_name || user.username}</div>
                  <div className="text-[10px] text-slate-500 dark:text-zinc-400">
                    @{user.username}
                  </div>
                </td>
                <td className="py-2">
                  {user.is_admin ? `👑 ${t('admin.roleAdmin')}` : t('admin.roleUser')}
                </td>
                <td className="py-2">
                  {user.connections.length === 0 ? (
                    <span className="text-[11px] text-amber-600 dark:text-amber-400">
                      {t('admin.noConnections')}
                    </span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {user.connections.map((connection) => (
                        <span
                          key={connection.name}
                          className="chip text-[10px] text-emerald-700 dark:text-emerald-400"
                          title={`${connection.base_url} · ${connection.model}`}
                        >
                          {connection.name}
                          <span className="ml-1 font-mono text-[10px] opacity-70">
                            {connection.model}
                          </span>
                        </span>
                      ))}
                    </div>
                  )}
                </td>
                <td className="py-2">{user.stats?.projects ?? 0}</td>
                <td className="py-2">{user.stats?.files ?? 0}</td>
                <td className="py-2">
                  {user.is_active ? t('admin.active') : t('admin.disabled')}
                </td>
                <td className="py-2 text-right">
                  <div className="flex justify-end gap-1">
                    <button type="button" className="btn" onClick={() => openEdit(user)}>
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
                      onClick={() => setPendingDelete(user)}
                    >
                      🗑
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.length === 0 ? (
          <p className="mt-3 text-xs text-slate-500 dark:text-zinc-400">{t('admin.noUsers')}</p>
        ) : null}
      </section>

      <Modal
        open={modal !== null}
        title={
          modal?.mode === 'edit' && modal.user
            ? t('admin.editUserTitle', { username: modal.user.username })
            : t('admin.createUserTitle')
        }
        subtitle={t('admin.keyHint')}
        onClose={() => {
          if (!busy) setModal(null);
        }}
        footer={
          <div className="flex w-full flex-wrap items-center justify-between gap-2">
            <div>
              {modal?.mode === 'edit' && modal.user ? (
                <button
                  type="button"
                  className="btn border-rose-300 text-rose-600 dark:text-rose-400"
                  disabled={busy || modal.user.id === auth.user?.id}
                  onClick={() => {
                    setPendingDelete(modal.user ?? null);
                    setModal(null);
                  }}
                >
                  {t('common.delete')}
                </button>
              ) : null}
            </div>
            <div className="flex items-center gap-2">
              <button type="button" className="btn" onClick={() => setModal(null)} disabled={busy}>
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void save()}
                disabled={
                  busy ||
                  !draft.username.trim() ||
                  !connectionsValid ||
                  (modal?.mode === 'create' && draft.password.length < 6)
                }
              >
                {busy ? t('common.working') : t('common.save')}
              </button>
            </div>
          </div>
        }
      >
        <div className="space-y-4">
          {error ? (
            <div className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
              {error}
            </div>
          ) : null}
          <section>
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('admin.accountSection')}
            </h3>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              <label className="text-xs">
                <span className="mb-1 block text-slate-500 dark:text-zinc-400">
                  {t('admin.username')}
                </span>
                {modal?.mode === 'edit' ? (
                  <input className="input" value={draft.username} readOnly disabled />
                ) : (
                  <input
                    className="input"
                    value={draft.username}
                    onChange={(event) => setDraft({ ...draft, username: event.target.value })}
                  />
                )}
              </label>
              <label className="text-xs">
                <span className="mb-1 block text-slate-500 dark:text-zinc-400">
                  {t('admin.displayName')}
                </span>
                <input
                  className="input"
                  value={draft.display_name}
                  onChange={(event) => setDraft({ ...draft, display_name: event.target.value })}
                />
              </label>
              <label className="text-xs">
                <span className="mb-1 block text-slate-500 dark:text-zinc-400">
                  {modal?.mode === 'edit' ? t('admin.newPassword') : t('admin.password')}
                </span>
                <input
                  className="input"
                  type="password"
                  autoComplete="new-password"
                  value={draft.password}
                  onChange={(event) => setDraft({ ...draft, password: event.target.value })}
                />
              </label>
              <label className="text-xs">
                <span className="mb-1 block text-slate-500 dark:text-zinc-400">
                  {t('admin.role')}
                </span>
                <select
                  className="input"
                  value={draft.role}
                  onChange={(event) => setDraft({ ...draft, role: event.target.value as UserRole })}
                >
                  <option value="user">{t('admin.roleUser')}</option>
                  <option value="admin">{t('admin.roleAdmin')}</option>
                </select>
              </label>
              {modal?.mode === 'edit' ? (
                <label className="flex items-center gap-2 self-end pb-2 text-xs">
                  <input
                    type="checkbox"
                    checked={draft.is_active}
                    onChange={(event) =>
                      setDraft({ ...draft, is_active: event.target.checked })
                    }
                  />
                  {t('admin.activeLabel')}
                </label>
              ) : null}
            </div>
          </section>

          <section>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-zinc-400">
                {t('admin.connectionsSection')}
              </h3>
              <button type="button" className="btn ml-auto px-2 py-1 text-[11px]" onClick={addConnection}>
                {t('admin.addConnection')}
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[820px] text-left text-xs">
                <thead className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
                  <tr>
                    <th className="py-1 pr-2">{t('admin.connectionName')}</th>
                    <th className="py-1 pr-2">{t('admin.connectionAddress')}</th>
                    <th className="py-1 pr-2">{t('admin.connectionKey')}</th>
                    <th className="py-1 pr-2">{t('admin.connectionModel')}</th>
                    <th className="py-1 text-right">{t('admin.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {draft.connections.map((row, index) => (
                    <tr key={index} className="border-t align-top">
                      <td className="py-2 pr-2">
                        <input
                          className="input py-1 text-[11px]"
                          value={row.name}
                          placeholder={t('admin.connectionNamePlaceholder')}
                          onChange={(event) =>
                            patchConnection(index, { name: event.target.value })
                          }
                        />
                      </td>
                      <td className="py-2 pr-2">
                        <input
                          className="input py-1 font-mono text-[11px]"
                          value={row.base_url}
                          placeholder={t('admin.connectionAddressPlaceholder')}
                          onChange={(event) =>
                            patchConnection(index, { base_url: event.target.value })
                          }
                        />
                      </td>
                      <td className="py-2 pr-2">
                        <input
                          className="input py-1 font-mono text-[11px]"
                          value={row.key}
                          placeholder={
                            row.clearKey
                              ? t('admin.keyWillClear')
                              : row.hasKey
                                ? t('admin.keyKeepMasked', { key: row.keyMasked })
                                : t('admin.connectionKeyOptional')
                          }
                          onChange={(event) =>
                            patchConnection(index, { key: event.target.value })
                          }
                        />
                        {row.hasKey ? (
                          <button
                            type="button"
                            className={`mt-1 text-[10px] underline ${
                              row.clearKey ? 'text-rose-600 dark:text-rose-400' : ''
                            }`}
                            onClick={() =>
                              patchConnection(index, { clearKey: !row.clearKey })
                            }
                          >
                            {row.clearKey ? t('admin.undoClearKey') : t('admin.clearKey')}
                          </button>
                        ) : null}
                      </td>
                      <td className="py-2 pr-2">
                        <input
                          className="input py-1 text-[11px]"
                          value={row.model}
                          placeholder={t('admin.connectionModelPlaceholder')}
                          onChange={(event) =>
                            patchConnection(index, { model: event.target.value })
                          }
                        />
                      </td>
                      <td className="py-2 text-right">
                        <button
                          type="button"
                          className="btn px-2 py-1 text-[11px]"
                          title={t('admin.removeConnection')}
                          onClick={() => removeConnection(index)}
                        >
                          ✕
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p
              className={`mt-2 text-[11px] ${
                connectionsValid
                  ? 'text-slate-500 dark:text-zinc-400'
                  : 'text-rose-600 dark:text-rose-400'
              }`}
            >
              {connectionsHint}
            </p>
          </section>
        </div>
      </Modal>

      <ConfirmModal
        open={pendingDelete !== null}
        title={t('admin.deleteTitle')}
        message={t('admin.confirmDelete', { username: pendingDelete?.username ?? '' })}
        confirmLabel={t('common.delete')}
        danger
        busy={busy}
        onClose={() => setPendingDelete(null)}
        onConfirm={confirmDelete}
      />

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
