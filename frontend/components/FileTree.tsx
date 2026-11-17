'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, type UploadEntry } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';
import { fileGlyph, formatBytes, indexStatusKey, indexStatusTone } from '@/lib/utils';

import { ConfirmModal, PromptModal } from './Modal';
import { ContextMenu, type ContextMenuEntry } from './ContextMenu';

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

const CONVERTIBLE_PDF_EXTENSIONS = new Set([
  '.docx',
  '.odt',
  '.rtf',
  '.xlsx',
  '.xlsm',
  '.ods',
  '.csv',
  '.pptx',
  '.odp',
]);

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
  const [rootMenu, setRootMenu] = useState<{ x: number; y: number } | null>(null);
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

  const convertPdf = async (node: WorkNode) => {
    try {
      const created = await api.convertToPdf(node.id);
      onNotify(t('tree.notify.converted', { name: created.name }));
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
            event.stopPropagation();
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
          className={`group flex cursor-pointer items-center gap-1.5 rounded-lg px-1.5 py-1 text-xs transition-colors ${
            isSelected
              ? 'bg-white/70 text-slate-900 shadow-sm dark:bg-white/10 dark:text-white'
              : 'hover:bg-white/45 dark:hover:bg-white/5'
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
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-1 px-2 py-1.5">
        <input
          className="input h-8 min-w-0 flex-1 py-1 text-xs"
          placeholder={t('tree.filter')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {busy ? <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" /> : null}
        <button
          type="button"
          className="icon-btn"
          title={t('tree.newProject')}
          onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            setRootMenu({ x: rect.right - 210, y: rect.bottom + 4 });
          }}
        >
          ＋
        </button>
      </div>

      <div
        className="min-h-0 flex-1 overflow-auto p-1.5"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => void rootDrop(event)}
        onContextMenu={(event) => {
          event.preventDefault();
          setRootMenu({ x: event.clientX, y: event.clientY });
        }}
      >
        {nodes.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-4 text-center text-[11px] text-slate-500 dark:text-zinc-400">
            <div className="text-2xl opacity-60">📁</div>
            <p>{t('tree.emptyTitle')}</p>
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

      <ContextMenu
        open={menu !== null}
        x={menu?.x ?? 0}
        y={menu?.y ?? 0}
        header={menu?.node.name}
        onClose={() => setMenu(null)}
        items={
          menu
            ? [
                {
                  key: 'open',
                  label: t('tree.menu.open'),
                  icon: '↗',
                  onSelect: () => onSelect(menu.node),
                },
                ...(menu.node.kind !== 'file'
                  ? [
                      {
                        key: 'folder',
                        label: t('tree.menu.newFolder'),
                        icon: '＋',
                        onSelect: () => setPrompt({ mode: 'folder', node: menu.node }),
                      },
                      {
                        key: 'upload-files',
                        label: t('tree.menu.uploadFiles'),
                        icon: '⬆',
                        onSelect: () => openUpload(menu.node, 'files'),
                      },
                      {
                        key: 'upload-folder',
                        label: t('tree.menu.uploadFolder'),
                        icon: '🗂',
                        onSelect: () => openUpload(menu.node, 'folder'),
                      },
                      {
                        key: 'upload-zip',
                        label: t('tree.menu.uploadZip'),
                        icon: '🗜',
                        onSelect: () => openUpload(menu.node, 'zip'),
                      },
                      { key: 'sep-upload', separator: true as const },
                    ]
                  : []),
                {
                  key: 'rename',
                  label: t('tree.menu.rename'),
                  icon: '✎',
                  onSelect: () => setPrompt({ mode: 'rename', node: menu.node }),
                },
                {
                  key: 'download',
                  label:
                    menu.node.kind === 'file'
                      ? t('tree.menu.downloadFile')
                      : t('tree.menu.downloadZip'),
                  icon: '⬇',
                  onSelect: () => window.location.assign(api.downloadUrl(menu.node.id)),
                },
                {
                  key: 'attach',
                  label: t('tree.menu.attach'),
                  icon: '📎',
                  onSelect: () => onAttach(menu.node),
                },
                ...(menu.node.kind === 'file'
                  ? [
                      {
                        key: 'reindex',
                        label: t('tree.menu.reindex'),
                        icon: '♻',
                        onSelect: () => void reindex(menu.node),
                      },
                      ...(CONVERTIBLE_PDF_EXTENSIONS.has(menu.node.extension)
                        ? [
                            {
                              key: 'convert-pdf',
                              label: t('tree.menu.convertPdf'),
                              icon: '📄',
                              onSelect: () => void convertPdf(menu.node),
                            },
                          ]
                        : []),
                    ]
                  : []),
                { key: 'sep-delete', separator: true as const },
                {
                  key: 'delete',
                  label: t('tree.menu.delete'),
                  icon: '🗑',
                  danger: true,
                  onSelect: () => setDeleteTarget(menu.node),
                },
              ]
            : []
        }
      />

      <ContextMenu
        open={rootMenu !== null}
        x={rootMenu?.x ?? 0}
        y={rootMenu?.y ?? 0}
        header={selectedProject?.name}
        onClose={() => setRootMenu(null)}
        items={[
          {
            key: 'new-project',
            label: t('tree.menu.newProject'),
            icon: '＋',
            onSelect: () => setPrompt({ mode: 'project' }),
          },
          { key: 'sep-root', separator: true },
          {
            key: 'upload-files',
            label: t('tree.menu.uploadFiles'),
            icon: '⬆',
            disabled: !selectedProject,
            onSelect: () => openUpload(selectedProject, 'files'),
          },
          {
            key: 'upload-folder',
            label: t('tree.menu.uploadFolder'),
            icon: '🗂',
            disabled: !selectedProject,
            onSelect: () => openUpload(selectedProject, 'folder'),
          },
          {
            key: 'upload-zip',
            label: t('tree.menu.uploadZip'),
            icon: '🗜',
            disabled: !selectedProject,
            onSelect: () => openUpload(selectedProject, 'zip'),
          },
        ]}
      />

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




