'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';

import { Modal } from '../Modal';

interface PdfDocument {
  numPages: number;
  getPage: (page: number) => Promise<any>;
}

interface PageItem {
  key: string;
  sourceId: string;
  sourceName: string;
  page: number;
  rotate: number;
}

function collectPdfs(nodes: WorkNode[], excludeId: string): WorkNode[] {
  const result: WorkNode[] = [];
  const walk = (items: WorkNode[]) => {
    for (const item of items) {
      if (item.kind === 'file' && item.extension === '.pdf' && item.id !== excludeId) {
        result.push(item);
      }
      if (item.children?.length) walk(item.children);
    }
  };
  walk(nodes);
  return result;
}

async function loadPdfDocument(pdfjs: any, nodeId: string): Promise<PdfDocument> {
  const response = await fetch(api.contentUrl(nodeId));
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = new Uint8Array(await response.arrayBuffer());
  return (await pdfjs.getDocument({ data }).promise) as PdfDocument;
}

export function PdfViewer({ node, onSaved }: { node: WorkNode; onSaved?: () => void }) {
  const { t } = useI18n();
  const [pdf, setPdf] = useState<PdfDocument | null>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [items, setItems] = useState<PageItem[]>([]);
  const [selected, setSelected] = useState(0);
  const [saving, setSaving] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [library, setLibrary] = useState<WorkNode[] | null>(null);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [libraryError, setLibraryError] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const renderTaskRef = useRef<any>(null);
  const docsRef = useRef<Map<string, PdfDocument>>(new Map());
  const pdfjsRef = useRef<any>(null);
  const seqRef = useRef(0);
  const initialRef = useRef('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPdf(null);
    setPage(1);
    setEditing(false);
    setItems([]);
    setSelected(0);
    docsRef.current.clear();
    void (async () => {
      try {
        const pdfjs = await import('pdfjs-dist');
        pdfjs.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.mjs';
        pdfjsRef.current = pdfjs;
        const document = await loadPdfDocument(pdfjs, node.id);
        if (cancelled) return;
        docsRef.current.set(node.id, document);
        setPdf(document);
        setPages(document.numPages);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [node.id, node.updated_at]);

  const getDocFor = useCallback(async (sourceId: string): Promise<PdfDocument> => {
    const cached = docsRef.current.get(sourceId);
    if (cached) return cached;
    const pdfjs = pdfjsRef.current ?? (await import('pdfjs-dist'));
    pdfjsRef.current = pdfjs;
    const document = await loadPdfDocument(pdfjs, sourceId);
    docsRef.current.set(sourceId, document);
    return document;
  }, []);

  useEffect(() => {
    if (!pdf) return;
    const item = editing
      ? items[selected]
      : { sourceId: node.id, page, rotate: 0 };
    if (!item) return;
    let cancelled = false;
    void (async () => {
      try {
        const document = await getDocFor(item.sourceId);
        const pdfPage = await document.getPage(item.page);
        if (cancelled) return;
        const rotation = ((pdfPage.rotate || 0) + item.rotate) % 360;
        const viewport = pdfPage.getViewport({ scale: 1.4, rotation });
        const canvas = canvasRef.current;
        if (!canvas) return;
        const context = canvas.getContext('2d');
        if (!context) return;
        canvas.width = viewport.width;
        canvas.height = viewport.height;
        if (renderTaskRef.current) {
          try {
            renderTaskRef.current.cancel();
          } catch {
            /* ignore */
          }
        }
        renderTaskRef.current = pdfPage.render({ canvasContext: context, viewport });
        await renderTaskRef.current.promise;
      } catch (err) {
        if (!cancelled && (err as Error)?.name !== 'RenderingCancelledException') {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [pdf, page, editing, items, selected, node.id, getDocFor]);

  const nextKey = () => `p${(seqRef.current += 1)}`;

  const startEdit = () => {
    const list: PageItem[] = Array.from({ length: pages }, (_, index) => ({
      key: nextKey(),
      sourceId: node.id,
      sourceName: node.name,
      page: index + 1,
      rotate: 0,
    }));
    setItems(list);
    setSelected(Math.min(Math.max(page - 1, 0), Math.max(list.length - 1, 0)));
    initialRef.current = JSON.stringify(list);
    setEditing(true);
    setError(null);
  };

  const cancelEdit = () => {
    if (JSON.stringify(items) !== initialRef.current && !window.confirm(t('pdf.discardConfirm'))) {
      return;
    }
    setEditing(false);
    setItems([]);
    setError(null);
  };

  const rotateSelected = () =>
    setItems((prev) =>
      prev.map((item, index) =>
        index === selected ? { ...item, rotate: (item.rotate + 90) % 360 } : item,
      ),
    );

  const removeSelected = () => {
    if (!items.length) return;
    setItems((prev) => prev.filter((_, index) => index !== selected));
    setSelected((value) => Math.min(value, Math.max(items.length - 2, 0)));
  };

  const moveSelected = (delta: number) => {
    const target = selected + delta;
    if (target < 0 || target >= items.length) return;
    setItems((prev) => {
      const next = [...prev];
      [next[selected], next[target]] = [next[target], next[selected]];
      return next;
    });
    setSelected(target);
  };

  const openAdd = async () => {
    setAddOpen(true);
    setLibrary(null);
    setLibraryError(null);
    setLibraryLoading(true);
    try {
      const tree = await api.projectTree(node.project_id ?? '');
      setLibrary(collectPdfs(tree, node.id));
    } catch (err) {
      setLibraryError(err instanceof Error ? err.message : String(err));
    } finally {
      setLibraryLoading(false);
    }
  };

  const appendPdf = async (file: WorkNode) => {
    setAdding(file.id);
    setLibraryError(null);
    try {
      const document = await getDocFor(file.id);
      const appended: PageItem[] = Array.from({ length: document.numPages }, (_, index) => ({
        key: nextKey(),
        sourceId: file.id,
        sourceName: file.name,
        page: index + 1,
        rotate: 0,
      }));
      setItems((prev) => [...prev, ...appended]);
      setAddOpen(false);
    } catch (err) {
      setLibraryError(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(null);
    }
  };

  const save = async () => {
    if (saving) return;
    if (!items.length) {
      setError(t('pdf.noPages'));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.savePdfPages(
        node.id,
        items.map((item) => ({
          node_id: item.sourceId === node.id ? null : item.sourceId,
          page: item.page,
          rotate: item.rotate,
        })),
      );
      setEditing(false);
      setItems([]);
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('pdf.loading')}</div>;
  if (error && !pdf) return <div className="p-4 text-xs text-rose-600">{t('pdf.failed', { error })}</div>;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {editing ? (
        <div className="flex flex-wrap items-center gap-1 border-b px-2 py-1.5 text-xs">
          <button
            type="button"
            className="btn"
            disabled={selected <= 0}
            onClick={() => setSelected((value) => Math.max(0, value - 1))}
          >
            ←
          </button>
          <span>{t('pdf.page', { page: selected + 1, total: items.length })}</span>
          <button
            type="button"
            className="btn"
            disabled={selected >= items.length - 1}
            onClick={() => setSelected((value) => Math.min(items.length - 1, value + 1))}
          >
            →
          </button>
          <span className="mx-1 h-4 w-px bg-slate-200 dark:bg-zinc-700" />
          <button
            type="button"
            className="btn"
            title={t('pdf.moveUp')}
            disabled={selected <= 0}
            onClick={() => moveSelected(-1)}
          >
            ↑
          </button>
          <button
            type="button"
            className="btn"
            title={t('pdf.moveDown')}
            disabled={selected >= items.length - 1}
            onClick={() => moveSelected(1)}
          >
            ↓
          </button>
          <button
            type="button"
            className="btn"
            title={t('pdf.rotate')}
            disabled={!items.length}
            onClick={rotateSelected}
          >
            ⟳ 90°
          </button>
          <button
            type="button"
            className="btn"
            title={t('pdf.removePage')}
            disabled={!items.length}
            onClick={removeSelected}
          >
            🗑
          </button>
          <button type="button" className="btn" onClick={() => void openAdd()}>
            ＋ {t('pdf.addPdf')}
          </button>
          <span className="ml-auto flex items-center gap-1">
            <button type="button" className="btn" onClick={cancelEdit} disabled={saving}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void save()}
              disabled={saving || !items.length}
            >
              {saving ? t('common.working') : t('common.save')}
            </button>
          </span>
        </div>
      ) : (
        <div className="flex items-center gap-2 border-b px-2 py-1.5 text-xs">
          <button
            type="button"
            className="btn"
            disabled={page <= 1}
            onClick={() => setPage((value) => Math.max(1, value - 1))}
          >
            ←
          </button>
          <span>{t('pdf.page', { page, total: pages || '?' })}</span>
          <button
            type="button"
            className="btn"
            disabled={page >= pages}
            onClick={() => setPage((value) => Math.min(pages, value + 1))}
          >
            →
          </button>
          <button type="button" className="btn ml-auto" onClick={startEdit} title={t('pdf.editHint')}>
            ✎ {t('pdf.edit')}
          </button>
          <span className="text-[10px] text-slate-500">{t('pdf.renderedBy')}</span>
        </div>
      )}

      {error ? <div className="px-2 pt-1 text-[11px] text-rose-600">{error}</div> : null}

      {editing ? (
        <div className="flex min-h-0 flex-1">
          <aside className="w-48 shrink-0 overflow-auto border-r p-1 text-[11px]">
            {items.map((item, index) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setSelected(index)}
                className={`mb-0.5 flex w-full items-center gap-1 rounded px-1.5 py-1 text-left ${
                  index === selected
                    ? 'bg-indigo-600 text-white'
                    : 'hover:bg-slate-100 dark:hover:bg-zinc-800'
                }`}
              >
                <span className="w-5 shrink-0 text-right opacity-70">{index + 1}</span>
                <span
                  className="min-w-0 flex-1 truncate"
                  title={`${item.sourceName} · ${t('pdf.pageLabel', { page: item.page })}`}
                >
                  {item.sourceName}
                </span>
                <span className="shrink-0 opacity-70">
                  {item.page}
                  {item.rotate ? ` ↻${item.rotate}` : ''}
                </span>
              </button>
            ))}
            {items.length === 0 ? (
              <div className="p-2 text-slate-500">{t('pdf.noPages')}</div>
            ) : null}
          </aside>
          <div className="min-h-0 flex-1 overflow-auto bg-slate-200 p-4 dark:bg-zinc-950">
            <canvas ref={canvasRef} className="mx-auto shadow-lg" />
          </div>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto bg-slate-200 p-4 dark:bg-zinc-950">
          <canvas ref={canvasRef} className="mx-auto shadow-lg" />
        </div>
      )}

      <Modal
        open={addOpen}
        title={t('pdf.addTitle')}
        subtitle={t('pdf.addHint')}
        width="max-w-xl"
        onClose={() => setAddOpen(false)}
        footer={
          <button type="button" className="btn" onClick={() => setAddOpen(false)}>
            {t('common.close')}
          </button>
        }
      >
        {libraryLoading ? <p className="text-xs text-slate-500">{t('common.loading')}</p> : null}
        {libraryError ? <p className="text-xs text-rose-600">{libraryError}</p> : null}
        {!libraryLoading && library && library.length === 0 ? (
          <p className="text-xs text-slate-500">{t('pdf.noOtherPdfs')}</p>
        ) : null}
        {library && library.length > 0 ? (
          <ul className="space-y-1">
            {library.map((file) => (
              <li
                key={file.id}
                className="flex items-center gap-2 rounded border px-2 py-1.5 text-xs"
              >
                <span className="min-w-0 flex-1 truncate" title={file.name}>
                  📄 {file.name}
                </span>
                <button
                  type="button"
                  className="btn"
                  disabled={adding === file.id}
                  onClick={() => void appendPdf(file)}
                >
                  {adding === file.id ? t('common.working') : t('pdf.add')}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </Modal>
    </div>
  );
}
