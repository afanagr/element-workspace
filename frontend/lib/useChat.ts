'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { api, streamMessageRest, websocketUrl } from './api';
import type {
  AgentEvent,
  ChatError,
  ChatMsg,
  Citation,
  SessionInfo,
  ToolCallInfo,
  UsageInfo,
} from './types';

interface UseChatOptions {
  onWorkspaceChanged: () => void;
  onOpenCitation: (nodeId: string) => void;
}

interface ActiveModel {
  provider: string | null;
  model: string | null;
  fallbackFrom: string | null;
}

export interface ChatController {
  sessions: SessionInfo[];
  sessionId: string | null;
  messages: ChatMsg[];
  streaming: boolean;
  connected: boolean;
  error: ChatError | null;
  notice: string | null;
  usage: UsageInfo | null;
  citations: Citation[];
  active: ActiveModel;
  send: (
    text: string,
    attachments: string[],
    useRag: boolean,
    provider?: string | null,
    model?: string | null,
  ) => Promise<void>;
  newSession: () => Promise<void>;
  selectSession: (id: string) => Promise<void>;
  removeSession: (id: string) => Promise<void>;
  revertToolCall: (call: ToolCallInfo) => Promise<void>;
  clearError: () => void;
}

function updateLastAssistant(
  messages: ChatMsg[],
  updater: (message: ChatMsg) => ChatMsg,
): ChatMsg[] {
  const copy = [...messages];
  for (let index = copy.length - 1; index >= 0; index -= 1) {
    if (copy[index].role === 'assistant') {
      copy[index] = updater(copy[index]);
      return copy;
    }
  }
  return copy;
}

function withLive(messages: ChatMsg[], updater: (message: ChatMsg) => ChatMsg): ChatMsg[] {
  const copy = [...messages];
  const last = copy[copy.length - 1];
  if (last && last.role === 'assistant' && last.pending) {
    copy[copy.length - 1] = updater(last);
    return copy;
  }
  const live: ChatMsg = {
    id: `live-${Date.now()}`,
    role: 'assistant',
    content: '',
    pending: true,
    tool_calls: [],
  };
  copy.push(updater(live));
  return copy;
}

function mapHistory(rows: Awaited<ReturnType<typeof api.sessionMessages>>): ChatMsg[] {
  return rows
    .filter((row) => row.role === 'user' || row.role === 'assistant')
    .map((row) => ({
      id: row.id,
      role: row.role as 'user' | 'assistant',
      content: row.content,
      provider: row.provider,
      model: row.model,
      citations: row.citations ?? undefined,
      usage: row.usage ?? undefined,
      error: row.error,
      created_at: row.created_at,
      tool_calls: (row.tool_calls || []).map((call) => ({
        id: String(call.id ?? ''),
        name: String(call.name ?? 'tool'),
        arguments: (call.arguments as Record<string, unknown>) || {},
        status: call.ok === false ? ('error' as const) : ('done' as const),
        ok: call.ok !== false,
        summary: String(call.summary ?? ''),
        operation_id: (call.operation_id as string | null) ?? null,
      })),
    }));
}

