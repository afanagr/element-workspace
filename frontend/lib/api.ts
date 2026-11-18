import type {
  AdminOverview,
  AdminProject,
  AgentEvent,
  Citation,
  DocumentTemplate,
  HealthResponse,
  IndexStatus,
  LoginResponse,
  ProvidersResponse,
  SessionInfo,
  SettingsResponse,
  TextPreview,
  UploadResult,
  UserCreatePayload,
  UserInfo,
  UserUpdatePayload,
  VersionInfo,
  WorkNode,
} from './types';

export interface OfficeConfigResponse {
  enabled: boolean;
  documentServerUrl: string;
}

export interface OfficeSession {
  config: Record<string, unknown>;
  documentServerUrl: string;
}

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || '';

const TOKEN_KEY = 'afw-token';

let unauthorizedHandler: (() => void) | null = null;

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  if (typeof window === 'undefined') return;
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage may be blocked */
  }
}

/** Registered by the auth provider: called when the API answers 401. */
export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenQuery(): string {
  const token = getToken();
  return token ? `?token=${encodeURIComponent(token)}` : '';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = typeof FormData !== 'undefined' && init?.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...authHeaders(),
      ...(isForm ? {} : { 'Content-Type': 'application/json' }),
      ...(init?.headers || {}),
    },
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) {
        detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      /* non-json error body */
    }
    if (response.status === 401) {
      setToken(null);
      unauthorizedHandler?.();
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    return (await response.text()) as unknown as T;
  }
  return (await response.json()) as T;
}

export interface UploadEntry {
  file: File;
  path: string;
}

