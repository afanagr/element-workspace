export type NodeKind = 'project' | 'folder' | 'file';

export interface WorkNode {
  id: string;
  parent_id: string | null;
  project_id: string | null;
  name: string;
  kind: NodeKind;
  mime: string | null;
  size: number;
  index_status: 'pending' | 'indexing' | 'indexed' | 'failed' | 'unsupported' | string;
  index_error: string | null;
  chunk_count: number;
  indexed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  view_kind: string;
  extension: string;
  created_by: string;
  children: WorkNode[];
}

export interface UploadResult {
  items: { node: WorkNode; replaced: boolean }[];
  errors: string[];
}

export interface TextPreview {
  node_id: string;
  name: string;
  view_kind: string;
  text: string;
  segments: { label: string; text: string }[];
  meta: Record<string, unknown>;
  truncated: boolean;
}

export interface VersionInfo {
  id: string;
  version_no: number;
  size: number;
  created_by: string;
  note: string | null;
  created_at: string;
}

export interface ProviderInfo {
  name: string;
  label: string;
  configured: boolean;
  enabled: boolean;
  source: 'environment' | 'database' | 'local' | 'none' | string;
  models: string[];
  default_model: string;
  base_url: string | null;
  supports_tools: boolean;
  supports_vision: boolean;
  supports_embeddings: boolean;
  local: boolean;
  api_key_masked: string;
}

export interface EmbeddingProviderInfo {
  name: string;
  label: string;
  configured: boolean;
  models: string[];
}

export interface Preferences {
  provider: string;
  model: string;
  embedding_provider: string;
  embedding_model: string;
}

export interface ProvidersResponse {
  providers: ProviderInfo[];
  preferences: Preferences;
  embedding_providers: EmbeddingProviderInfo[];
  chat_available: boolean;
  degraded_banner: string | null;
}

export interface SettingsResponse {
  providers: ProviderInfo[];
  preferences: Preferences;
  embedding_providers: EmbeddingProviderInfo[];
  secret_key_insecure: boolean;
  max_upload_mb: number;
  default_provider: string;
  default_model: string;
  ollama_base_url: string;
  auth_enabled: boolean;
}

export interface SessionInfo {
  id: string;
  title: string;
  provider: string | null;
  model: string | null;
  project_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface Citation {
  index: number;
  node_id: string;
  name: string;
  path: string;
  label: string;
  text: string;
  score: number;
}

export interface ToolCallInfo {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  status: 'running' | 'done' | 'error';
  ok?: boolean;
  summary?: string;
  operation_id?: string | null;
  reverted?: boolean;
}

export interface ChatMsg {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  provider?: string | null;
  model?: string | null;
  tool_calls?: ToolCallInfo[] | null;
  citations?: Citation[] | null;
  usage?: Record<string, number> | null;
  error?: string | null;
  created_at?: string;
  pending?: boolean;
}

export interface ChatError {
  message: string;
  code?: string;
  provider?: string | null;
  status_code?: number | null;
  retryable?: boolean;
}

export interface UsageInfo {
  provider: string;
  model: string;
  usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  duration_ms: number;
}

export interface HealthResponse {
  status: 'ok' | 'degraded' | 'error';
  version: string;
  checks: Record<string, { ok: boolean; message: string }>;
  providers: {
    configured: string[];
    embedding_configured: string[];
    default_provider: string;
    default_model: string;
    details: ProviderInfo[];
  };
  banner: string | null;
  warnings: string[];
}

export interface IndexStatus {
  files: Record<string, number>;
  chunks: number;
  vectors: number;
}

export interface AgentEvent {
  type: string;
  [key: string]: unknown;
}
