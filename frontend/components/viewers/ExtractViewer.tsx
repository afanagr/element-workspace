'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { TextPreview, WorkNode } from '@/lib/types';

/** Fallback viewer for legacy .doc, scanned PDFs and unsupported binaries. */
export function ExtractViewer({ node }: { node: WorkNode }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<TextPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPreview(null);
    void (async () => {
      try {
        const result = await api.text(node.id);
        if (!cancelled) setPreview(result);
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

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('extract.loading')}</div>;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b px-3 py-2 text-[11px] text-slate-500">
        {error
          ? t('extract.unavailable')
          : t('extract.title', { count: preview?.segments.length ?? 0 })}
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {error ? <div className="text-xs text-rose-600">{error}</div> : null}
        {preview ? (
          preview.text ? (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed">
              {preview.text}
            </pre>
          ) : (
            <div className="text-xs text-slate-500">{t('extract.none')}</div>
          )
        ) : null}
      </div>
    </div>
  );
}
