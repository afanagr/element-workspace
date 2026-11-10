'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import * as XLSX from 'xlsx';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';

export function ExcelViewer({ node }: { node: WorkNode }) {
  const { t } = useI18n();
  const [sheets, setSheets] = useState<Record<string, string[][]>>({});
  const [active, setActive] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const cacheRef = useRef<Record<string, string[][]>>({});

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSheets({});
    setActive('');
    void (async () => {
      try {
        const response = await fetch(api.contentUrl(node.id));
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const buffer = await response.arrayBuffer();
        const workbook = XLSX.read(buffer, { type: 'array' });
        const parsed: Record<string, string[][]> = {};
        workbook.SheetNames.forEach((name) => {
          const sheet = workbook.Sheets[name];
          const rows = XLSX.utils.sheet_to_json<string[]>(sheet, {
            header: 1,
            raw: false,
            defval: '',
          });
          parsed[name] = rows.slice(0, 300).map((row) => row.slice(0, 60).map((cell) => String(cell ?? '')));
        });
        if (cancelled) return;
        cacheRef.current = parsed;
        setSheets(parsed);
        setActive(workbook.SheetNames[0] ?? '');
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

  const rows = useMemo(() => sheets[active] ?? [], [sheets, active]);

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('excel.loading')}</div>;
  if (error)
    return (
      <div className="p-4 text-xs text-rose-600">{t('excel.failed', { error })}</div>
    );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap gap-1 border-b px-2 py-1.5">
        {Object.keys(sheets).map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => setActive(name)}
            className={`rounded px-2 py-1 text-xs ${
              name === active
                ? 'bg-indigo-600 text-white'
                : 'bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-zinc-800 dark:text-zinc-300'
            }`}
          >
            {name}
          </button>
        ))}
        <span className="ml-auto self-center text-[10px] text-slate-500">
          {t('excel.rows', { count: rows.length })}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full border-collapse text-xs">
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className={rowIndex === 0 ? 'font-semibold' : ''}>
                <td className="sticky left-0 min-w-[32px] border border-slate-200 bg-slate-50 px-1 text-center text-[10px] text-slate-400 dark:border-zinc-700 dark:bg-zinc-900">
                  {rowIndex + 1}
                </td>
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    className="max-w-[280px] truncate border border-slate-200 px-1.5 py-1 dark:border-zinc-700"
                    title={cell}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 ? (
          <div className="p-4 text-xs text-slate-500">{t('excel.empty')}</div>
        ) : null}
      </div>
    </div>
  );
}
