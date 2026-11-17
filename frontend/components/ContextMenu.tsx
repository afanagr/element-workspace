'use client';

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

export interface ContextMenuItem {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
  shortcut?: string;
  onSelect: () => void;
}

export interface ContextMenuSeparator {
  key: string;
  separator: true;
}

export type ContextMenuEntry = ContextMenuItem | ContextMenuSeparator;

interface ContextMenuProps {
  open: boolean;
  x: number;
  y: number;
  items: ContextMenuEntry[];
  header?: string;
  onClose: () => void;
}

function isSeparator(entry: ContextMenuEntry): entry is ContextMenuSeparator {
  return 'separator' in entry;
}

/** A floating glass menu used for right-click actions and button popovers. */
export function ContextMenu({ open, x, y, items, header, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ x, y });
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onClose();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('pointerdown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open) return;
    const element = ref.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const margin = 8;
    setPosition({
      x: Math.max(margin, Math.min(x, window.innerWidth - rect.width - margin)),
      y: Math.max(margin, Math.min(y, window.innerHeight - rect.height - margin)),
    });
  }, [open, x, y, items]);

  if (!open || !mounted) return null;

  return createPortal(
    <div
      ref={ref}
      className="context-menu"
      style={{ left: position.x, top: position.y }}
      role="menu"
      onContextMenu={(event) => event.preventDefault()}
    >
      {header ? <div className="menu-label truncate">{header}</div> : null}
      {items.map((entry) =>
        isSeparator(entry) ? (
          <div key={entry.key} className="divider my-1" />
        ) : (
          <button
            key={entry.key}
            type="button"
            role="menuitem"
            className={`menu-item ${entry.danger ? 'text-rose-600 dark:text-rose-400' : ''}`}
            disabled={entry.disabled}
            onClick={() => {
              if (entry.disabled) return;
              entry.onSelect();
              onClose();
            }}
          >
            <span className="w-4 shrink-0 text-center text-[13px] opacity-80">{entry.icon ?? ''}</span>
            <span className="flex-1 truncate">{entry.label}</span>
            {entry.shortcut ? (
              <span className="text-[10px] text-slate-400 dark:text-zinc-500">{entry.shortcut}</span>
            ) : null}
          </button>
        ),
      )}
    </div>,
    document.body,
  );
}
