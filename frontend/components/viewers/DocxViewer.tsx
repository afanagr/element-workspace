'use client';

import { useEffect, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';

export function DocxViewer({ node }: { node: WorkNode }) {
  const { t } = useI18n();
  const [html, setHtml] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setWarnings([]);
    void (async () => {
      try {
        const response = await fetch(api.contentUrl(node.id));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const buffer = await response.arrayBuffer();
        const mammoth = (await import('mammoth/mammoth.browser')).default;
        const result = await mammoth.convertToHtml({ arrayBuffer: buffer });
        if (cancelled) return;
        setHtml(result.value || `<p><em>${t('docx.empty')}</em></p>`);
        setWarnings(result.messages.slice(0, 3).map((message) => message.message));
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [node.id, node.updated_at, t]);

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('docx.loading')}</div>;
  if (error) return <div className="p-4 text-xs text-rose-600">{t('docx.failed', { error })}</div>;

  return (
    <div className="h-full overflow-auto p-6">
      <div
        className="docx-preview rounded-lg bg-white p-6 text-slate-900 shadow-sm dark:bg-zinc-100"
        dangerouslySetInnerHTML={{ __html: html }}
      />
      {warnings.length > 0 ? (
        <div className="mt-3 text-[10px] text-slate-500">
          {t('docx.notes', { notes: warnings.join(' · ') })}
        </div>
      ) : null}
    </div>
  );
}
