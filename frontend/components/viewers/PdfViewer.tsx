'use client';

import { useEffect, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';

interface PdfDocument {
  numPages: number;
  getPage: (page: number) => Promise<any>;
}

export function PdfViewer({ node }: { node: WorkNode }) {
  const { t } = useI18n();
  const [pdf, setPdf] = useState<PdfDocument | null>(null);
  const [page, setPage] = useState(1);
  const [pages, setPages] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const renderTaskRef = useRef<any>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPdf(null);
    setPage(1);
    void (async () => {
      try {
        const pdfjs = await import('pdfjs-dist');
        pdfjs.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.mjs';
        const response = await fetch(api.contentUrl(node.id));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = new Uint8Array(await response.arrayBuffer());
        const document = await pdfjs.getDocument({ data }).promise;
        if (cancelled) return;
        setPdf(document as unknown as PdfDocument);
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

  useEffect(() => {
    if (!pdf) return;
    let cancelled = false;
    void (async () => {
      try {
        const pdfPage = await pdf.getPage(page);
        if (cancelled) return;
        const viewport = pdfPage.getViewport({ scale: 1.4 });
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
  }, [pdf, page]);

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('pdf.loading')}</div>;
  if (error) return <div className="p-4 text-xs text-rose-600">{t('pdf.failed', { error })}</div>;

  return (
    <div className="flex h-full min-h-0 flex-col">
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
        <span className="ml-auto text-[10px] text-slate-500">{t('pdf.renderedBy')}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-slate-200 p-4 dark:bg-zinc-950">
        <canvas ref={canvasRef} className="mx-auto shadow-lg" />
      </div>
    </div>
  );
}
