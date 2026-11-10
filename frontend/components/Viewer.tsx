'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { VersionInfo, WorkNode } from '@/lib/types';
import { fileGlyph, formatBytes, indexStatusKey, indexStatusTone } from '@/lib/utils';

import { ConfirmModal, PromptModal } from './Modal';
import { DocxViewer } from './viewers/DocxViewer';
import { ExcelViewer } from './viewers/ExcelViewer';
import { ExtractViewer } from './viewers/ExtractViewer';
import { ImageViewer } from './viewers/ImageViewer';
import { PdfViewer } from './viewers/PdfViewer';
import { TextViewer } from './viewers/TextViewer';

interface ViewerProps {
  node: WorkNode | null;
  onRefresh: () => void | Promise<void>;
  onNotify: (message: string, tone?: 'info' | 'error') => void;
  onAttach: (node: WorkNode) => void;
}

const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.tiff', '.tif', '.bmp', '.gif']);
const EXCEL_EXTENSIONS = new Set(['.xlsx', '.xls', '.xlsm', '.csv']);

export function Viewer({ node, onRefresh, onNotify, onAttach }: ViewerProps) {
  const { t } = useI18n();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<VersionInfo[] | null>(null);

  useEffect(() => {
    setVersions(null);
    setRenaming(false);
    setDeleting(false);
  }, [node?.id]);

  if (!node) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-xs text-slate-500 dark:text-zinc-400">
        <div className="text-4xl">🗂</div>
        <div className="text-sm font-medium text-slate-600 dark:text-zinc-300">
          {t('viewer.nothingTitle')}
        </div>
        <p className="max-w-xs">{t('viewer.nothingHint')}</p>
      </div>
    );
  }

  const indexText = t(indexStatusKey(node.index_status), { status: node.index_status });
  const indexTone = indexStatusTone(node.index_status);

  const rename = async (name: string) => {
    setBusy(true);
    try {
      await api.updateNode(node.id, { name });
      setRenaming(false);
      onNotify(t('tree.notify.renamed'));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.deleteNode(node.id);
      setDeleting(false);
      onNotify(t('tree.notify.deleted', { name: node.name }));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setBusy(false);
    }
  };

  const reindex = async () => {
    try {
      const result = await api.reindex(node.id);
      onNotify(t('tree.notify.queued', { count: result.queued }));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  const loadVersions = async () => {
    try {
      setVersions(await api.versions(node.id));
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  const revert = async (version: VersionInfo) => {
    try {
      await api.revertVersion(node.id, version.id);
      onNotify(t('viewer.reverted', { version: version.version_no }));
      setVersions(null);
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    }
  };

  const renderBody = () => {
    if (node.kind !== 'file') return null;
    if (EXCEL_EXTENSIONS.has(node.extension)) {
      return <ExcelViewer key={`${node.id}-${node.updated_at}`} node={node} />;
    }
    if (node.extension === '.docx') {
      return <DocxViewer key={`${node.id}-${node.updated_at}`} node={node} />;
    }
    if (node.extension === '.pdf') {
      return <PdfViewer key={`${node.id}-${node.updated_at}`} node={node} />;
    }
    if (IMAGE_EXTENSIONS.has(node.extension)) {
      return <ImageViewer node={node} onOpenNode={() => undefined} />;
    }
    if (node.view_kind === 'text') {
      return <TextViewer key={`${node.id}-${node.updated_at}`} node={node} />;
    }
    return <ExtractViewer key={`${node.id}-${node.updated_at}`} node={node} />;
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-[rgb(var(--surface))]">
      <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
        <span className="text-base">{fileGlyph(node)}</span>
        <div className="min-w-0">
          <div className="truncate text-sm font-medium" title={node.name}>
            {node.name}
          </div>
          <div className="flex items-center gap-2 text-[10px] text-slate-500 dark:text-zinc-400">
            {node.kind === 'file' ? (
              <span>{formatBytes(node.size)}</span>
            ) : (
              <span>{t('viewer.items', { count: node.children.length })}</span>
            )}
            {node.kind === 'file' ? (
              <span className={indexTone} title={node.index_error || undefined}>
                {indexText}
                {node.chunk_count ? ` · ${t('common.chunks', { count: node.chunk_count })}` : ''}
              </span>
            ) : null}
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          <button
            type="button"
            className="btn"
            onClick={() => onAttach(node)}
            title={t('viewer.attachTip')}
          >
            {t('common.attach')}
          </button>
          {node.kind === 'file' ? (
            <button
              type="button"
              className="btn"
              onClick={() => void reindex()}
              title={t('viewer.reindexTip')}
            >
              {t('common.reindex')}
            </button>
          ) : null}
          <button type="button" className="btn" onClick={() => void loadVersions()}>
            {t('common.versions')}
          </button>
          <button type="button" className="btn" onClick={() => setRenaming(true)}>
            {t('common.rename')}
          </button>
          <a className="btn" href={api.downloadUrl(node.id)}>
            {t('common.download')}
            {node.kind === 'file' ? '' : ' ZIP'}
          </a>
          <button
            type="button"
            className="btn text-rose-600 dark:text-rose-400"
            onClick={() => setDeleting(true)}
          >
            {t('common.delete')}
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1">
        {node.kind === 'file' ? (
          renderBody()
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-xs text-slate-500 dark:text-zinc-400">
            <div className="text-4xl">{node.kind === 'project' ? '📁' : '📂'}</div>
            <div className="text-sm font-medium text-slate-600 dark:text-zinc-300">{node.name}</div>
            <p>
              {t('viewer.folderHintCount', {
                count: node.children.length,
                kind: t(`viewer.kind.${node.kind}`),
              })}
            </p>
            <a className="btn btn-primary" href={api.downloadUrl(node.id)}>
              {t('common.download')} ZIP
            </a>
          </div>
        )}
      </div>

      <PromptModal
        open={renaming}
        title={t('viewer.renameTitle', { name: node.name })}
        initialValue={node.name}
        busy={busy}
        confirmLabel={t('common.rename')}
        onConfirm={rename}
        onClose={() => setRenaming(false)}
      />
      <ConfirmModal
        open={deleting}
        title={t('viewer.deleteTitle')}
        message={t('viewer.deleteMessage', { name: node.name })}
        confirmLabel={t('common.delete')}
        danger
        busy={busy}
        onConfirm={remove}
        onClose={() => setDeleting(false)}
      />

      {versions ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setVersions(null);
          }}
        >
          <div className="panel w-full max-w-md rounded-xl p-4 shadow-2xl">
            <div className="mb-2 flex items-center">
              <div className="text-sm font-semibold">{t('viewer.versionHistory')}</div>
              <button
                type="button"
                className="btn btn-ghost ml-auto"
                onClick={() => setVersions(null)}
              >
                ✕
              </button>
            </div>
            {versions.length === 0 ? (
              <div className="text-xs text-slate-500">{t('viewer.noVersions')}</div>
            ) : (
              <ul className="max-h-80 space-y-1 overflow-auto">
                {versions.map((version) => (
                  <li
                    key={version.id}
                    className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs"
                  >
                    <span className="font-mono">v{version.version_no}</span>
                    <span className="text-slate-500">{formatBytes(version.size)}</span>
                    <span className="text-slate-500">{version.created_by}</span>
                    <span className="truncate text-slate-400">{version.note || ''}</span>
                    <span className="ml-auto text-[10px] text-slate-400">
                      {new Date(version.created_at).toLocaleString()}
                    </span>
                    <button
                      type="button"
                      className="btn px-1.5 py-0.5"
                      onClick={() => void revert(version)}
                    >
                      {t('common.restore')}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

