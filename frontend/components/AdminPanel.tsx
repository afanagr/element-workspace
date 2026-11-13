'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '@/lib/api';
import { useRequireAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type {
  AdminOverview,
  AdminProject,
  ProviderInfo,
  UserCreatePayload,
  UserInfo,
  UserRole,
  UserUpdatePayload,
} from '@/lib/types';

import { ConfirmModal, Modal } from './Modal';

const PROVIDER_NAMES = ['deepseek', 'qwen', 'qwen-local'] as const;

interface ProviderDraft {
  address: string;
  key: string;
  model: string;
  clearKey: boolean;
}

interface Draft {
  username: string;
  password: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  providers: Record<string, ProviderDraft>;
}

const PROVIDER_FALLBACK: ProviderInfo[] = [
  {
    name: 'deepseek',
    label: 'DeepSeek',
    configured: false,
    enabled: true,
    source: 'none',
    models: [],
    default_model: 'deepseek-chat',
    base_url: 'https://api.deepseek.com',
    supports_tools: true,
    supports_vision: false,
    supports_embeddings: false,
    local: false,
    api_key_masked: '',
    requires_api_key: true,
  },
  {
    name: 'qwen',
    label: 'Qwen (DashScope)',
    configured: false,
    enabled: true,
    source: 'none',
    models: [],
    default_model: 'qwen-plus',
    base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    supports_tools: true,
    supports_vision: false,
    supports_embeddings: false,
    local: false,
    api_key_masked: '',
    requires_api_key: true,
  },
  {
    name: 'qwen-local',
    label: 'Qwen (local)',
    configured: false,
    enabled: true,
    source: 'none',
    models: [],
    default_model: 'qwen2.5:7b-instruct',
    base_url: null,
    supports_tools: true,
    supports_vision: false,
    supports_embeddings: false,
    local: true,
    api_key_masked: '',
    requires_api_key: false,
  },
];

function emptyProviderDraft(): ProviderDraft {
  return { address: '', key: '', model: '', clearKey: false };
}

function emptyDraft(): Draft {
  const providers: Record<string, ProviderDraft> = {};
  for (const name of PROVIDER_NAMES) providers[name] = emptyProviderDraft();
  return {
    username: '',
    password: '',
    display_name: '',
    role: 'user',
    is_active: true,
    providers,
  };
}

function draftFromUser(user: UserInfo): Draft {
  const draft = emptyDraft();
  draft.username = user.username;
  draft.display_name = user.display_name;
  draft.role = user.role;
  draft.is_active = user.is_active;
  for (const name of PROVIDER_NAMES) {
    const info = user.providers?.[name];
    draft.providers[name] = {
      address: info?.base_url_override ?? '',
      key: '',
      model: info?.model_override ?? '',
      clearKey: false,
    };
  }
  return draft;
}

export function AdminPanel() {
  const { t } = useI18n();
  const auth = useRequireAuth();
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<UserInfo[]>([]);
  const [projects, setProjects] = useState<AdminProject[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [modal, setModal] = useState<{ mode: 'create' | 'edit'; user?: UserInfo } | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [pendingDelete, setPendingDelete] = useState<UserInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const providerList = useMemo(
    () => (providers.length ? providers : PROVIDER_FALLBACK),
    [providers],
  );

  const load = useCallback(async () => {
    try {
      const [userRows, projectRows, summary, settings] = await Promise.all([
        api.adminUsers(),
        api.adminProjects(),
        api.adminOverview(),
        api.settings(),
      ]);
      setUsers(userRows);
      setProjects(projectRows);
      setOverview(summary);
      setProviders(settings.providers);
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

  const patchProvider = (name: string, patch: Partial<ProviderDraft>) => {
    setDraft((prev) => {
      const current = prev.providers[name];
      const next = { ...current, ...patch };
      if (patch.key && patch.key.trim()) next.clearKey = false;
      if (patch.clearKey === true) next.key = '';
      return { ...prev, providers: { ...prev.providers, [name]: next } };
    });
  };

  const modelFields = () => ({
    deepseek_model: draft.providers.deepseek?.model.trim() || null,
    qwen_model: draft.providers.qwen?.model.trim() || null,
    qwen_local_model: draft.providers['qwen-local']?.model.trim() || null,
  });

  const secretFields = (): Pick<
    UserCreatePayload,
    'deepseek_api_key' | 'qwen_api_key' | 'qwen_local_api_key'
  > => {
    const result: Pick<
      UserCreatePayload,
      'deepseek_api_key' | 'qwen_api_key' | 'qwen_local_api_key'
    > = {};
    const deepseek = draft.providers.deepseek;
    const qwen = draft.providers.qwen;
    const local = draft.providers['qwen-local'];
    if (deepseek?.key.trim() && !deepseek.clearKey) result.deepseek_api_key = deepseek.key.trim();
    if (qwen?.key.trim() && !qwen.clearKey) result.qwen_api_key = qwen.key.trim();
    if (local?.key.trim() && !local.clearKey) result.qwen_local_api_key = local.key.trim();
    return result;
  };

  const clearFields = (): Pick<
    UserUpdatePayload,
    'clear_deepseek_key' | 'clear_qwen_key' | 'clear_qwen_local_key'
  > => {
    const result: Pick<
      UserUpdatePayload,
      'clear_deepseek_key' | 'clear_qwen_key' | 'clear_qwen_local_key'
    > = {};
    if (draft.providers.deepseek?.clearKey) result.clear_deepseek_key = true;
    if (draft.providers.qwen?.clearKey) result.clear_qwen_key = true;
    if (draft.providers['qwen-local']?.clearKey) result.clear_qwen_local_key = true;
    return result;
  };

  const baseUrls = () => {
    const result: Record<string, string> = {};
    for (const name of PROVIDER_NAMES) {
      const address = draft.providers[name]?.address.trim();
      if (address) result[name] = address;
    }
    return result;
  };

  const save = () =>
    run(
      async () => {
        if (!modal) return;
        if (modal.mode === 'create') {
          const payload: UserCreatePayload = {
            username: draft.username.trim(),
            password: draft.password,
            display_name: draft.display_name.trim(),
            role: draft.role,
            provider_base_urls: baseUrls(),
            ...modelFields(),
            ...secretFields(),
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
          provider_base_urls: baseUrls(),
          ...modelFields(),
          ...secretFields(),
          ...clearFields(),
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
                  <div className="flex flex-wrap gap-1">
                    {providerList.map((provider) => {
                      const info = user.providers?.[provider.name];
                      const enabled = info?.configured ?? false;
                      return (
                        <span
                          key={provider.name}
                          className={`chip text-[10px] ${
                            enabled
                              ? 'text-emerald-700 dark:text-emerald-400'
                              : 'text-slate-400 dark:text-zinc-500'
                          }`}
                          title={
                            enabled
                              ? `${provider.label}: ${info?.model || provider.default_model}`
                              : `${provider.label}: ${t('admin.notProvider')}`
                          }
                        >
                          {enabled ? '✓' : '—'} {provider.label}
                        </span>
                      );
                    })}
                  </div>
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
                  onChange={(event) =>
                    setDraft({ ...draft, role: event.target.value as UserRole })
                  }
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
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-zinc-400">
              {t('admin.modelsSection')}
            </h3>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-xs">
                <thead className="text-[10px] uppercase tracking-wide text-slate-500 dark:text-zinc-400">
                  <tr>
                    <th className="py-1 pr-2">{t('admin.providerCol')}</th>
                    <th className="py-1 pr-2">{t('admin.addressCol')}</th>
                    <th className="py-1 pr-2">{t('admin.keyCol')}</th>
                    <th className="py-1 pr-2">{t('admin.modelCol')}</th>
                    <th className="py-1 text-right">{t('admin.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {providerList.map((provider) => {
                    const row = draft.providers[provider.name] ?? emptyProviderDraft();
                    const info =
                      modal?.mode === 'edit' ? modal.user?.providers?.[provider.name] : undefined;
                    const addressPlaceholder =
                      provider.base_url || info?.default_base_url || t('admin.addressNotSet');
                    const keyPlaceholder = row.clearKey
                      ? t('admin.keyWillClear')
                      : info?.key_configured
                        ? t('admin.keyKeepMasked', { key: info.key_masked })
                        : provider.requires_api_key === false
                          ? t('admin.keyOptional')
                          : t('admin.keyPlaceholder');
                    const modelPlaceholder =
                      info?.model || provider.default_model || t('admin.modelPlaceholder');
                    return (
                      <tr key={provider.name} className="border-t align-top">
                        <td className="py-2 pr-2">
                          <div className="font-medium">{provider.label}</div>
                          <div className="text-[10px] text-slate-500 dark:text-zinc-400">
                            {provider.local ? t('admin.localProvider') : t('admin.cloudProvider')}
                          </div>
                        </td>
                        <td className="py-2 pr-2">
                          <input
                            className="input py-1 font-mono text-[11px]"
                            value={row.address}
                            placeholder={addressPlaceholder}
                            onChange={(event) =>
                              patchProvider(provider.name, { address: event.target.value })
                            }
                          />
                        </td>
                        <td className="py-2 pr-2">
                          <input
                            className="input py-1 font-mono text-[11px]"
                            value={row.key}
                            placeholder={keyPlaceholder}
                            onChange={(event) =>
                              patchProvider(provider.name, { key: event.target.value })
                            }
                          />
                        </td>
                        <td className="py-2 pr-2">
                          <input
                            className="input py-1 text-[11px]"
                            value={row.model}
                            placeholder={modelPlaceholder}
                            onChange={(event) =>
                              patchProvider(provider.name, { model: event.target.value })
                            }
                          />
                        </td>
                        <td className="py-2 text-right">
                          {info?.key_configured ? (
                            <button
                              type="button"
                              className={`btn px-2 py-1 text-[11px] ${
                                row.clearKey
                                  ? 'border-rose-400 text-rose-600 dark:text-rose-400'
                                  : ''
                              }`}
                              onClick={() =>
                                patchProvider(provider.name, { clearKey: !row.clearKey })
                              }
                            >
                              {row.clearKey ? t('admin.undoClearKey') : t('admin.clearKey')}
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-[11px] text-slate-500 dark:text-zinc-400">
              {t('admin.modelsHint')}
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
