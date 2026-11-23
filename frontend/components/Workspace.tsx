'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';

import { api } from '@/lib/api';
import { useRequireAuth } from '@/lib/auth';
import { useI18n } from '@/lib/i18n';
import type { HealthResponse, IndexStatus, ProvidersResponse, WorkNode } from '@/lib/types';
import { findNode, flatten } from '@/lib/utils';
import { useChat } from '@/lib/useChat';

import { AdminPanel } from './AdminPanel';
import { ChatPanel } from './ChatPanel';
import { FileTree } from './FileTree';
import { OverlayPanel } from './Modal';
import { SettingsPanel } from './SettingsPanel';
import { Splitter } from './Splitter';
import { TopBar } from './TopBar';
import { Viewer } from './Viewer';

type MobileView = 'tree' | 'viewer' | 'chat';
type Panel = 'admin' | 'settings' | null;

const DEFAULT_CHAT_WIDTH = 460;
const MIN_CHAT_WIDTH = 320;
const MIN_VIEWER_WIDTH = 320;
const CHAT_WIDTH_KEY = 'afw-chat-width';

export default function Workspace({ initialPanel = null }: { initialPanel?: Panel }) {
  const { t } = useI18n();
  const auth = useRequireAuth();
  const router = useRouter();
  const pathname = usePathname();
  const [panel, setPanel] = useState<Panel>(initialPanel);
  const [tree, setTree] = useState<WorkNode[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [providers, setProviders] = useState<ProvidersResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [indexStatus, setIndexStatus] = useState<IndexStatus | null>(null);
  const [attachments, setAttachments] = useState<string[]>([]);
  const [toast, setToast] = useState<{ text: string; tone: 'info' | 'error' } | null>(null);
  const [mobile, setMobile] = useState<MobileView>('viewer');
  const [chatWidth, setChatWidth] = useState(DEFAULT_CHAT_WIDTH);
  const [ownerFilter, setOwnerFilter] = useState<string | null>(null);
  const layoutRef = useRef<HTMLDivElement | null>(null);

  // Administrators can open another account's workspace with ?owner=<uuid>
  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search);
      const owner = params.get('owner');
      if (owner) setOwnerFilter(owner);
    } catch {
      /* ignore */
    }
  }, []);

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

  const closePanel = useCallback(() => {
    setPanel(null);
    if (pathname && pathname !== '/') router.replace('/');
  }, [pathname, router]);

  const refreshTree = useCallback(async () => {
    try {
      setTree(await api.tree(ownerFilter));
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), 'error');
    }
  }, [notify, ownerFilter]);

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

  const selected = selectedId ? findNode(tree, selectedId) : null;

  /** The project the chat is allowed to see: the selection's project root. */
  const activeProject = useMemo(() => {
    if (!selected) return null;
    if (selected.kind === 'project') return selected;
    if (!selected.project_id) return null;
    return findNode(tree, selected.project_id);
  }, [selected, tree]);

  const activeProjectId = activeProject?.id ?? null;

  const chat = useChat({
    projectId: activeProjectId,
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

  useEffect(() => {
    const onWorkspaceChanged = () => {
      void refreshTree();
      void refreshIndex();
    };
    window.addEventListener('afw-workspace-changed', onWorkspaceChanged);
    return () => window.removeEventListener('afw-workspace-changed', onWorkspaceChanged);
  }, [refreshTree, refreshIndex]);

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

  useEffect(() => {
    if (selectedId && tree.length > 0 && !findNode(tree, selectedId)) {
      setSelectedId(null);
    }
  }, [tree, selectedId]);

  // Open the first file (or project) automatically so the canvas, project chip
  // and chat are usable immediately after login without an extra click.
  useEffect(() => {
    if (selectedId || tree.length === 0) return;
    const firstProject = tree.find((node) => node.kind === 'project') ?? tree[0];
    const firstFile = firstProject
      ? flatten([firstProject]).find((node) => node.kind === 'file')
      : null;
    const initial = firstFile ?? firstProject;
    if (initial) setSelectedId(initial.id);
  }, [tree, selectedId]);

  // Leaving a project drops its attachments: context never leaks between projects.
  useEffect(() => {
    setAttachments([]);
  }, [activeProjectId]);

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

  if (auth.loading || !auth.user) {
    return (
      <div className="flex h-screen items-center justify-center text-sm text-slate-500 dark:text-zinc-400">
        {t('common.loading')}
      </div>
    );
  }

  return (
    <div className="app-frame flex h-screen flex-col gap-2 overflow-hidden p-2">
      <TopBar
        health={health}
        onRefreshHealth={() => {
          void refreshHealth();
          void refreshProviders();
        }}
        activeProvider={chat.active.provider}
        activeModel={chat.active.model}
        indexSummary={indexSummary}
        projectName={activeProject?.name ?? null}
        viewingOwner={ownerFilter}
        onOpenAdmin={() => setPanel('admin')}
        onOpenSettings={() => setPanel('settings')}
      />

      <div className="flex items-center gap-1 rounded-xl border border-white/20 bg-white/30 p-1 text-xs backdrop-blur dark:border-white/10 dark:bg-white/5 lg:hidden">
        {(['tree', 'viewer', 'chat'] as MobileView[]).map((view) => (
          <button
            key={view}
            type="button"
            className={`flex-1 rounded-lg px-2 py-1.5 transition ${
              mobile === view
                ? 'bg-white/80 font-medium text-slate-900 shadow-sm dark:bg-white/15 dark:text-white'
                : 'text-slate-500 dark:text-zinc-400'
            }`}
            onClick={() => setMobile(view)}
          >
            {view === 'tree' ? t('app.filesTab') : view === 'viewer' ? t('app.viewerTab') : t('app.chatTab')}
          </button>
        ))}
      </div>

      <div ref={layoutRef} className="flex min-h-0 flex-1 gap-2">
        <aside
          className={`${mobile === 'tree' ? 'flex' : 'hidden'} pane min-h-0 w-full shrink-0 lg:flex lg:w-[280px]`}
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
          className={`${mobile === 'chat' ? 'flex' : 'hidden'} pane min-h-0 w-full shrink-0 lg:flex lg:w-[var(--chat-width)]`}
          style={{ '--chat-width': `${chatWidth}px` } as React.CSSProperties}
        >
          <ChatPanel
            chat={chat}
            providers={providers}
            attachments={attachmentNodes}
            projectLabel={activeProject?.name ?? null}
            onRemoveAttachment={(id) => setAttachments((prev) => prev.filter((item) => item !== id))}
            onAttachSelected={attachSelected}
            onOpenNode={(nodeId) => {
              setSelectedId(nodeId);
              setMobile('viewer');
            }}
            onSettings={() => setPanel('settings')}
            onDeleteSession={(id) => chat.removeSession(id)}
          />
        </section>

        <Splitter onResize={resizeChat} onReset={resetChatWidth} label={t('app.splitter')} />

        <main className={`${mobile === 'viewer' ? 'flex' : 'hidden'} pane min-h-0 min-w-0 flex-1 lg:flex`}>
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

      <OverlayPanel
        open={panel === 'admin'}
        title={t('admin.title')}
        subtitle={t('admin.subtitle')}
        width="max-w-6xl"
        onClose={closePanel}
      >
        <AdminPanel embedded />
      </OverlayPanel>

      <OverlayPanel
        open={panel === 'settings'}
        title={t('settings.title')}
        width="max-w-3xl"
        onClose={closePanel}
      >
        <SettingsPanel embedded />
      </OverlayPanel>

      {toast ? (
        <div
          className={`glass-strong fixed bottom-4 left-1/2 z-[90] -translate-x-1/2 rounded-full px-3.5 py-2 text-xs shadow-xl ${
            toast.tone === 'error'
              ? 'text-rose-600 dark:text-rose-400'
              : 'text-slate-700 dark:text-zinc-200'
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

