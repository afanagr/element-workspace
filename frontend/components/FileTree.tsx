'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, type UploadEntry } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';
import { fileGlyph, formatBytes, indexStatusKey, indexStatusTone } from '@/lib/utils';

import { ConfirmModal, PromptModal } from './Modal';

interface FileTreeProps {
  nodes: WorkNode[];
  selectedId: string | null;
  onSelect: (node: WorkNode) => void;
  onRefresh: () => void | Promise<void>;
  onAttach: (node: WorkNode) => void;
  onNotify: (message: string, tone?: 'info' | 'error') => void;
}

type MenuState = { x: number; y: number; node: WorkNode } | null;
type PromptState =
  | { mode: 'project'; node?: undefined }
  | { mode: 'folder'; node: WorkNode }
  | { mode: 'rename'; node: WorkNode }
  | null;

async function entriesFromDataTransfer(dataTransfer: DataTransfer): Promise<UploadEntry[]> {
  const output: UploadEntry[] = [];
  const items = Array.from(dataTransfer.items || []);

  const walk = async (entry: any, prefix: string): Promise<void> => {
    if (entry.isFile) {
      const file: File = await new Promise((resolve, reject) => entry.file(resolve, reject));
      output.push({ file, path: prefix ? `${prefix}/${file.name}` : file.name });
      return;
    }
    if (entry.isDirectory) {
      const reader = entry.createReader();
      const readBatch = () =>
        new Promise<any[]>((resolve, reject) => reader.readEntries(resolve, reject));
      let batch = await readBatch();
      while (batch.length > 0) {
        for (const child of batch) {
          await walk(child, prefix ? `${prefix}/${entry.name}` : entry.name);
        }
        batch = await readBatch();
      }
    }
  };

  for (const item of items) {
    const entry = (item as any).webkitGetAsEntry?.();
    if (entry) await walk(entry, '');
  }
  if (output.length === 0) {
    for (const file of Array.from(dataTransfer.files || [])) {
      output.push({ file, path: (file as any).webkitRelativePath || file.name });
    }
  }
  return output;
}

