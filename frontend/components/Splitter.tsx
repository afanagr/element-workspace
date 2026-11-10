'use client';

import { useCallback, useRef } from 'react';

interface SplitterProps {
  onResize: (deltaX: number) => void;
  onReset?: () => void;
  label: string;
}

/**
 * Vertical drag handle between the chat and the viewer.
 * Uses pointer capture so dragging keeps working outside the 6px strip.
 */
export function Splitter({ onResize, onReset, label }: SplitterProps) {
  const dragging = useRef(false);
  const lastX = useRef(0);

  const finish = useCallback((element: HTMLDivElement) => {
    if (!dragging.current) return;
    dragging.current = false;
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    element.classList.remove('bg-indigo-400');
  }, []);

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      title={label}
      onDoubleClick={() => onReset?.()}
      onPointerDown={(event) => {
        dragging.current = true;
        lastX.current = event.clientX;
        event.currentTarget.setPointerCapture(event.pointerId);
        event.currentTarget.classList.add('bg-indigo-400');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
      }}
      onPointerMove={(event) => {
        if (!dragging.current) return;
        const delta = event.clientX - lastX.current;
        lastX.current = event.clientX;
        if (delta !== 0) onResize(delta);
      }}
      onPointerUp={(event) => finish(event.currentTarget)}
      onPointerCancel={(event) => finish(event.currentTarget)}
      className="group relative z-10 hidden w-[6px] shrink-0 cursor-col-resize touch-none bg-[rgb(var(--border))] transition-colors hover:bg-indigo-400 lg:block"
    >
      <div className="pointer-events-none absolute inset-y-0 left-1/2 flex -translate-x-1/2 items-center">
        <div className="h-10 w-[2px] rounded bg-[rgb(var(--muted))]/40 group-hover:bg-white/70" />
      </div>
    </div>
  );
}