export const api = {
  health: () => request<HealthResponse>('/api/health'),

  // --------------------------------------------------------------- auth
  login: (username: string, password: string) =>
    request<LoginResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  me: () => request<UserInfo>('/api/auth/me'),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ ok: boolean }>('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),

  // -------------------------------------------------------------- admin
  adminUsers: () => request<UserInfo[]>('/api/admin/users'),
  adminCreateUser: (payload: UserCreatePayload) =>
    request<UserInfo>('/api/admin/users', { method: 'POST', body: JSON.stringify(payload) }),
  adminUpdateUser: (userId: string, payload: UserUpdatePayload) =>
    request<UserInfo>(`/api/admin/users/${userId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  adminDeleteUser: (userId: string) =>
    request<{ deleted: boolean; username: string }>(`/api/admin/users/${userId}`, {
      method: 'DELETE',
    }),
  adminProjects: (ownerId?: string | null) =>
    request<AdminProject[]>(
      `/api/admin/projects${ownerId ? `?owner_id=${encodeURIComponent(ownerId)}` : ''}`,
    ),
  adminOverview: () => request<AdminOverview>('/api/admin/overview'),

  tree: (ownerId?: string | null) =>
    request<WorkNode[]>(`/api/tree${ownerId ? `?owner_id=${encodeURIComponent(ownerId)}` : ''}`),
  projectTree: (projectId: string) =>
    request<WorkNode[]>(`/api/tree?project_id=${encodeURIComponent(projectId)}`),
  createProject: (name: string) =>
    request<WorkNode>('/api/projects', { method: 'POST', body: JSON.stringify({ name }) }),
  createFolder: (parentId: string, name: string) =>
    request<WorkNode>('/api/folders', {
      method: 'POST',
      body: JSON.stringify({ parent_id: parentId, name }),
    }),
  updateNode: (nodeId: string, payload: { name?: string; parent_id?: string | null }) =>
    request<WorkNode>(`/api/nodes/${nodeId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteNode: (nodeId: string) =>
    request<{ deleted: boolean; items: number }>(`/api/nodes/${nodeId}`, { method: 'DELETE' }),
  reindex: (nodeId: string) =>
    request<{ queued: number }>(`/api/nodes/${nodeId}/reindex`, { method: 'POST' }),
  convertToPdf: (nodeId: string) =>
    request<WorkNode>(`/api/nodes/${nodeId}/convert/pdf`, { method: 'POST' }),
  indexStatus: () => request<IndexStatus>('/api/index/status'),

  upload: async (parentId: string, entries: UploadEntry[]): Promise<UploadResult> => {
    const form = new FormData();
    form.append('parent_id', parentId);
    for (const entry of entries) {
      form.append('files', entry.file);
      form.append('paths', entry.path);
    }
    return request<UploadResult>('/api/upload', { method: 'POST', body: form });
  },

  extract: async (nodeId: string, file: File): Promise<{ count: number }> => {
    const form = new FormData();
    form.append('file', file);
    return request<{ count: number }>(`/api/nodes/${nodeId}/extract`, {
      method: 'POST',
      body: form,
    });
  },

  downloadUrl: (nodeId: string) => `${API_BASE}/api/nodes/${nodeId}/download${tokenQuery()}`,
  contentUrl: (nodeId: string, download = false) => {
    const params = new URLSearchParams();
    if (download) params.set('download', 'true');
    const token = getToken();
    if (token) params.set('token', token);
    const query = params.toString();
    return `${API_BASE}/api/nodes/${nodeId}/content${query ? `?${query}` : ''}`;
  },
  text: (nodeId: string) => request<TextPreview>(`/api/nodes/${nodeId}/text`),
  templates: (scope: 'available' | 'standard' | 'mine' = 'available') =>
    request<DocumentTemplate[]>(`/api/templates?scope=${encodeURIComponent(scope)}`),
  createTemplate: (
    file: File,
    payload: { name?: string; description?: string; scope: 'standard' | 'user' },
  ) => {
    const form = new FormData();
    form.append('file', file);
    if (payload.name) form.append('name', payload.name);
    if (payload.description) form.append('description', payload.description);
    form.append('scope', payload.scope);
    return request<DocumentTemplate>('/api/templates', { method: 'POST', body: form });
  },
  updateTemplate: (
    templateId: string,
    payload: { name?: string; description?: string; fields?: DocumentTemplate['fields'] },
  ) =>
    request<DocumentTemplate>(`/api/templates/${templateId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteTemplate: (templateId: string) =>
    request<{ deleted: boolean }>(`/api/templates/${templateId}`, { method: 'DELETE' }),
  markupTemplate: (templateId: string) =>
    request<DocumentTemplate>(`/api/templates/${templateId}/markup`, { method: 'POST' }),
  createTemplateFromNode: (
    nodeId: string,
    payload: { name?: string; description?: string },
  ) =>
    request<DocumentTemplate>('/api/templates/from-node', {
      method: 'POST',
      body: JSON.stringify({ node_id: nodeId, ...payload }),
    }),
  templateDownloadUrl: (templateId: string) =>
    `${API_BASE}/api/templates/${templateId}/download${tokenQuery()}`,
  ocr: (nodeId: string) =>
    request<TextPreview>(`/api/nodes/${nodeId}/ocr`, { method: 'POST' }),
  saveText: (nodeId: string, content: string) =>
    request<WorkNode>(`/api/nodes/${nodeId}/content`, {
      method: 'PUT',
      body: JSON.stringify({ content }),
    }),
  sourceText: (nodeId: string) =>
    request<{ content: string }>(`/api/nodes/${nodeId}/source`),
  saveSheetCells: (nodeId: string, cells: { sheet: string; cell: string; value: string | null }[]) =>
    request<WorkNode>(`/api/nodes/${nodeId}/sheet`, {
      method: 'PUT',
      body: JSON.stringify({ cells }),
    }),
  savePdfPages: (
    nodeId: string,
    pages: { node_id?: string | null; page: number; rotate: number }[],
  ) =>
    request<WorkNode>(`/api/nodes/${nodeId}/pages`, {
      method: 'PUT',
      body: JSON.stringify({ pages }),
    }),
  officeConfig: () => request<OfficeConfigResponse>('/api/office/config'),
  officeSession: (nodeId: string) =>
    request<OfficeSession>(`/api/nodes/${nodeId}/office`, { method: 'POST' }),
  versions: (nodeId: string) => request<VersionInfo[]>(`/api/nodes/${nodeId}/versions`),
  revertVersion: (nodeId: string, versionId: string) =>
    request<WorkNode>(`/api/nodes/${nodeId}/versions/${versionId}/revert`, { method: 'POST' }),

  sessions: (projectId?: string | null) =>
    request<SessionInfo[]>(
      `/api/sessions${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`,
    ),
  createSession: (payload: { title?: string; project_id: string }) =>
    request<SessionInfo>('/api/sessions', { method: 'POST', body: JSON.stringify(payload) }),
  sessionMessages: (sessionId: string) =>
    request<
      {
        id: string;
        role: string;
        content: string;
        provider: string | null;
        model: string | null;
        tool_calls: Record<string, unknown>[] | null;
        citations: Citation[] | null;
        usage: Record<string, number> | null;
        error: string | null;
        created_at: string;
      }[]
    >(`/api/sessions/${sessionId}/messages`),
  deleteSession: (sessionId: string) =>
    request<{ deleted: boolean }>(`/api/sessions/${sessionId}`, { method: 'DELETE' }),

  providers: () => request<ProvidersResponse>('/api/providers'),
  settings: () => request<SettingsResponse>('/api/settings'),
  testProvider: (provider: string) =>
    request<{ provider: string; ok: boolean; message: string; latency_ms: number; models: string[] }>(
      `/api/providers/test?provider=${encodeURIComponent(provider)}`,
      { method: 'POST' },
    ),
  providerModels: (provider: string) =>
    request<{ models: string[]; dynamic: boolean; error?: string }>(
      `/api/providers/models?provider=${encodeURIComponent(provider)}`,
    ),
  testEmbeddings: (provider?: string, model?: string) => {
    const params = new URLSearchParams();
    if (provider) params.set('provider', provider);
    if (model) params.set('model', model);
    const query = params.toString();
    return request<{
      ok: boolean;
      provider?: string;
      model?: string;
      dimension?: number;
      message?: string;
    }>(`/api/embeddings/test${query ? `?${query}` : ''}`, { method: 'POST' });
  },

  ragSearch: (payload: {
    query: string;
    node_ids?: string[];
    project_id?: string | null;
    top_k?: number;
  }) =>
    request<{ citations: Citation[]; error: string | null }>('/api/rag/search', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  revertOperation: (operationId: string) =>
    request<{ reverted: boolean; description: string }>(`/api/operations/${operationId}/revert`, {
      method: 'POST',
    }),
};

export interface StreamCallbacks {
  onEvent: (event: AgentEvent) => void;
  onDone: () => void;
  onError: (message: string) => void;
}

/** NDJSON fallback when WebSockets are unavailable (corporate proxies, etc.). */
export async function streamMessageRest(
  sessionId: string,
  payload: {
    content: string;
    provider?: string | null;
    model?: string | null;
    attachments: string[];
    use_rag: boolean;
  },
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}/api/sessions/${sessionId}/message`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok || !response.body) {
    callbacks.onError(`HTTP ${response.status}: ${response.statusText}`);
    callbacks.onDone();
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          callbacks.onEvent(JSON.parse(trimmed) as AgentEvent);
        } catch {
          /* ignore malformed frame */
        }
      }
    }
    if (buffer.trim()) {
      try {
        callbacks.onEvent(JSON.parse(buffer) as AgentEvent);
      } catch {
        /* ignore */
      }
    }
  } catch (error) {
    callbacks.onError(error instanceof Error ? error.message : String(error));
  } finally {
    callbacks.onDone();
  }
}

export function websocketUrl(sessionId: string): string {
  const base = API_BASE || (typeof window !== 'undefined' ? window.location.origin : '');
  const url = new URL(`/api/ws/chat/${sessionId}`, base || 'http://localhost:3000');
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = getToken();
  if (token) url.searchParams.set('token', token);
  return url.toString();
}