export function FileTree({
  nodes,
  selectedId,
  onSelect,
  onRefresh,
  onAttach,
  onNotify,
}: FileTreeProps) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [menu, setMenu] = useState<MenuState>(null);
  const [prompt, setPrompt] = useState<PromptState>(null);
  const [deleteTarget, setDeleteTarget] = useState<WorkNode | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const uploadTarget = useRef<WorkNode | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const zipInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setExpanded((prev) => {
      const next = new Set(prev);
      for (const node of nodes) if (node.kind === 'project') next.add(node.id);
      return next;
    });
  }, [nodes]);

  useEffect(() => {
    if (folderInputRef.current) {
      folderInputRef.current.setAttribute('webkitdirectory', '');
      folderInputRef.current.setAttribute('directory', '');
    }
  }, []);

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener('click', close);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('click', close);
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [menu]);

  const toggle = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const doUpload = useCallback(
    async (target: WorkNode, entries: UploadEntry[]) => {
      if (entries.length === 0) return;
      setBusy(true);
      try {
        const result = await api.upload(target.id, entries);
        if (result.errors.length > 0) {
          onNotify(result.errors.slice(0, 3).join('\n'), 'error');
        }
        if (result.items.length > 0) {
          onNotify(t('tree.notify.uploaded', { count: result.items.length, name: target.name }));
          setExpanded((prev) => new Set(prev).add(target.id));
        }
        await onRefresh();
      } catch (error) {
        onNotify(error instanceof Error ? error.message : String(error), 'error');
      } finally {
        setBusy(false);
      }
    },
    [onNotify, onRefresh, t],
  );

  const handleFiles = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const target = uploadTarget.current;
    const list = Array.from(event.target.files || []);
    event.target.value = '';
    if (!target || list.length === 0) return;
    await doUpload(
      target,
      list.map((file) => ({ file, path: (file as any).webkitRelativePath || file.name })),
    );
  };

  const handleZip = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const target = uploadTarget.current;
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!target || !file) return;
    setBusy(true);
    try {
      const result = await api.extract(target.id, file);
      onNotify(t('tree.notify.extracted', { count: result.count, name: target.name }));
      setExpanded((prev) => new Set(prev).add(target.id));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(false);
    }
  };

  const submitPrompt = async (value: string) => {
    if (!prompt) return;
    setBusy(true);
    try {
      if (prompt.mode === 'project') {
        const node = await api.createProject(value);
        setExpanded((prev) => new Set(prev).add(node.id));
        onSelect(node);
        onNotify(t('tree.notify.projectCreated', { name: node.name }));
      } else if (prompt.mode === 'folder') {
        await api.createFolder(prompt.node.id, value);
        setExpanded((prev) => new Set(prev).add(prompt.node.id));
        onNotify(t('tree.notify.folderCreated', { name: value }));
      } else {
        await api.updateNode(prompt.node.id, { name: value });
        onNotify(t('tree.notify.renamed'));
      }
      setPrompt(null);
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await api.deleteNode(deleteTarget.id);
      onNotify(t('tree.notify.deleted', { name: deleteTarget.name }));
      setDeleteTarget(null);
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(false);
    }
  };

  const reindex = async (node: WorkNode) => {
    try {
      const result = await api.reindex(node.id);
      onNotify(t('tree.notify.queued', { count: result.queued }));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  const onDropOnNode = async (node: WorkNode, event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setDragOverId(null);
    if (node.kind === 'file') return;
    const internalId = event.dataTransfer.getData('application/x-afw-node');
    if (internalId) {
      if (internalId === node.id) return;
      try {
        await api.updateNode(internalId, { parent_id: node.id });
        onNotify(t('tree.notify.moved'));
        setExpanded((prev) => new Set(prev).add(node.id));
        await onRefresh();
      } catch (error) {
        onNotify(error instanceof Error ? error.message : String(error), 'error');
      }
      return;
    }
    const entries = await entriesFromDataTransfer(event.dataTransfer);
    await doUpload(node, entries);
  };

  const rootDrop = async (event: React.DragEvent) => {
    event.preventDefault();
    const internalId = event.dataTransfer.getData('application/x-afw-node');
    if (internalId) return;
    const entries = await entriesFromDataTransfer(event.dataTransfer);
    if (entries.length === 0) return;
    const firstProject = nodes.find((node) => node.kind === 'project');
    if (!firstProject) {
      onNotify(t('tree.notify.needProject'), 'error');
      return;
    }
    await doUpload(firstProject, entries);
  };

  const [query, setQuery] = useState('');
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return null;
    const output: { node: WorkNode; depth: number }[] = [];
    const walk = (list: WorkNode[], depth: number) => {
      for (const node of list) {
        if (node.name.toLowerCase().includes(term)) output.push({ node, depth });
        if (node.children?.length) walk(node.children, depth + 1);
      }
    };
    walk(nodes, 0);
    return output;
  }, [nodes, query]);

  const selectedProject = useMemo(() => {
    if (!selectedId) return nodes.find((node) => node.kind === 'project') ?? null;
    const stack = [...nodes];
    let current: WorkNode | null = null;
    while (stack.length) {
      const node = stack.pop() as WorkNode;
      if (node.id === selectedId) current = node;
      if (node.children?.length) stack.push(...node.children);
    }
    if (!current) return nodes.find((node) => node.kind === 'project') ?? null;
    if (current.kind !== 'file') return current;
    if (current.project_id) {
      const found = nodes.find((node) => node.id === current?.project_id);
      if (found) return found;
    }
    return nodes.find((node) => node.kind === 'project') ?? null;
  }, [nodes, selectedId]);

  const fileCount = useMemo(() => {
    let count = 0;
    const walk = (list: WorkNode[]) => {
      for (const node of list) {
        if (node.kind === 'file') count += 1;
        if (node.children?.length) walk(node.children);
      }
    };
    walk(nodes);
    return count;
  }, [nodes]);

  const openUpload = (node: WorkNode | null, mode: 'files' | 'folder' | 'zip') => {
    if (!node) {
      onNotify(t('tree.notify.needSelection'), 'error');
      return;
    }
    uploadTarget.current = node;
    if (mode === 'files') fileInputRef.current?.click();
    if (mode === 'folder') folderInputRef.current?.click();
    if (mode === 'zip') zipInputRef.current?.click();
  };

  const renderRow = (node: WorkNode, depth: number): React.ReactNode => {
    const isSelected = selectedId === node.id;
    const isOpen = expanded.has(node.id);
    const indexText = t(indexStatusKey(node.index_status), { status: node.index_status });
    const indexTone = indexStatusTone(node.index_status);
    return (
      <div key={node.id}>
        <div
          draggable
          onDragStart={(event) => {
            event.dataTransfer.setData('application/x-afw-node', node.id);
            event.dataTransfer.effectAllowed = 'move';
          }}
          onClick={() => {
            onSelect(node);
            if (node.kind !== 'file') toggle(node.id);
          }}
          onContextMenu={(event) => {
            event.preventDefault();
            setMenu({ x: event.clientX, y: event.clientY, node });
          }}
          onDragOver={(event) => {
            if (node.kind === 'file') return;
            event.preventDefault();
            setDragOverId(node.id);
          }}
          onDragLeave={() => setDragOverId((current) => (current === node.id ? null : current))}
          onDrop={(event) => void onDropOnNode(node, event)}
          style={{ paddingLeft: 6 + depth * 12 }}
          className={`group flex cursor-pointer items-center gap-1.5 rounded px-1.5 py-1 text-xs ${
            isSelected
              ? 'bg-indigo-50 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300'
              : 'hover:bg-slate-100 dark:hover:bg-zinc-800'
          } ${dragOverId === node.id ? 'ring-2 ring-indigo-400' : ''}`}
          title={
            node.kind === 'file'
              ? t(node.index_error ? 'tree.indexTitleError' : 'tree.indexTitle', {
                  name: node.name,
                  size: formatBytes(node.size),
                  status: indexText,
                  error: node.index_error ?? '',
                })
              : node.name
          }
        >
          <span className="w-2.5 shrink-0 text-[9px] text-slate-400">
            {node.kind === 'file' ? '' : isOpen ? '▼' : '▶'}
          </span>
          <span className="shrink-0">{fileGlyph(node)}</span>
          <span className="flex-1 truncate">{node.name}</span>
          {node.kind === 'file' ? (
            <span className={`shrink-0 text-[9px] ${indexTone}`}>●</span>
          ) : null}
        </div>
        {node.kind !== 'file' && isOpen
          ? node.children.map((child) => renderRow(child, depth + 1))
          : null}
      </div>
    );
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-[rgb(var(--surface))]">
      <div className="flex items-center gap-1 border-b px-2 py-1.5">
        <button
          type="button"
          className="btn btn-primary flex-1"
          onClick={() => setPrompt({ mode: 'project' })}
        >
          {t('tree.newProject')}
        </button>
        <button
          type="button"
          className="btn"
          title={t('tree.uploadFilesTip')}
          onClick={() => openUpload(selectedProject, 'files')}
        >
          ⬆
        </button>
        <button
          type="button"
          className="btn"
          title={t('tree.uploadFolderTip')}
          onClick={() => openUpload(selectedProject, 'folder')}
        >
          🗂
        </button>
      </div>

      <div className="border-b px-2 py-1">
        <input
          className="input py-1 text-xs"
          placeholder={t('tree.filter')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>

      <div
        className="min-h-0 flex-1 overflow-auto p-1.5"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => void rootDrop(event)}
      >
        {nodes.length === 0 ? (
          <div className="m-2 rounded-lg border border-dashed p-4 text-center text-[11px] text-slate-500 dark:text-zinc-400">
            <div className="mb-2 text-2xl">📁</div>
            <p className="mb-2">{t('tree.emptyTitle')}</p>
            <p className="mb-3">{t('tree.emptyHint')}</p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setPrompt({ mode: 'project' })}
            >
              {t('tree.createFirst')}
            </button>
          </div>
        ) : filtered ? (
          filtered.length === 0 ? (
            <div className="p-3 text-[11px] text-slate-500 dark:text-zinc-400">
              {t('tree.noMatches')}
            </div>
          ) : (
            filtered.map(({ node, depth }) => renderRow(node, depth))
          )
        ) : (
          nodes.map((node) => renderRow(node, 0))
        )}
      </div>

      {busy ? (
        <div className="border-t px-2 py-1 text-[10px] text-amber-600 dark:text-amber-400">
          {t('common.working')}
        </div>
      ) : null}
      <div className="border-t px-2 py-1 text-[10px] text-slate-500 dark:text-zinc-500">
        {t('tree.footer', { count: fileCount })}
      </div>

      <input
        ref={fileInputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => void handleFiles(event)}
      />
      <input
        ref={folderInputRef}
        type="file"
        multiple
        hidden
        onChange={(event) => void handleFiles(event)}
      />
      <input
        ref={zipInputRef}
        type="file"
        accept=".zip"
        hidden
        onChange={(event) => void handleZip(event)}
      />

      {menu ? (
        <div
          className="panel fixed z-50 w-52 rounded-lg p-1 shadow-2xl"
          style={{
            left: Math.min(menu.x, window.innerWidth - 220),
            top: Math.min(menu.y, window.innerHeight - 330),
          }}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="truncate px-2 py-1 text-[10px] uppercase tracking-wide text-slate-400">
            {menu.node.name}
          </div>
          {menu.node.kind !== 'file' ? (
            <>
              <button
                type="button"
                className="menu-item"
                onClick={() => {
                  setPrompt({ mode: 'folder', node: menu.node });
                  setMenu(null);
                }}
              >
                {t('tree.menu.newFolder')}
              </button>
              <button
                type="button"
                className="menu-item"
                onClick={() => {
                  openUpload(menu.node, 'files');
                  setMenu(null);
                }}
              >
                {t('tree.menu.uploadFiles')}
              </button>
              <button
                type="button"
                className="menu-item"
                onClick={() => {
                  openUpload(menu.node, 'folder');
                  setMenu(null);
                }}
              >
                {t('tree.menu.uploadFolder')}
              </button>
              <button
                type="button"
                className="menu-item"
                onClick={() => {
                  openUpload(menu.node, 'zip');
                  setMenu(null);
                }}
              >
                {t('tree.menu.uploadZip')}
              </button>
              <div className="divider my-1" />
            </>
          ) : null}
          <button
            type="button"
            className="menu-item"
            onClick={() => {
              setPrompt({ mode: 'rename', node: menu.node });
              setMenu(null);
            }}
          >
            {t('tree.menu.rename')}
          </button>
          <a className="menu-item" href={api.downloadUrl(menu.node.id)} onClick={() => setMenu(null)}>
            {menu.node.kind === 'file' ? t('tree.menu.downloadFile') : t('tree.menu.downloadZip')}
          </a>
          <button
            type="button"
            className="menu-item"
            onClick={() => {
              onAttach(menu.node);
              setMenu(null);
            }}
          >
            {t('tree.menu.attach')}
          </button>
          <button
            type="button"
            className="menu-item"
            onClick={() => {
              void reindex(menu.node);
              setMenu(null);
            }}
          >
            {t('tree.menu.reindex')}
          </button>
          <div className="divider my-1" />
          <button
            type="button"
            className="menu-item text-rose-600 dark:text-rose-400"
            onClick={() => {
              setDeleteTarget(menu.node);
              setMenu(null);
            }}
          >
            {t('tree.menu.delete')}
          </button>
        </div>
      ) : null}

      <PromptModal
        open={prompt !== null}
        title={
          prompt?.mode === 'project'
            ? t('tree.prompt.newProject')
            : prompt?.mode === 'folder'
              ? t('tree.prompt.newFolder', { name: prompt.node.name })
              : t('tree.prompt.rename', { name: prompt?.node?.name ?? '' })
        }
        initialValue={prompt?.mode === 'rename' ? prompt.node.name : ''}
        placeholder={
          prompt?.mode === 'project' ? t('tree.prompt.placeholderProject') : t('tree.prompt.placeholderName')
        }
        confirmLabel={prompt?.mode === 'rename' ? t('common.rename') : t('common.create')}
        busy={busy}
        onConfirm={submitPrompt}
        onClose={() => setPrompt(null)}
      />

      <ConfirmModal
        open={deleteTarget !== null}
        title={t('tree.deleteTitle')}
        message={t('tree.deleteMessage', {
          name: deleteTarget?.name ?? '',
          nested:
            deleteTarget && deleteTarget.kind !== 'file' ? t('tree.deleteNested') : '',
        })}
        confirmLabel={t('common.delete')}
        danger
        busy={busy}
        onConfirm={confirmDelete}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
}




