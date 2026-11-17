'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { ChatMsg, Citation, ProvidersResponse, WorkNode } from '@/lib/types';
import type { ChatController } from '@/lib/useChat';
import { fileGlyph } from '@/lib/utils';

import { ContextMenu } from './ContextMenu';
import { Markdown } from './Markdown';
import { ToolCallCard } from './ToolCallCard';

interface ChatPanelProps {
  chat: ChatController;
  providers: ProvidersResponse | null;
  attachments: WorkNode[];
  onRemoveAttachment: (id: string) => void;
  onAttachSelected: () => void;
  onOpenNode: (id: string) => void;
  onSettings: () => void;
  onDeleteSession: (id: string) => void | Promise<void>;
  /** Name of the project the chat is scoped to; null disables the composer. */
  projectLabel: string | null;
}

interface MessageMenu {
  x: number;
  y: number;
  content: string;
  citations: Citation[];
}

export function ChatPanel({
  chat,
  providers,
  attachments,
  onRemoveAttachment,
  onAttachSelected,
  onOpenNode,
  onSettings,
  onDeleteSession,
  projectLabel,
}: ChatPanelProps) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  const [provider, setProvider] = useState<string>('');
  const [model, setModel] = useState<string>('');
  const [useRag, setUseRag] = useState(true);
  const [dynamicModels, setDynamicModels] = useState<Record<string, string[]>>({});
  const [menu, setMenu] = useState<MessageMenu | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const configured = useMemo(
    () => (providers?.providers || []).filter((item) => item.configured && item.enabled),
    [providers],
  );

  useEffect(() => {
    if (!providers || provider) return;
    const first = configured[0];
    if (first) {
      setProvider(first.name);
      setModel(first.default_model);
    }
  }, [providers, configured, provider]);

  useEffect(() => {
    if (!provider) return;
    let cancelled = false;
    void (async () => {
      try {
        const result = await api.providerModels(provider);
        if (!cancelled && result.models?.length) {
          setDynamicModels((prev) => ({ ...prev, [provider]: result.models.slice(0, 60) }));
        }
      } catch {
        /* keep the curated list */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [provider]);

  useEffect(() => {
    if (!provider) return;
    const list = dynamicModels[provider];
    if (!list || list.length === 0) return;
    if (model && list.includes(model)) return;
    const preferred = configured.find((item) => item.name === provider)?.default_model;
    setModel(preferred && list.includes(preferred) ? preferred : list[0]);
  }, [dynamicModels, provider, configured, model]);

  useEffect(() => {
    const element = scrollRef.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [chat.messages, chat.streaming]);

  const submit = async () => {
    const value = text.trim();
    if (!value || chat.streaming) return;
    setText('');
    await chat.send(
      value,
      attachments.map((node) => node.id),
      useRag,
      provider || null,
      model || null,
    );
    textareaRef.current?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const copyText = async (value: string) => {
    if (!value) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const area = document.createElement('textarea');
        area.value = value;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        document.execCommand('copy');
        document.body.removeChild(area);
      }
    } catch {
      /* clipboard can be blocked; the menu simply closes */
    }
  };

  const openMenu = (event: React.MouseEvent, content: string, citations?: Citation[]) => {
    if (!content && !citations?.length) return;
    event.preventDefault();
    setMenu({ x: event.clientX, y: event.clientY, content, citations: citations || [] });
  };

  const renderMessage = (message: ChatMsg) => {
    const citations = message.citations || (message.pending ? chat.citations : []);
    if (message.role === 'user') {
      return (
        <div key={message.id} className="flex justify-end">
          <div
            className="max-w-[92%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-indigo-500/90 px-3 py-2 text-sm text-white shadow-sm"
            onContextMenu={(event) => openMenu(event, message.content)}
          >
            {message.content}
          </div>
        </div>
      );
    }

    const visibleTools = (message.tool_calls || []).filter(
      (call) =>
        call.status === 'running' ||
        call.status === 'error' ||
        call.ok === false ||
        Boolean(call.operation_id),
    );
    if (!message.content && visibleTools.length === 0 && !message.error) return null;

    return (
      <div key={message.id} className="space-y-2">
        {message.content ? (
          <div
            className="glass rounded-2xl rounded-bl-md px-3 py-2"
            onContextMenu={(event) => openMenu(event, message.content, citations)}
          >
            <Markdown content={message.content} />
            {message.pending ? (
              <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-indigo-500" />
            ) : null}
          </div>
        ) : null}

        {visibleTools.length > 0 ? (
          <div className="space-y-1.5">
            {visibleTools.map((call) => (
              <ToolCallCard
                key={`${message.id}-${call.id}`}
                call={call}
                onRevert={(target) => void chat.revertToolCall(target)}
              />
            ))}
          </div>
        ) : null}

        {citations && citations.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {citations.map((citation) => (
              <button
                key={`${message.id}-${citation.index}-${citation.node_id}`}
                type="button"
                onClick={() => onOpenNode(citation.node_id)}
                title={`${citation.path} · ${citation.label}`}
                className="chip hover:border-indigo-400 hover:text-indigo-600 dark:hover:text-indigo-300"
              >
                [{citation.index}] {citation.name}
              </button>
            ))}
          </div>
        ) : null}

        {message.error ? (
          <p className="px-1 text-[11px] text-rose-600 dark:text-rose-400">{message.error}</p>
        ) : null}
      </div>
    );
  };

  const noProvider = providers !== null && !providers.chat_available;
  const modelsForProvider = (name: string) => {
    const dynamic = dynamicModels[name];
    if (dynamic && dynamic.length > 0) return dynamic;
    return configured.find((item) => item.name === name)?.models ?? [];
  };

  const lastMessage = chat.messages[chat.messages.length - 1];
  const lastAssistant = lastMessage?.role === 'assistant' ? lastMessage : null;
  const lastHasContent = Boolean(lastAssistant?.content);
  const runningTool = Boolean(lastAssistant?.tool_calls?.some((call) => call.status === 'running'));
  const showThinking = chat.streaming && !lastHasContent;
  const thinkingLabel = runningTool
    ? t('chat.thinkingTools')
    : chat.progress?.steps
      ? t('chat.thinkingAnswer')
      : t('chat.thinking');

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 px-2 py-1.5">
        <select
          className="input h-8 min-w-0 flex-1 py-1 text-xs"
          value={chat.sessionId ?? ''}
          onChange={(event) => void chat.selectSession(event.target.value)}
        >
          {chat.sessions.length === 0 ? <option value="">{t('chat.newChat')}</option> : null}
          {chat.sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.title || t('chat.fallbackTitle')}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="icon-btn"
          title={t('chat.newChat')}
          onClick={() => void chat.newSession()}
        >
          ＋
        </button>
        <button
          type="button"
          className="icon-btn"
          title={t('chat.deleteChat')}
          onClick={() => chat.sessionId && void onDeleteSession(chat.sessionId)}
        >
          🗑
        </button>
      </div>

      {noProvider ? (
        <div className="mx-2 mb-1 flex items-center gap-2 rounded-xl border border-amber-300/40 bg-amber-50/50 px-2.5 py-1.5 text-[11px] text-amber-800 dark:border-amber-800/40 dark:bg-amber-950/30 dark:text-amber-200">
          <span className="flex-1 truncate">{providers?.degraded_banner}</span>
          <button type="button" className="btn" onClick={onSettings}>
            {t('app.settings')}
          </button>
        </div>
      ) : null}

      {chat.error && chat.error.code !== 'no_provider' ? (
        <div className="mx-2 mb-1 rounded-xl border border-rose-300/40 bg-rose-50/50 px-2.5 py-1.5 text-[11px] text-rose-700 dark:border-rose-800/40 dark:bg-rose-950/30 dark:text-rose-300">
          <span className="font-medium">{chat.error.provider ? `${chat.error.provider}: ` : ''}</span>
          {chat.error.message}
          {(chat.error.status_code === 429 || chat.error.code === 'rate_limit') && (
            <button type="button" className="ml-2 underline" onClick={onSettings}>
              {t('app.settings')}
            </button>
          )}
        </div>
      ) : null}

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto px-3 py-3">
        {chat.messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-center text-slate-400 dark:text-zinc-500">
            <div className="text-2xl opacity-70">💬</div>
            <p className="text-[11px]">{t('chat.welcomeTitle')}</p>
          </div>
        ) : (
          chat.messages.map(renderMessage)
        )}
        {showThinking ? (
          <div className="thinking-bubble" role="status">
            <span className="flex items-center gap-1">
              <span className="thinking-dot" />
              <span className="thinking-dot" />
              <span className="thinking-dot" />
            </span>
            <span>{thinkingLabel}</span>
          </div>
        ) : null}
      </div>

      <div className="p-2">
        {attachments.length > 0 ? (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {attachments.map((node) => (
              <span key={node.id} className="chip">
                {fileGlyph(node)} {node.name}
                <button
                  type="button"
                  className="ml-0.5 text-slate-400 hover:text-rose-500"
                  onClick={() => onRemoveAttachment(node.id)}
                  title={t('chat.removeAttachment')}
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        ) : null}

        <div className="mb-1.5 flex items-center gap-1">
          <select
            className="input h-7 min-w-0 flex-1 py-0.5 text-[11px]"
            value={provider && model ? `${provider}::${model}` : ''}
            onChange={(event) => {
              const value = event.target.value;
              if (!value) {
                setProvider('');
                setModel('');
                return;
              }
              const [nextProvider, ...rest] = value.split('::');
              setProvider(nextProvider);
              setModel(rest.join('::'));
            }}
            title={t('app.activeProvider')}
          >
            <option value="">{t('chat.auto')}</option>
            {configured.map((item) => (
              <optgroup key={item.name} label={`${item.label}${item.local ? ' (local)' : ''}`}>
                {modelsForProvider(item.name).map((name) => (
                  <option key={`${item.name}::${name}`} value={`${item.name}::${name}`}>
                    {name}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
          <label className="chip cursor-pointer" title={t('chat.docsTip')}>
            <input
              type="checkbox"
              className="mr-1"
              checked={useRag}
              onChange={(event) => setUseRag(event.target.checked)}
            />
            {t('chat.docs')}
          </label>
          <button type="button" className="icon-btn-sm" onClick={onAttachSelected} title={t('chat.attachTip')}>
            📎
          </button>
        </div>

        <div className="flex items-end gap-1.5">
          <textarea
            ref={textareaRef}
            className="input min-h-[50px] max-h-40 resize-y py-2 text-sm"
            rows={2}
            placeholder={projectLabel ? t('chat.placeholder') : t('chat.selectProjectFirst')}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
            disabled={!projectLabel}
          />
          <button
            type="button"
            className="flex h-[50px] w-11 shrink-0 items-center justify-center rounded-xl bg-indigo-600 text-lg text-white shadow-lg shadow-indigo-600/20 transition hover:bg-indigo-500 disabled:opacity-40"
            disabled={!chat.streaming && (chat.streaming || !text.trim() || noProvider || !projectLabel)}
            onClick={() => (chat.streaming ? chat.stop() : void submit())}
            title={chat.streaming ? t('chat.stop') : t('chat.send')}
          >
            {chat.streaming ? '■' : '➤'}
          </button>
        </div>
      </div>

      <ContextMenu
        open={menu !== null}
        x={menu?.x ?? 0}
        y={menu?.y ?? 0}
        onClose={() => setMenu(null)}
        items={[
          {
            key: 'copy',
            label: t('chat.copy'),
            icon: '⧉',
            onSelect: () => void copyText(menu?.content || ''),
          },
          ...(menu?.citations.length
            ? [
                {
                  key: 'sources',
                  label: t('chat.openSources'),
                  icon: '↗',
                  onSelect: () => onOpenNode(menu.citations[0].node_id),
                },
              ]
            : []),
        ]}
      />
    </div>
  );
}