export function useChat(options: UseChatOptions): ChatController {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<ChatError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageInfo | null>(null);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [active, setActive] = useState<ActiveModel>({
    provider: null,
    model: null,
    fallbackFrom: null,
  });

  const socketRef = useRef<WebSocket | null>(null);
  const sessionRef = useRef<string | null>(null);
  const streamingRef = useRef(false);
  const closedRef = useRef(false);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const setStreamingState = useCallback((value: boolean) => {
    streamingRef.current = value;
    setStreaming(value);
  }, []);

  const applyEvent = useCallback(
    (event: AgentEvent) => {
      const type = String(event.type || '');
      switch (type) {
        case 'user_saved': {
          const id = String(event.message_id || '');
          setMessages((prev) => {
            const copy = [...prev];
            for (let index = copy.length - 1; index >= 0; index -= 1) {
              if (copy[index].role === 'user' && copy[index].pending) {
                copy[index] = { ...copy[index], id: id || copy[index].id, pending: false };
                break;
              }
            }
            return copy;
          });
          break;
        }
        case 'start': {
          const fallbackFrom = event.fallback_from ? String(event.fallback_from) : null;
          setActive({
            provider: event.provider ? String(event.provider) : null,
            model: event.model ? String(event.model) : null,
            fallbackFrom,
          });
          if (fallbackFrom) {
            setNotice(`Requested provider unavailable - switched to ${String(event.provider)}.`);
          }
          setStreamingState(true);
          setError(null);
          setUsage(null);
          setCitations([]);
          break;
        }
        case 'citations': {
          const items = (event.items as Citation[]) || [];
          setCitations(items);
          setMessages((prev) =>
            updateLastAssistant(prev, (message) => ({ ...message, citations: items })),
          );
          break;
        }
        case 'delta': {
          const text = String(event.text || '');
          if (!text) break;
          setMessages((prev) =>
            withLive(prev, (message) => ({ ...message, content: message.content + text })),
          );
          break;
        }
        case 'tool_call': {
          const call: ToolCallInfo = {
            id: String(event.id || `call-${Date.now()}`),
            name: String(event.name || 'tool'),
            arguments: (event.arguments as Record<string, unknown>) || {},
            status: 'running',
          };
          setMessages((prev) =>
            withLive(prev, (message) => ({
              ...message,
              tool_calls: [...(message.tool_calls || []), call],
            })),
          );
          break;
        }
        case 'tool_result': {
          const id = String(event.id || '');
          setMessages((prev) =>
            updateLastAssistant(prev, (message) => ({
              ...message,
              tool_calls: (message.tool_calls || []).map((call) =>
                call.id === id
                  ? {
                      ...call,
                      status: event.ok ? ('done' as const) : ('error' as const),
                      ok: Boolean(event.ok),
                      summary: String(event.summary || ''),
                      operation_id: (event.operation_id as string | null) ?? null,
                    }
                  : call,
              ),
            })),
          );
          if (event.ok) optionsRef.current.onWorkspaceChanged();
          break;
        }
        case 'assistant_progress': {
          const id = String(event.message_id || '');
          const toolCalls = (event.tool_calls as ToolCallInfo[] | undefined) || [];
          const normalized = toolCalls.map((call) => ({ ...call, status: 'done' as const }));
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            if (last && last.role === 'assistant' && last.pending) {
              copy[copy.length - 1] = {
                ...last,
                id: id || last.id,
                pending: false,
                tool_calls: normalized.length ? normalized : last.tool_calls,
              };
            } else if (id) {
              copy.push({
                id,
                role: 'assistant',
                content: String(event.content || ''),
                pending: false,
                tool_calls: normalized,
              });
            }
            return copy;
          });
          optionsRef.current.onWorkspaceChanged();
          break;
        }
        case 'assistant_done': {
          const id = String(event.message_id || '');
          const content = String(event.content || '');
          const items = (event.citations as Citation[] | undefined) || undefined;
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            if (last && last.role === 'assistant' && last.pending) {
              copy[copy.length - 1] = {
                ...last,
                id: id || last.id,
                content,
                pending: false,
                citations: items ?? last.citations,
              };
            } else if (id) {
              copy.push({ id, role: 'assistant', content, pending: false, citations: items });
            } else if (last) {
              copy[copy.length - 1] = { ...last, content: last.content || content };
            }
            return copy;
          });
          optionsRef.current.onWorkspaceChanged();
          break;
        }
        case 'usage': {
          setUsage({
            provider: String(event.provider || ''),
            model: String(event.model || ''),
            usage: (event.usage as UsageInfo['usage']) || {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: 0,
            },
            duration_ms: Number(event.duration_ms || 0),
          });
          break;
        }
        case 'provider_fallback': {
          setNotice(
            `Switched from ${String(event.from)} to ${String(event.to)}: ${String(event.reason || '')}`,
          );
          setActive({
            provider: String(event.to),
            model: null,
            fallbackFrom: String(event.from),
          });
          break;
        }
        case 'error': {
          const chatError: ChatError = {
            message: String(event.message || 'Unknown error'),
            code: event.code ? String(event.code) : undefined,
            provider: event.provider ? String(event.provider) : undefined,
            status_code: typeof event.status_code === 'number' ? event.status_code : undefined,
            retryable: Boolean(event.retryable),
          };
          setError(chatError);
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            if (last && last.role === 'assistant' && last.pending) {
              copy[copy.length - 1] = { ...last, pending: false, error: chatError.message };
            }
            return copy;
          });
          break;
        }
        case 'node_changed':
          optionsRef.current.onWorkspaceChanged();
          break;
        case 'stream_end':
          setStreamingState(false);
          optionsRef.current.onWorkspaceChanged();
          break;
        default:
          break;

      }
    },
    [setStreamingState],
  );

  const connect = useCallback(
    (sid: string) => {
      if (reconnectRef.current) {
        clearTimeout(reconnectRef.current);
        reconnectRef.current = null;
      }
      const previous = socketRef.current;
      if (previous) {
        previous.onopen = null;
        previous.onmessage = null;
        previous.onerror = null;
        previous.onclose = null;
        try {
          previous.close();
        } catch {
          /* ignore */
        }
      }
      const socket = new WebSocket(websocketUrl(sid));
      socketRef.current = socket;
      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        try {
          applyEvent(JSON.parse(String(message.data)) as AgentEvent);
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (closedRef.current || sessionRef.current !== sid) return;
        reconnectRef.current = setTimeout(() => {
          if (!closedRef.current && sessionRef.current === sid) connect(sid);
        }, 2500);
      };
      socket.onerror = () => setConnected(false);
    },
    [applyEvent],
  );

  const openSession = useCallback(
    async (id: string, known?: SessionInfo) => {
      sessionRef.current = id;
      setSessionId(id);
      setError(null);
      setNotice(null);
      setUsage(null);
      setCitations([]);
      try {
        const rows = await api.sessionMessages(id);
        setMessages(mapHistory(rows));
      } catch {
        setMessages([]);
      }
      if (known) {
        setSessions((prev) => (prev.some((item) => item.id === id) ? prev : [known, ...prev]));
      }
      connect(id);
    },
    [connect],
  );

  const newSession = useCallback(async () => {
    const created = await api.createSession({});
    setSessions((prev) => [created, ...prev]);
    await openSession(created.id, created);
  }, [openSession]);

  const selectSession = useCallback(
    async (id: string) => {
      await openSession(id);
    },
    [openSession],
  );

  const removeSession = useCallback(async (id: string) => {
    try {
      await api.deleteSession(id);
    } catch {
      /* already gone */
    }
    setSessions((prev) => prev.filter((item) => item.id !== id));
    if (sessionRef.current === id) {
      sessionRef.current = null;
      setSessionId(null);
      setMessages([]);
      setStreamingState(false);
    }
  }, [setStreamingState]);

  useEffect(() => {
    closedRef.current = false;
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.sessions();
        if (cancelled) return;
        setSessions(list);
        if (list.length > 0) {
          await openSession(list[0].id);
        } else if (!sessionRef.current) {
          await newSession();
        }
      } catch (err) {
        if (!cancelled) setError({ message: err instanceof Error ? err.message : String(err) });
      }
    })();
    return () => {
      cancelled = true;
      closedRef.current = true;
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };
  }, [newSession, openSession]);

  const send = useCallback(
    async (
      text: string,
      attachments: string[],
      useRag: boolean,
      provider?: string | null,
      model?: string | null,
    ) => {
      const content = text.trim();
      if (!content || streamingRef.current) return;

      let sid = sessionRef.current;
      if (!sid) {
        const created = await api.createSession({});
        sid = created.id;
        sessionRef.current = sid;
        setSessionId(sid);
        setSessions((prev) => [created, ...prev]);
        connect(sid);
      }

      setMessages((prev) => [
        ...prev,
        {
          id: `local-${Date.now()}`,
          role: 'user',
          content,
          pending: true,
          created_at: new Date().toISOString(),
        },
      ]);
      setStreamingState(true);
      setError(null);
      setNotice(null);
      setUsage(null);
      setCitations([]);

      const payload = {
        type: 'message',
        content,
        provider: provider ?? null,
        model: model ?? null,
        attachments,
        use_rag: useRag,
      };
      const socket = socketRef.current;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(payload));
        return;
      }

      await streamMessageRest(
        sid,
        { content, provider: provider ?? null, model: model ?? null, attachments, use_rag: useRag },
        {
          onEvent: applyEvent,
          onDone: () => {
            setStreamingState(false);
            optionsRef.current.onWorkspaceChanged();
          },
          onError: (message) =>
            applyEvent({ type: 'error', message, code: 'connection', retryable: true }),
        },
      );
    },
    [applyEvent, connect, setStreamingState],
  );

  const revertToolCall = useCallback(async (call: ToolCallInfo) => {
    if (!call.operation_id) return;
    try {
      const result = await api.revertOperation(call.operation_id);
      setMessages((prev) =>
        prev.map((message) => ({
          ...message,
          tool_calls: (message.tool_calls || []).map((item) =>
            item.id === call.id
              ? {
                  ...item,
                  reverted: true,
                  summary: `${item.summary ? `${item.summary}\n` : ''}↩ reverted: ${result.description}`,
                }
              : item,
          ),
        })),
      );
      optionsRef.current.onWorkspaceChanged();
    } catch (err) {
      setError({ message: err instanceof Error ? err.message : String(err) });
    }
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return {
    sessions,
    sessionId,
    messages,
    streaming,
    connected,
    error,
    notice,
    usage,
    citations,
    active,
    send,
    newSession,
    selectSession,
    removeSession,
    revertToolCall,
    clearError,
  };
}


