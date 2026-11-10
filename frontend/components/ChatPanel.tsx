'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { ChatMsg, ProvidersResponse, WorkNode } from '@/lib/types';
import type { ChatController } from '@/lib/useChat';
import { fileGlyph } from '@/lib/utils';

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
}

export function ChatPanel({
  chat,
  providers,
  attachments,
  onRemoveAttachment,
  onAttachSelected,
  onOpenNode,
  onSettings,
}: ChatPanelProps) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  const [provider, setProvider] = useState<string>('');
  const [model, setModel] = useState<string>('');
  const [useRag, setUseRag] = useState(true);
  const [dynamicModels, setDynamicModels] = useState<Record<string, string[]>>({});
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);

  const configured = useMemo(
    () => (providers?.providers || []).filter((item) => item.configured && item.enabled),
    [providers],
  );

  useEffect(() => {
    if (!providers || provider) return;
    const preferred = providers.preferences.provider;
    const exists = configured.some((item) => item.name === preferred);
    const fallback = exists ? preferred : configured[0]?.name;
    if (fallback) {
      setProvider(fallback);
      setModel(exists ? providers.preferences.model : '');
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

  // Always send a model that the provider actually offers: prefer the provider
  // default when it is in the live list, otherwise the first live model.
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

  const activeLabel = chat.active.provider
    ? `${chat.active.provider}${chat.active.model ? ` · ${chat.active.model}` : ''}`
    : provider
      ? `${provider}${model ? ` · ${model}` : ''}`
      : 'auto';

  const onKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const renderMessage = (message: ChatMsg) => {
    if (message.role === 'user') {
      return (
        <div key={message.id} className="flex justify-end">
          <div className="max-w-[92%] whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-indigo-600 px-3 py-2 text-sm text-white">
            {message.content}
          </div>
        </div>
      );
    }

    const citations = message.citations || (message.pending ? chat.citations : []);
    return (
      <div key={message.id} className="space-y-2">
        {message.content ? (
          <div className="rounded-2xl rounded-bl-sm border bg-slate-50 px-3 py-2 dark:bg-zinc-900/70">
            <Markdown content={message.content} />
            {message.pending ? (
              <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-indigo-500" />
            ) : null}
          </div>
        ) : message.pending ? (
          <div className="flex items-center gap-2 rounded-2xl border bg-slate-50 px-3 py-2 text-xs text-slate-500 dark:bg-zinc-900/70">
            <span className="h-2 w-2 animate-ping rounded-full bg-indigo-500" /> {t('chat.thinking')}
          </div>
        ) : null}

        {message.tool_calls && message.tool_calls.length > 0 ? (
          <div className="space-y-1.5">
            {message.tool_calls.map((call) => (
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
          <div className="rounded-lg border border-rose-300 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-700 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-300">
            {message.error}
          </div>
        ) : null}

        {message.provider || message.usage ? (
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-slate-400">
            {message.provider ? (
              <span>
                {message.provider}
                {message.model ? `/${message.model}` : ''}
              </span>
            ) : null}
            {message.usage?.total_tokens ? (
              <span>
                {message.usage.prompt_tokens ?? 0} in / {message.usage.completion_tokens ?? 0} out
              </span>
            ) : null}
          </div>
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

  return (
    <div className="flex h-full min-h-0 flex-col bg-[rgb(var(--surface))]">
      <div className="flex items-center gap-1.5 border-b px-2 py-1.5">
        <select
          className="input w-36 py-1 text-xs"
          value={chat.sessionId ?? ''}
          onChange={(event) => void chat.selectSession(event.target.value)}
        >
          {chat.sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.title || t('chat.fallbackTitle')}
            </option>
          ))}
        </select>
        <button type="button" className="btn" title={t('chat.newChat')} onClick={() => void chat.newSession()}>
          ＋
        </button>
        <button
          type="button"
          className="btn"
          title={t('chat.deleteChat')}
          onClick={() => chat.sessionId && void chat.removeSession(chat.sessionId)}
        >
          🗑
        </button>
        <span
          className={`chip ml-auto ${
            chat.connected
              ? 'text-emerald-600 dark:text-emerald-400'
              : 'text-amber-600 dark:text-amber-400'
          }`}
          title={chat.connected ? t('chat.liveTip') : t('chat.httpTip')}
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${chat.connected ? 'bg-emerald-500' : 'bg-amber-500'}`}
          />
          {chat.connected ? t('chat.live') : t('chat.http')}
        </span>
      </div>

      {noProvider ? (
        <div className="border-b border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
          <div className="font-semibold">{t('chat.noProviderTitle')}</div>
          <p className="mt-0.5">{providers?.degraded_banner}</p>
          <div className="mt-1.5 flex gap-2">
            <button type="button" className="btn" onClick={onSettings}>
              {t('chat.addKey')}
            </button>
          </div>
        </div>
      ) : null}

      {chat.notice ? (
        <div className="border-b border-sky-200 bg-sky-50 px-3 py-1.5 text-[11px] text-sky-800 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-200">
          {chat.notice}
        </div>
      ) : null}

      {chat.error && chat.error.code !== 'no_provider' ? (
        <div className="border-b border-rose-300 bg-rose-50 px-3 py-2 text-[11px] text-rose-800 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">
          <div className="font-semibold">
            {chat.error.provider ? `${chat.error.provider}: ` : ''}
            {chat.error.message}
          </div>
          {chat.error.status_code === 429 || chat.error.code === 'rate_limit' ? (
            <div className="mt-1 flex flex-wrap gap-2">
              <button
                type="button"
                className="btn"
                onClick={() => {
                  selectRef.current?.focus();
                  chat.clearError();
                }}
              >
                {t('chat.switchProvider')}
              </button>
              <button type="button" className="btn" onClick={onSettings}>
                {t('app.settings')}
              </button>
            </div>
          ) : chat.error.retryable ? (
            <div className="mt-1 text-[10px] opacity-80">{t('chat.transient')}</div>
          ) : null}
        </div>
      ) : null}

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {chat.messages.length === 0 ? (
          <div className="mx-auto mt-6 max-w-xs space-y-2 text-center text-[11px] text-slate-500 dark:text-zinc-400">
            <div className="text-3xl">💬</div>
            <p className="font-medium text-slate-600 dark:text-zinc-300">{t('chat.welcomeTitle')}</p>
            <p>{t('chat.example1')}</p>
            <p>{t('chat.example2')}</p>
            <p>{t('chat.example3')}</p>
            <p>{t('chat.example4')}</p>
          </div>
        ) : (
          chat.messages.map(renderMessage)
        )}
      </div>

      <div className="border-t p-2">
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

        <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
          <select
            ref={selectRef}
            className="input max-w-[260px] py-1 text-xs"
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
            title="Provider and model (grouped by provider)"
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
          <button
            type="button"
            className="btn"
            onClick={onAttachSelected}
            title={t('chat.attachTip')}
          >
            {t('common.attach')}
          </button>
          <span className="ml-auto truncate text-[10px] text-slate-400" title={t('app.activeProvider')}>
            {activeLabel}
          </span>
        </div>

        <div className="flex items-end gap-1.5">
          <textarea
            ref={textareaRef}
            className="input min-h-[54px] max-h-40 resize-y py-2 text-sm"
            rows={2}
            placeholder={t('chat.placeholder')}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <button
            type="button"
            className="btn btn-primary h-[54px] px-3"
            disabled={chat.streaming || !text.trim() || noProvider}
            onClick={() => void submit()}
          >
            {chat.streaming ? '…' : t('chat.send')}
          </button>
        </div>
        <div className="mt-1 flex items-center justify-between text-[10px] text-slate-400">
          <span>{t('chat.hint')}</span>
          {chat.usage ? (
            <span>
              {t('chat.usage', {
                provider: chat.usage.provider,
                model: chat.usage.model,
                tokens: chat.usage.usage.total_tokens,
                ms: Math.round(chat.usage.duration_ms),
              })}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );
}
