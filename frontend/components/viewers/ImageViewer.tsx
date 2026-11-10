'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { TextPreview, WorkNode } from '@/lib/types';
import { indexStatusKey } from '@/lib/utils';

export function ImageViewer({ node, onOpenNode }: { node: WorkNode; onOpenNode: (id: string) => void }) {
  const { t } = useI18n();
  const [ocr, setOcr] = useState<TextPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showOcr, setShowOcr] = useState(false);

  useEffect(() => {
    setOcr(null);
    setShowOcr(false);
    setError(null);
  }, [node.id]);

  const runOcr = async () => {
    setShowOcr(true);
    if (ocr || loading) return;
    setLoading(true);
    setError(null);
    try {
      setOcr(await api.text(node.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b px-2 py-1.5 text-xs">
        <button type="button" className="btn" onClick={() => void runOcr()}>
          {t('image.ocr')}
        </button>
        <span className="text-[10px] text-slate-500">
          {ocr?.meta?.width ? `${String(ocr.meta.width)}×${String(ocr.meta.height)} px` : ''}
        </span>
        <span className="ml-auto chip">
          {t('image.indexChip', {
            status: t(indexStatusKey(node.index_status), { status: node.index_status }),
            chunks: node.chunk_count,
          })}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-[repeating-conic-gradient(#e2e8f0_0%_25%,#f8fafc_0%_50%)] bg-[length:20px_20px] p-4 dark:bg-[repeating-conic-gradient(#27272a_0%_25%,#18181b_0%_50%)]">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={api.contentUrl(node.id)}
          alt={node.name}
          className="mx-auto max-h-full max-w-full object-contain shadow-lg"
        />
      </div>
      {showOcr ? (
        <div className="max-h-56 shrink-0 overflow-auto border-t p-3 text-xs">
          {loading ? <div className="text-slate-500">{t('image.running')}</div> : null}
          {error ? <div className="text-rose-600">{error}</div> : null}
          {ocr ? (
            <>
              <div className="mb-1 flex items-center gap-2">
                <span className="font-semibold">{t('image.title')}</span>
                <span className="chip">{t('common.chars', { count: ocr.text.length })}</span>
                <button
                  type="button"
                  className="btn btn-ghost ml-auto"
                  onClick={() => setShowOcr(false)}
                >
                  {t('common.hide')}
                </button>
              </div>
              <pre className="whitespace-pre-wrap break-words text-[11px] text-slate-600 dark:text-zinc-300">
                {ocr.text || t('image.none')}
              </pre>
            </>
          ) : null}
        </div>
      ) : null}
      <div className="border-t px-2 py-1 text-[10px] text-slate-500">{t('image.hint')}</div>
      <button type="button" className="hidden" onClick={() => onOpenNode(node.id)} />
    </div>
  );
}
