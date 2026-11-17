'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import type { OfficeSession } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { VersionInfo, WorkNode } from '@/lib/types';
import { fileGlyph, formatBytes, indexStatusKey, indexStatusTone } from '@/lib/utils';

import { ConfirmModal, Modal, PromptModal } from './Modal';
import { ContextMenu, type ContextMenuEntry } from './ContextMenu';
import { DocxViewer } from './viewers/DocxViewer';
import { ExcelViewer } from './viewers/ExcelViewer';
import { ExtractViewer } from './viewers/ExtractViewer';
import { ImageViewer } from './viewers/ImageViewer';
import { OfficeViewer } from './viewers/OfficeViewer';
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
// These files are opened in OnlyOffice when the deployment has it configured;
// otherwise the built-in viewers below stay in use (the rollback path).
const OFFICE_EXTENSIONS = new Set([
  '.docx',
  '.odt',
  '.rtf',
  '.xlsx',
  '.xlsm',
  '.ods',
  '.csv',
  '.pptx',
  '.odp',
  '.pdf',
]);
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

export function Viewer({ node, onRefresh, onNotify, onAttach }: ViewerProps) {
  const { t } = useI18n();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [versions, setVersions] = useState<VersionInfo[] | null>(null);
  const [ocrBusy, setOcrBusy] = useState(false);
  const [converting, setConverting] = useState(false);
  const [templatePrompt, setTemplatePrompt] = useState(false);
  const [savingTemplate, setSavingTemplate] = useState(false);
  const [ocrText, setOcrText] = useState<string | null>(null);
  const [ocrPages, setOcrPages] = useState(0);
  const [actionMenu, setActionMenu] = useState<{ x: number; y: number } | null>(null);
  const [officeSession, setOfficeSession] = useState<{
    nodeId: string;
    session: OfficeSession;
  } | null>(null);
  const [officeLoading, setOfficeLoading] = useState(false);
  const activeOfficeSession =
    node && officeSession?.nodeId === node.id ? officeSession.session : null;
  const officeCandidate = Boolean(
    node && node.kind === 'file' && OFFICE_EXTENSIONS.has(node.extension),
  );

  useEffect(() => {
    setVersions(null);
    setRenaming(false);
    setDeleting(false);
    setOcrText(null);
    setOcrPages(0);
    setConverting(false);
    setTemplatePrompt(false);
    setSavingTemplate(false);
    setOfficeSession(null);
    setOfficeLoading(false);
    setActionMenu(null);
  }, [node?.id]);

  useEffect(() => {
    if (!node || node.kind !== 'file' || !OFFICE_EXTENSIONS.has(node.extension)) return;
    let cancelled = false;
    setOfficeSession(null);
    setOfficeLoading(true);
    void (async () => {
      try {
        const config = await api.officeConfig();
        if (cancelled || !config.enabled) return;
        const session = await api.officeSession(node.id);
        if (!cancelled) setOfficeSession({ nodeId: node.id, session });
      } catch {
        // Any failure (backend unavailable, OnlyOffice disabled or
        // misconfigured) falls back to the built-in viewers below.
      } finally {
        if (!cancelled) setOfficeLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [node?.id]);

  if (!node) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-4xl opacity-20">🗂</div>
      </div>
    );
  }

  const indexText = t(indexStatusKey(node.index_status), { status: node.index_status });
  const indexTone = indexStatusTone(node.index_status);

  const runOcr = async () => {
    if (ocrBusy) return;
    setOcrBusy(true);
    try {
      const result = await api.ocr(node.id);
      const pages = Number(result.meta?.ocr_pages ?? 0);
      setOcrText(result.text);
      setOcrPages(pages);
      onNotify(
        t('viewer.ocrDone', { chars: result.text.length, pages }),
        result.text.trim() ? 'info' : 'error',
      );
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setOcrBusy(false);
    }
  };

  const copyOcr = async () => {
    if (!ocrText) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(ocrText);
      } else {
        // Plain HTTP on the LAN is not a secure context, so the async clipboard
        // API is unavailable; fall back to the legacy selection-based copy.
        const area = document.createElement('textarea');
        area.value = ocrText;
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(area);
        if (!ok) throw new Error('copy failed');
      }
      onNotify(t('viewer.ocrCopied'));
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    }
  };

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

  const convertPdf = async () => {
    if (converting) return;
    setConverting(true);
    try {
      const created = await api.convertToPdf(node.id);
      onNotify(t('viewer.converted', { name: created.name }));
      await onRefresh();
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setConverting(false);
    }
  };

  const saveAsTemplate = async (name: string) => {
    setSavingTemplate(true);
    try {
      const template = await api.createTemplateFromNode(node.id, { name });
      setTemplatePrompt(false);
      onNotify(t('templates.savedFromFile', { name: template.name }));
    } catch (error) {
      onNotify(error instanceof Error ? error.message : String(error), 'error');
    } finally {
      setSavingTemplate(false);
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
    if (activeOfficeSession) {
      return (
        <OfficeViewer
          key={node.id}
          node={node}
          session={activeOfficeSession}
          onSaved={onRefresh}
        />
      );
    }
    if (officeCandidate && officeLoading) {
      return <div className="p-4 text-xs text-slate-500">{t('office.connecting')}</div>;
    }
    if (EXCEL_EXTENSIONS.has(node.extension)) {
      return <ExcelViewer key={`${node.id}-${node.updated_at}`} node={node} onSaved={onRefresh} />;
    }
    if (node.extension === '.docx') {
      return <DocxViewer key={`${node.id}-${node.updated_at}`} node={node} />;
    }
    if (node.extension === '.pdf') {
      return <PdfViewer key={`${node.id}-${node.updated_at}`} node={node} onSaved={onRefresh} />;
    }
    if (IMAGE_EXTENSIONS.has(node.extension)) {
      return <ImageViewer node={node} onOpenNode={() => undefined} />;
    }
    if (node.view_kind === 'text') {
      return <TextViewer key={`${node.id}-${node.updated_at}`} node={node} onSaved={onRefresh} />;
    }
    return <ExtractViewer key={`${node.id}-${node.updated_at}`} node={node} />;
  };

  const actionItems: ContextMenuEntry[] = [
    {
      key: 'attach',
      label: t('menu.attach'),
      icon: '📎',
      onSelect: () => onAttach(node),
    },
    ...(node.kind === 'file' && node.extension === '.pdf'
      ? [
          {
            key: 'ocr',
            label: ocrBusy ? t('viewer.ocrBusy') : t('viewer.ocr'),
            icon: '🔍',
            disabled: ocrBusy,
            onSelect: () => void runOcr(),
          },
        ]
      : []),
    ...(node.kind === 'file' && CONVERTIBLE_PDF_EXTENSIONS.has(node.extension)
      ? [
          {
            key: 'convert-pdf',
            label: converting ? t('viewer.converting') : t('viewer.convertPdf'),
            icon: '📄',
            disabled: converting,
            onSelect: () => void convertPdf(),
          },
        ]
      : []),
    ...(node.kind === 'file'
      ? [
          {
            key: 'reindex',
            label: t('menu.reindex'),
            icon: '♻',
            onSelect: () => void reindex(),
          },
          ...(['.docx', '.xlsx'].includes(node.extension)
            ? [
                {
                  key: 'save-template',
                  label: t('templates.saveAsTemplate'),
                  icon: '📋',
                  onSelect: () => setTemplatePrompt(true),
                },
              ]
            : []),
        ]
      : []),
    {
      key: 'versions',
      label: t('menu.versions'),
      icon: '🕘',
      onSelect: () => void loadVersions(),
    },
    {
      key: 'rename',
      label: t('common.rename'),
      icon: '✎',
      onSelect: () => setRenaming(true),
    },
    {
      key: 'download',
      label: node.kind === 'file' ? t('common.download') : `${t('common.download')} ZIP`,
      icon: '⬇',
      onSelect: () => window.location.assign(api.downloadUrl(node.id)),
    },
    { key: 'sep', separator: true },
    {
      key: 'delete',
      label: t('common.delete'),
      icon: '🗑',
      danger: true,
      onSelect: () => setDeleting(true),
    },
  ];

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className="flex items-center gap-2 px-3 py-2"
        onContextMenu={(event) => {
          event.preventDefault();
          setActionMenu({ x: event.clientX, y: event.clientY });
        }}
      >
        <span className="text-base">{fileGlyph(node)}</span>
        <div className="min-w-0 flex-1">
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
        <button
          type="button"
          className="icon-btn"
          title={t('viewer.actions')}
          aria-label={t('viewer.actions')}
          onClick={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            setActionMenu({ x: rect.right - 220, y: rect.bottom + 4 });
          }}
        >
          ⋯
        </button>
      </div>

      <ContextMenu
        open={actionMenu !== null}
        x={actionMenu?.x ?? 0}
        y={actionMenu?.y ?? 0}
        header={node.name}
        onClose={() => setActionMenu(null)}
        items={actionItems}
      />

      <div className="min-h-0 flex-1">
        {node.kind === 'file' ? (
          renderBody()
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-xs text-slate-500 dark:text-zinc-400">
            <div className="text-4xl">{node.kind === 'project' ? '📁' : '📂'}</div>
            <div className="text-sm font-medium text-slate-600 dark:text-zinc-300">{node.name}</div>
            <a className="btn btn-primary" href={api.downloadUrl(node.id)}>
              {t('common.download')} ZIP
            </a>
          </div>
        )}
      </div>

      <Modal
        open={ocrText !== null}
        title={t('viewer.ocrTitle', { name: node.name })}
        subtitle={t('viewer.ocrHint')}
        width="max-w-3xl"
        onClose={() => setOcrText(null)}
        footer={
          <>
            <button
              type="button"
              className="btn"
              onClick={() => void copyOcr()}
              disabled={!ocrText}
            >
              {t('viewer.ocrCopy')}
            </button>
            <button type="button" className="btn" onClick={() => setOcrText(null)}>
              {t('common.close')}
            </button>
          </>
        }
      >
        {ocrText?.trim() ? (
          <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-slate-50 p-3 font-mono text-xs text-slate-800 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100">
            {ocrText}
          </pre>
        ) : (
          <p className="text-xs text-slate-500">{t('viewer.ocrEmpty')}</p>
        )}
        {ocrPages ? (
          <p className="mt-2 text-[10px] text-slate-500">
            {t('viewer.ocrMeta', { pages: ocrPages })}
          </p>
        ) : null}
      </Modal>

      <PromptModal
        open={renaming}
        title={t('viewer.renameTitle', { name: node.name })}
        initialValue={node.name}
        busy={busy}
        confirmLabel={t('common.rename')}
        onConfirm={rename}
        onClose={() => setRenaming(false)}
      />
      <PromptModal
        open={templatePrompt}
        title={t('templates.saveAsTemplateTitle', { name: node.name })}
        initialValue={node.name.replace(/\.[^.]+$/, '')}
        busy={savingTemplate}
        confirmLabel={t('templates.saveAsTemplate')}
        onConfirm={saveAsTemplate}
        onClose={() => setTemplatePrompt(false)}
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

