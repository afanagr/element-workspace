'use client';

import { useEffect, useRef, useState } from 'react';

import { useI18n } from '@/lib/i18n';

interface ModalProps {
  title: string;
  open: boolean;
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  busy?: boolean;
  onConfirm: (value: string) => void | Promise<void>;
  onClose: () => void;
}

export function PromptModal({
  title,
  open,
  initialValue = '',
  placeholder,
  confirmLabel,
  busy = false,
  onConfirm,
  onClose,
}: ModalProps) {
  const { t } = useI18n();
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setValue(initialValue);
      setTimeout(() => inputRef.current?.focus(), 30);
    }
  }, [open, initialValue]);

  if (!open) return null;

  const submit = async () => {
    const trimmed = value.trim();
    if (!trimmed) return;
    await onConfirm(trimmed);
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="panel w-full max-w-sm rounded-xl p-4 shadow-2xl">
        <div className="mb-3 text-sm font-semibold">{title}</div>
        <input
          ref={inputRef}
          className="input"
          value={value}
          placeholder={placeholder}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void submit();
            if (event.key === 'Escape') onClose();
          }}
        />
        <div className="mt-4 flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => void submit()}
            disabled={busy || !value.trim()}
          >
            {busy ? t('common.working') : confirmLabel ?? t('common.save')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface ConfirmModalProps {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  open: boolean;
  busy?: boolean;
  onConfirm: () => void | Promise<void>;
  onClose: () => void;
}

export function ConfirmModal({
  title,
  message,
  confirmLabel,
  danger,
  open,
  busy,
  onConfirm,
  onClose,
}: ConfirmModalProps) {
  const { t } = useI18n();
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="panel w-full max-w-sm rounded-xl p-4 shadow-2xl">
        <div className="mb-2 text-sm font-semibold">{title}</div>
        <p className="mb-4 text-xs text-slate-600 dark:text-zinc-400">{message}</p>
        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className={`btn ${danger ? 'border-rose-500 bg-rose-600 text-white hover:bg-rose-700' : 'btn-primary'}`}
            onClick={() => void onConfirm()}
            disabled={busy}
          >
            {busy ? t('common.working') : confirmLabel ?? t('common.save')}
          </button>
        </div>
      </div>
    </div>
  );
}
