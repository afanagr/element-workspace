'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import * as XLSX from 'xlsx';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { WorkNode } from '@/lib/types';

interface CellChange {
  sheet: string;
  cell: string;
  value: string;
}

function cloneSheets(source: Record<string, string[][]>): Record<string, string[][]> {
  const copy: Record<string, string[][]> = {};
  for (const [name, rows] of Object.entries(source)) {
    copy[name] = rows.map((row) => [...row]);
  }
  return copy;
}

export function ExcelViewer({ node, onSaved }: { node: WorkNode; onSaved?: () => void }) {
  const { t } = useI18n();
  const [sheets, setSheets] = useState<Record<string, string[][]>>({});
  const [draft, setDraft] = useState<Record<string, string[][]>>({});
  const [active, setActive] = useState<string>('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [changeCount, setChangeCount] = useState(0);
  const changesRef = useRef<Map<string, CellChange>>(new Map());

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSheets({});
    setDraft({});
    setActive('');
    setEditing(false);
    changesRef.current.clear();
    setChangeCount(0);
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
        setSheets(parsed);
        setDraft(cloneSheets(parsed));
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

  const rows = useMemo(
    () => (editing ? draft[active] : sheets[active]) ?? [],
    [editing, draft, sheets, active],
  );
  const editable = node.extension === '.xlsx' || node.extension === '.xlsm';

  const setCell = (rowIndex: number, cellIndex: number, value: string) => {
    setDraft((prev) => {
      const sheetRows = (prev[active] ?? []).map((row) => [...row]);
      while (sheetRows.length <= rowIndex) sheetRows.push([]);
      const row = [...sheetRows[rowIndex]];
      while (row.length <= cellIndex) row.push('');
      row[cellIndex] = value;
      sheetRows[rowIndex] = row;
      return { ...prev, [active]: sheetRows };
    });
    const cell = XLSX.utils.encode_cell({ r: rowIndex, c: cellIndex });
    changesRef.current.set(`${active}!${cell}`, { sheet: active, cell, value });
    setChangeCount(changesRef.current.size);
  };

  const startEdit = () => {
    setDraft(cloneSheets(sheets));
    changesRef.current.clear();
    setChangeCount(0);
    setEditing(true);
    setError(null);
  };

  const cancelEdit = () => {
    if (changeCount && !window.confirm(t('excel.discardConfirm'))) return;
    setEditing(false);
    changesRef.current.clear();
    setChangeCount(0);
  };

  const save = async () => {
    if (saving) return;
    if (!changeCount) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.saveSheetCells(node.id, Array.from(changesRef.current.values()));
      changesRef.current.clear();
      setChangeCount(0);
      setEditing(false);
      onSaved?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <div className="p-4 text-xs text-slate-500">{t('excel.loading')}</div>;
  if (error && !editing)
    return (
      <div className="p-4 text-xs text-rose-600">{t('excel.failed', { error })}</div>
    );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-1 border-b px-2 py-1.5">
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
          {editing && changeCount ? ` · ${t('excel.changes', { count: changeCount })}` : ''}
        </span>
        {editing ? (
          <>
            <button type="button" className="btn" onClick={cancelEdit} disabled={saving}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void save()}
              disabled={saving || !changeCount}
            >
              {saving ? t('common.working') : t('common.save')}
            </button>
          </>
        ) : editable ? (
          <button type="button" className="btn" onClick={startEdit} title={t('excel.editHint')}>
            ✎ {t('excel.edit')}
          </button>
        ) : null}
      </div>
      {error ? <div className="px-2 pt-1 text-[11px] text-rose-600">{error}</div> : null}
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
                    className="max-w-[280px] border border-slate-200 px-1.5 py-1 dark:border-zinc-700"
                  >
                    {editing ? (
                      <input
                        className="w-full min-w-[64px] bg-transparent text-xs outline-none focus:ring-1 focus:ring-indigo-400"
                        value={cell}
                        onChange={(event) => setCell(rowIndex, cellIndex, event.target.value)}
                      />
                    ) : (
                      <span className="block max-w-[280px] truncate" title={cell}>
                        {cell}
                      </span>
                    )}
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
