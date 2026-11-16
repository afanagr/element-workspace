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

export function TextViewer({ node, onSaved }: { node: WorkNode; onSaved?: () => void }) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<TextPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [raw, setRaw] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setPreview(null);
    setEditing(false);
    setDraft('');
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
  const dirty = editing && preview ? draft !== preview.text : false;

  const startEdit = () => {
    if (!preview) return;
    setDraft(preview.text);
    setRaw(true);
    setEditing(true);
    setError(null);
  };

  const cancelEdit = () => {
    if (dirty && !window.confirm(t('text.discardConfirm'))) return;
    setEditing(false);
    setDraft('');
  };

  const save = async () => {
    if (!preview || saving) return;
    setSaving(true);
    setError(null);
    try {
      await api.saveText(node.id, draft);
      setEditing(false);
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b px-2 py-1.5 text-[11px] text-slate-500">
        <span className="chip">{node.extension || 'text'}</span>
        <span>{t('common.chars', { count: preview.text.length.toLocaleString() })}</span>
        {preview.truncated ? (
          <span className="chip text-amber-600">{t('text.truncated')}</span>
        ) : null}
        {dirty ? <span className="chip text-amber-600">{t('text.unsaved')}</span> : null}
        <div className="ml-auto flex gap-1">
          {editing ? (
            <>
              <button type="button" className="btn" onClick={cancelEdit} disabled={saving}>
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => void save()}
                disabled={saving || !dirty}
                title="Ctrl/⌘+S"
              >
                {saving ? t('common.working') : t('common.save')}
              </button>
            </>
          ) : (
            <>
              {isMarkdown ? (
                <button type="button" className="btn" onClick={() => setRaw((value) => !value)}>
                  {raw ? t('text.rendered') : t('text.raw')}
                </button>
              ) : null}
              <button
                type="button"
                className="btn"
                onClick={startEdit}
                disabled={preview.truncated}
                title={preview.truncated ? t('text.editTooLarge') : t('text.editHint')}
              >
                ✎ {t('text.edit')}
              </button>
            </>
          )}
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4">
        {error ? <div className="mb-2 text-xs text-rose-600">{error}</div> : null}
        {editing ? (
          <textarea
            className="h-full min-h-[320px] w-full resize-none rounded-md border border-slate-200 bg-transparent p-2 font-mono text-xs outline-none focus:border-indigo-400 dark:border-zinc-700"
            value={draft}
            spellCheck={false}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
                event.preventDefault();
                void save();
              }
              if (event.key === 'Escape') cancelEdit();
            }}
          />
        ) : isMarkdown && !raw ? (
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
