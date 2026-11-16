'use client';

import { useEffect, useMemo, useRef, useState } from 'react';

import { useI18n } from '@/lib/i18n';
import type { OfficeSession } from '@/lib/api';
import type { WorkNode } from '@/lib/types';

interface DocsEditor {
  destroyEditor?: () => void;
}

declare global {
  interface Window {
    DocsAPI?: {
      DocEditor: new (placeholderId: string, config: Record<string, unknown>) => DocsEditor;
    };
  }
}

let loaderPromise: Promise<void> | null = null;
let loaderUrl = '';

function loadDocsApi(baseUrl: string): Promise<void> {
  const normalized = baseUrl.replace(/\/$/, '');
  if (window.DocsAPI) return Promise.resolve();
  if (loaderPromise && loaderUrl === normalized) return loaderPromise;
  loaderUrl = normalized;
  loaderPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = `${normalized}/web-apps/apps/api/documents/api.js`;
    script.async = true;
    script.dataset.onlyofficeApi = '1';
    script.onload = () => resolve();
    script.onerror = () => {
      loaderPromise = null;
      reject(new Error('OnlyOffice API failed to load'));
    };
    document.head.appendChild(script);
  });
  return loaderPromise;
}

interface OfficeViewerProps {
  node: WorkNode;
  session: OfficeSession;
  onSaved?: () => void;
}

export function OfficeViewer({ node, session, onSaved }: OfficeViewerProps) {
  const { t } = useI18n();
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const savedRef = useRef<(() => void) | undefined>(onSaved);
  const editorRef = useRef<DocsEditor | null>(null);

  useEffect(() => {
    savedRef.current = onSaved;
  }, [onSaved]);

  const placeholder = useMemo(
    () => `office-${node.id.replace(/-/g, '')}-${attempt}`,
    [node.id, attempt],
  );

  useEffect(() => {
    let cancelled = false;
    setError(null);
    void (async () => {
      try {
        await loadDocsApi(session.documentServerUrl);
        if (cancelled) return;
        const docsApi = window.DocsAPI;
        if (!docsApi) throw new Error('OnlyOffice API is unavailable');
        editorRef.current?.destroyEditor?.();
        editorRef.current = new docsApi.DocEditor(placeholder, {
          ...session.config,
          events: {
            onRequestClose: () => {
              savedRef.current?.();
            },
            onError: (event: { data?: { errorDescription?: string } }) => {
              setError(event?.data?.errorDescription || t('office.failedShort'));
            },
          },
        });
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
      try {
        editorRef.current?.destroyEditor?.();
      } catch {
        /* editor may not have been created yet */
      }
      editorRef.current = null;
    };
  }, [placeholder, session, t]);

  return (
    <div className="relative h-full min-h-0">
      <div key={placeholder} id={placeholder} className="h-full w-full" />
      {error ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-[rgb(var(--surface))] p-6 text-center">
          <div className="text-xs text-rose-600 dark:text-rose-400">
            {t('office.failed', { error })}
          </div>
          <button
            type="button"
            className="btn"
            onClick={() => {
              setError(null);
              setAttempt((value) => value + 1);
            }}
          >
            {t('office.retry')}
          </button>
          <a className="btn" href={`/api/nodes/${node.id}/download`}>
            {t('common.download')}
          </a>
        </div>
      ) : null}
    </div>
  );
}
