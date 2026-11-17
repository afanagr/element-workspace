'use client';

import { useState } from 'react';

import { useI18n } from '@/lib/i18n';
import type { ToolCallInfo } from '@/lib/types';

const TOOL_ICONS: Record<string, string> = {
  list_workspace: '🗂',
  find_nodes: '🔍',
  read_file: '📖',
  write_file: '✍️',
  edit_file: '📝',
  append_file: '➕',
  create_project: '📁',
  create_folder: '📂',
  rename_node: '🏷',
  move_node: '🚚',
  delete_node: '🗑',
  excel_read: '📊',
  excel_write: '📈',
  excel_add_sheet: '➕',
  search_documents: '🔎',
  translate_file: '🌐',
  create_pdf: '📕',
  convert_to_pdf: '📄',
  pdf_split: '✂️',
};

const TOOL_ACTIONS: Record<string, string> = {
  list_workspace: 'tool.action.search',
  find_nodes: 'tool.action.search',
  search_documents: 'tool.action.search',
  read_file: 'tool.action.read',
  excel_read: 'tool.action.read',
  write_file: 'tool.action.write',
  create_pdf: 'tool.action.write',
  edit_file: 'tool.action.edit',
  append_file: 'tool.action.edit',
  translate_file: 'tool.action.edit',
  excel_write: 'tool.action.excel',
  excel_add_sheet: 'tool.action.excel',
  create_project: 'tool.action.folder',
  create_folder: 'tool.action.folder',
  rename_node: 'tool.action.rename',
  move_node: 'tool.action.move',
  delete_node: 'tool.action.delete',
  convert_to_pdf: 'tool.action.convert',
  pdf_split: 'tool.action.convert',
};

function statusTone(call: ToolCallInfo): string {
  if (call.reverted) return 'text-slate-500 dark:text-zinc-500';
  if (call.status === 'running') return 'text-amber-600 dark:text-amber-400';
  if (call.status === 'error' || call.ok === false) return 'text-rose-600 dark:text-rose-400';
  return 'text-emerald-600 dark:text-emerald-400';
}

const _LONG_ARGUMENT_KEYS = new Set(['content', 'old_text', 'new_text', 'rows', 'parts']);

function describeValue(key: string, value: unknown): string {
  if (Array.isArray(value)) return `${value.length} item(s)`;
  if (value && typeof value === 'object') return 'object';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  if (!text) return '""';
  if (_LONG_ARGUMENT_KEYS.has(key) || text.length > 60) return `${text.length} char(s)`;
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}

function argumentsPreview(arguments_: Record<string, unknown>): string {
  return Object.entries(arguments_)
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${describeValue(key, value)}`)
    .join(' · ');
}

interface VerificationInfo {
  ok: boolean;
  note: string;
}

function verificationInfo(call: ToolCallInfo): VerificationInfo | null {
  const raw = call.data?.verification;
  if (!raw || typeof raw !== 'object') return null;
  const info = raw as {
    ok?: boolean;
    problem?: string;
    sheets?: string[];
    rows?: number;
    pages?: number;
    paragraphs?: number;
    tables?: number;
    lines?: number;
  };
  if (info.ok === false) {
    return { ok: false, note: info.problem || 'verification failed' };
  }
  const parts: string[] = [];
  if (info.sheets) {
    parts.push(`${info.sheets.length} sheet(s)`, `${info.rows ?? 0} row(s)`);
  }
  if (typeof info.pages === 'number') parts.push(`${info.pages} page(s)`);
  if (typeof info.paragraphs === 'number') {
    parts.push(`${info.paragraphs} paragraph(s)`, `${info.tables ?? 0} table(s)`);
  }
  if (typeof info.lines === 'number') parts.push(`${info.lines} line(s)`);
  if (!parts.length) return null;
  return { ok: true, note: parts.join(' · ') };
}

export function ToolCallCard({
  call,
  onRevert,
}: {
  call: ToolCallInfo;
  onRevert?: (call: ToolCallInfo) => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const status = call.reverted
    ? t('tool.reverted')
    : call.status === 'running'
      ? t('tool.running')
      : call.ok === false || call.status === 'error'
        ? t('tool.failed')
        : t('tool.done');
  const verified = verificationInfo(call);
  const actionLabel = t(TOOL_ACTIONS[call.name] || 'tool.action.other');
  return (
    <div className="glass overflow-hidden rounded-xl text-[11px]">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left"
        onClick={() => setOpen((value) => !value)}
        title={call.name}
      >
        <span className="text-sm">{TOOL_ICONS[call.name] || '🛠'}</span>
        <span className="truncate font-medium">{actionLabel}</span>
        <span className={`ml-auto shrink-0 ${statusTone(call)}`}>{status}</span>
        <span className="text-[10px] text-slate-400">{open ? '▾' : '▸'}</span>
      </button>
      {open ? (
        <div className="border-t border-white/10 px-2.5 py-2">
          <div className="mb-1 font-mono text-[10px] text-slate-500 dark:text-zinc-400">
            {argumentsPreview(call.arguments) || t('tool.noArgs')}
          </div>
          {verified ? (
            <div
              className={`mb-1 text-[10px] ${
                verified.ok
                  ? 'text-emerald-600 dark:text-emerald-400'
                  : 'text-rose-600 dark:text-rose-400'
              }`}
            >
              {verified.ok
                ? `✓ ${t('tool.verified')} ${verified.note}`
                : `⚠ ${t('tool.verifyFailed')}: ${verified.note}`}
            </div>
          ) : null}
          {call.summary ? (
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] text-slate-600 dark:text-zinc-300">
              {call.summary}
            </pre>
          ) : null}
          <details className="mt-1">
            <summary className="cursor-pointer text-[10px] text-slate-500 dark:text-zinc-400">
              {t('tool.arguments')}
            </summary>
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[10px] text-slate-500 dark:text-zinc-400">
              {JSON.stringify(call.arguments, null, 2)}
            </pre>
          </details>
        </div>
      ) : null}
      {call.operation_id && !call.reverted && onRevert ? (
        <div className="flex justify-end border-t border-white/10 px-2 py-1">
          <button
            type="button"
            className="text-[10px] font-medium text-indigo-600 hover:underline dark:text-indigo-400"
            onClick={() => onRevert(call)}
          >
            {t('tool.undo')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
