'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { HealthResponse, IndexStatus, ProvidersResponse, WorkNode } from '@/lib/types';
import { findNode, flatten } from '@/lib/utils';
import { useChat } from '@/lib/useChat';

import { ChatPanel } from './ChatPanel';
import { FileTree } from './FileTree';
import { Splitter } from './Splitter';
import { TopBar } from './TopBar';
import { Viewer } from './Viewer';

type MobileView = 'tree' | 'viewer' | 'chat';

const DEFAULT_CHAT_WIDTH = 460;
const MIN_CHAT_WIDTH = 320;
const MIN_VIEWER_WIDTH = 320;
const CHAT_WIDTH_KEY = 'afw-chat-width';

export default function Workspace() {
  const { t } = useI18n();
  const [tree, setTree] = useState<WorkNode[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [attachments, setAttachments] = useState<string[]>([]);
  const [toast, setToast] = useState<{ text: string; tone: 'info' | 'error' } | null>(null);
  const [mobile, setMobile] = useState<MobileView>('viewer');
  const [chatWidth, setChatWidth] = useState(DEFAULT_CHAT_WIDTH);
  const layoutRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    try {
      const stored = Number(window.localStorage.getItem(CHAT_WIDTH_KEY));
      if (Number.isFinite(stored) && stored >= MIN_CHAT_WIDTH) setChatWidth(stored);
    } catch {
      /* ignore */
    }
  }, []);

  const resizeChat = useCallback((delta: number) => {
    setChatWidth((current) => {
      const containerWidth = layoutRef.current?.clientWidth ?? window.innerWidth;
      const max = Math.max(MIN_CHAT_WIDTH, containerWidth - MIN_VIEWER_WIDTH - 300);
      const next = Math.min(Math.max(current + delta, MIN_CHAT_WIDTH), max);
      try {
        window.localStorage.setItem(CHAT_WIDTH_KEY, String(Math.round(next)));
      } catch {
        /* ignore */
      }
      return next;
    });
  }, []);

  const resetChatWidth = useCallback(() => {
    setChatWidth(DEFAULT_CHAT_WIDTH);
    try {
      window.localStorage.setItem(CHAT_WIDTH_KEY, String(DEFAULT_CHAT_WIDTH));
    } catch {
      /* ignore */
    }
  }, []);


  const notify = useCallback((text: string, tone: 'info' | 'error' = 'info') => {
    setToast({ text, tone });
  }, []);

  const refreshTree = useCallback(async () => {
    try {
      setTree(await api.tree());
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'error');
    }
  }, [notify]);

  const refreshProviders = useCallback(async () => {
    try {
      setProviders(await api.providers());
    } catch {
      /* provider list is optional for rendering */
    }
  }, []);

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      setHealth(null);
    }
  }, []);

  const refreshIndex = useCallback(async () => {
    try {
      setIndexStatus(await api.indexStatus());
    } catch {
      /* ignore */
    }
  }, []);

  const chat = useChat({
    onWorkspaceChanged: () => {
      void refreshTree();
      void refreshIndex();
    },
    onOpenCitation: (nodeId) => setSelectedId(nodeId),
  });

  useEffect(() => {
    void refreshTree();
    void refreshProviders();
    void refreshHealth();
    void refreshIndex();
  }, [refreshTree, refreshProviders, refreshHealth, refreshIndex]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), toast.tone === 'error' ? 8000 : 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  const hasPending = useMemo(
    () =>
      flatten(tree).some(
        (node) => node.index_status === 'pending' || node.index_status === 'indexing',
      ),
    [tree],
  );

  useEffect(() => {
    if (!hasPending) return;
    const timer = setInterval(() => {
      void refreshTree();
      void refreshIndex();
    }, 4000);
    return () => clearInterval(timer);
  }, [hasPending, refreshTree, refreshIndex]);

  const selected = selectedId ? findNode(tree, selectedId) : null;

  useEffect(() => {
    if (selectedId && tree.length > 0 && !findNode(tree, selectedId)) {
      setSelectedId(null);
    }
  }, [tree, selectedId]);

  const attach = useCallback(
    (node: WorkNode) => {
      setAttachments((prev) => (prev.includes(node.id) ? prev : [...prev, node.id]));
      notify(t('app.noticeAttached', { name: node.name }));
    },
    [notify, t],
  );

  const attachSelected = useCallback(() => {
    if (!selected) {
      notify(t('app.noticeSelectFirst'), 'error');
      return;
    }
    attach(selected);
  }, [selected, attach, notify, t]);

  const attachmentNodes = useMemo(
    () => attachments.map((id) => findNode(tree, id)).filter((node): node is WorkNode => Boolean(node)),
    [attachments, tree],
  );

  const indexSummary = useMemo(() => {
    if (!indexStatus) return undefined;
    const values = Object.values(indexStatus.files);
    const total = values.reduce((sum, value) => sum + value, 0);
    if (total === 0) return t('app.indexSummaryNoFiles');
    const indexed = indexStatus.files.indexed ?? 0;
    return t('app.indexSummary', { indexed, total, chunks: indexStatus.chunks });
  }, [indexStatus, t]);

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <TopBar
        health={health}
        onRefreshHealth={() => {
          void refreshHealth();
          void refreshProviders();
        }}
        activeProvider={chat.active.provider}
        activeModel={chat.active.model}
        indexSummary={indexSummary}
      />

      <div className="flex items-center gap-1 border-b px-2 py-1 lg:hidden">
        {(['tree', 'viewer', 'chat'] as MobileView[]).map((view) => (
          <button
            key={view}
            type="button"
            className={`btn flex-1 ${mobile === view ? 'btn-primary' : ''}`}
            onClick={() => setMobile(view)}
          >
            {view === 'tree' ? t('app.filesTab') : view === 'viewer' ? t('app.viewerTab') : t('app.chatTab')}
          </button>
        ))}
      </div>

      {health?.banner ? (
        <div className="flex items-center gap-2 border-b border-amber-300 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span className="flex-1">{health.banner}</span>
          <a className="btn" href="/settings">
            {t('app.openSettings')}
          </a>
        </div>
      ) : null}

      <div ref={layoutRef} className="flex min-h-0 flex-1">
        <aside
          className={`${mobile === 'tree' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col border-r lg:flex lg:w-[280px]`}
        >
          <FileTree
            nodes={tree}
            selectedId={selectedId}
            onSelect={(node) => {
              setSelectedId(node.id);
              setMobile('viewer');
            }}
            onRefresh={refreshTree}
            onAttach={(node) => {
              attach(node);
              setMobile('chat');
            }}
            onNotify={notify}
          />
        </aside>

        <section
          className={`${mobile === 'chat' ? 'flex' : 'hidden'} min-h-0 w-full shrink-0 flex-col bg-[rgb(var(--surface))] lg:flex lg:w-[var(--chat-width)]`}
          style={{ '--chat-width': `${chatWidth}px` } as React.CSSProperties}
        >
          <ChatPanel
            chat={chat}
            providers={providers}
            attachments={attachmentNodes}
            onRemoveAttachment={(id) => setAttachments((prev) => prev.filter((item) => item !== id))}
            onAttachSelected={attachSelected}
            onOpenNode={(nodeId) => {
              setSelectedId(nodeId);
              setMobile('viewer');
            }}
            onSettings={() => {
              window.location.href = '/settings';
            }}
          />
        </section>

        <Splitter onResize={resizeChat} onReset={resetChatWidth} label={t('app.splitter')} />

        <main
          className={`${mobile === 'viewer' ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-1 flex-col border-l lg:flex lg:border-l-0`}
        >
          <Viewer
            node={selected}
            onRefresh={refreshTree}
            onNotify={notify}
            onAttach={(node) => {
              attach(node);
              setMobile('chat');
            }}
          />
        </main>
      </div>

      {toast ? (
        <div
          className={`fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg px-3 py-2 text-xs shadow-xl ${
            toast.tone === 'error'
              ? 'bg-rose-600 text-white'
              : 'bg-slate-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
          }`}
          onClick={() => setToast(null)}
          role="status"
        >
          {toast.text}
        </div>
      ) : null}
    </div>
  );
}

