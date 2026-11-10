'use client';

import { useEffect, useMemo, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { TextPreview, WorkNode } from '@/lib/types';

import { Markdown } from '../Markdown';

const LANGUAGE_BY_EXT: Record<string, string> = {
  '.py': 'python',
  '.js': 'javascript',
  '.mjs': 'javascript',
  '.cjs': 'javascript',
  '.jsx': 'javascript',
  '.ts': 'typescript',
  '.tsx': 'typescript',
  '.json': 'json',
  '.jsonl': 'json',
  '.html': 'xml',
  '.htm': 'xml',
  '.xml': 'xml',
  '.css': 'css',
  '.scss': 'scss',
  '.yml': 'yaml',
  '.yaml': 'yaml',
  '.toml': 'ini',
  '.ini': 'ini',
  '.sh': 'bash',
  '.bash': 'bash',
  '.ps1': 'powershell',
  '.sql': 'sql',
  '.go': 'go',
  '.rs': 'rust',
  '.rb': 'ruby',
  '.php': 'php',
  '.java': 'java',
  '.kt': 'kotlin',
  '.cs': 'csharp',
  '.c': 'c',
  '.h': 'c',
  '.cpp': 'cpp',
  '.hpp': 'cpp',
  '.vue': 'xml',
  '.svelte': 'xml',
};

const MARKDOWN_EXT = new Set(['.md', '.markdown']);

export function TextViewer({ node }: { node: WorkNode }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<TextPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [raw, setRaw] = useState(false);

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

  const formatted = useMemo(() => {
    if (!preview) return '';
    if (node.extension === '.json') {
      try {
        return JSON.stringify(JSON.parse(preview.text), null, 2);
      } catch {
        return preview.text;
      }
    }
    return preview.text;
  }, [preview, node.extension]);

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('text.loading')}</div>;
  if (error) return <div className="p-4 text-xs text-rose-600">{error}</div>;
  if (!preview) return null;

  const isMarkdown = MARKDOWN_EXT.has(node.extension);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b px-2 py-1.5 text-[11px] text-slate-500">
        <span className="chip">{node.extension || 'text'}</span>
        <span>{t('common.chars', { count: preview.text.length.toLocaleString() })}</span>
        {preview.truncated ? (
          <span className="chip text-amber-600">{t('text.truncated')}</span>
        ) : null}
        <div className="ml-auto flex gap-1">
          {isMarkdown ? (
            <button type="button" className="btn" onClick={() => setRaw((value) => !value)}>
              {raw ? t('text.rendered') : t('text.raw')}
            </button>
          ) : null}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {isMarkdown && !raw ? (
          <Markdown content={formatted} />
        ) : (
          <Markdown
            content={`\`\`\`${LANGUAGE_BY_EXT[node.extension] || ''}\n${formatted.replace(/```/g, '``\\`')}\n\`\`\``}
          />
        )}
      </div>
    </div>
  );
}
