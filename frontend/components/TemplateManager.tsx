'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from '@/lib/api';
import { useI18n } from '@/lib/i18n';
import type { DocumentTemplate, TemplateField, WorkNode } from '@/lib/types';

import { ConfirmModal, Modal, PromptModal } from './Modal';

interface TemplateManagerProps {
  mode: 'standard' | 'user';
}

function fieldLabel(field: TemplateField): string {
  return field.label || field.name;
}

export function TemplateManager({ mode }: TemplateManagerProps) {
  const { t } = useI18n();
  const [templates, setTemplates] = useState<DocumentTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<DocumentTemplate | null>(null);
  const [renameTarget, setRenameTarget] = useState<DocumentTemplate | null>(null);
  const [details, setDetails] = useState<DocumentTemplate | null>(null);
  const [markingId, setMarkingId] = useState<string | null>(null);
  const [fillTarget, setFillTarget] = useState<DocumentTemplate | null>(null);
  const [fillProjects, setFillProjects] = useState<WorkNode[]>([]);
  const [fillProject, setFillProject] = useState('');
  const [fillName, setFillName] = useState('');
  const [fillValues, setFillValues] = useState<Record<string, string>>({});
  const [fillError, setFillError] = useState<string | null>(null);
  const [filling, setFilling] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setTemplates(await api.templates(mode === 'standard' ? 'standard' : 'available'));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [mode]);

  useEffect(() => {
    void load();
  }, [load]);

  const closeUpload = () => {
    if (busy) return;
    setUploadOpen(false);
    setFile(null);
    setName('');
    setDescription('');
    if (fileRef.current) fileRef.current.value = '';
  };

  const upload = async () => {
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await api.createTemplate(file, {
        name: name.trim() || undefined,
        description: description.trim() || undefined,
        scope: mode === 'standard' ? 'standard' : 'user',
      });
      setNotice(
        created.fields.length === 0
          ? t('templates.uploadedNoFields', { name: created.name })
          : t('templates.uploaded', { name: created.name }),
      );
      closeUpload();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const openFill = async (template: DocumentTemplate) => {
    setFillTarget(template);
    setFillName(`${template.name}${template.extension}`);
    setFillValues(Object.fromEntries(template.fields.map((field) => [field.name, ''])));
    setFillError(null);
    setError(null);
    try {
      const tree = await api.tree();
      const projects = tree.filter((node) => node.kind === 'project');
      setFillProjects(projects);
      setFillProject(projects[0]?.id ?? '');
    } catch (err) {
      setFillError(err instanceof Error ? err.message : String(err));
    }
  };

  const fill = async () => {
    if (!fillTarget || !fillProject || filling) return;
    setFilling(true);
    setFillError(null);
    try {
      const data: Record<string, unknown> = {};
      for (const field of fillTarget.fields) {
        const raw = (fillValues[field.name] ?? '').trim();
        if (!raw) {
          if (field.required) {
            throw new Error(t('templates.requiredField', { field: field.label || field.name }));
          }
          continue;
        }
        if (field.type === 'array' || field.type === 'object') {
          try {
            data[field.name] = JSON.parse(raw);
          } catch {
            throw new Error(t('templates.invalidJson', { field: field.label || field.name }));
          }
        } else {
          data[field.name] = raw;
        }
      }
      let name = fillName.trim() || `${fillTarget.name}${fillTarget.extension}`;
      if (!name.toLowerCase().endsWith(fillTarget.extension)) {
        name += fillTarget.extension;
      }
      const created = await api.fillTemplate(fillTarget.id, {
        parent_id: fillProject,
        name,
        data,
      });
      setNotice(t('templates.fillDone', { name: created.name }));
      setFillTarget(null);
      window.dispatchEvent(new CustomEvent('afw-workspace-changed'));
    } catch (err) {
      setFillError(err instanceof Error ? err.message : String(err));
    } finally {
      setFilling(false);
    }
  };

  const remove = async () => {
    if (!pendingDelete || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteTemplate(pendingDelete.id);
      setNotice(t('templates.deleted', { name: pendingDelete.name }));
      setPendingDelete(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const rename = async (value: string) => {
    if (!renameTarget || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await api.updateTemplate(renameTarget.id, { name: value });
      setNotice(t('templates.renamed', { name: updated.name }));
      setRenameTarget(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const markup = async (template: DocumentTemplate) => {
    if (markingId) return;
    setMarkingId(template.id);
    setError(null);
    setNotice(null);
    try {
      const updated = await api.markupTemplate(template.id);
      setNotice(
        t('templates.aiMarkupDone', {
          count: updated.fields.length,
          skipped: updated.markup_skipped ?? 0,
        }),
      );
      setDetails(updated);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMarkingId(null);
    }
  };

  const standard = templates.filter((item) => item.scope === 'standard');
  const mine = templates.filter((item) => item.scope === 'user');
  const primary = mode === 'standard' ? standard : mine;

  const renderCard = (template: DocumentTemplate, readOnly: boolean) => (
    <article key={template.id} className="panel flex flex-col gap-2 rounded-xl p-3">
      <div className="flex items-start gap-2">
        <span className="text-xl">{template.kind === 'docx' ? '📝' : '📊'}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <h3 className="truncate text-sm font-semibold" title={template.name}>
              {template.name}
            </h3>
            <span className="chip uppercase">{template.kind}</span>
            {template.scope === 'standard' ? (
              <span className="chip text-indigo-600 dark:text-indigo-300">
                {t('templates.standardBadge')}
              </span>
            ) : null}
          </div>
          <p className="mt-0.5 line-clamp-2 text-[11px] text-slate-500 dark:text-zinc-400">
            {template.description || t('templates.noDescription')}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-1">
        {template.fields.length === 0 ? (
          <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-amber-700 dark:text-amber-300">
            <span>{t('templates.noFields')}</span>
            {!readOnly ? (
              <button
                type="button"
                className="btn px-2 py-0.5 text-[10px]"
                disabled={markingId === template.id}
                onClick={() => void markup(template)}
              >
                ✨ {markingId === template.id ? t('templates.aiMarkupBusy') : t('templates.aiMarkup')}
              </button>
            ) : null}
          </div>
        ) : (
          template.fields.slice(0, 5).map((field) => (
            <span key={field.name} className="chip text-[10px]" title={field.name}>
              {fieldLabel(field)}
              {field.type === 'array' ? '[]' : ''}
            </span>
          ))
        )}
        {template.fields.length > 5 ? (
          <span className="chip text-[10px]">+{template.fields.length - 5}</span>
        ) : null}
      </div>

      <div className="mt-auto flex flex-wrap items-center gap-1 pt-1">
        <button
          type="button"
          className="btn px-2 py-1 text-[11px]"
          onClick={() => setDetails(template)}
        >
          {t('templates.fields')}
        </button>
        {template.fields.length > 0 ? (
          <button
            type="button"
            className="btn btn-primary px-2 py-1 text-[11px]"
            disabled={filling}
            onClick={() => void openFill(template)}
          >
            {t('templates.createDocument')}
          </button>
        ) : null}
        <a
          className="btn px-2 py-1 text-[11px]"
          href={api.templateDownloadUrl(template.id)}
        >
          {t('common.download')}
        </a>
        {!readOnly ? (
          <>
            <button
              type="button"
              className="btn px-2 py-1 text-[11px]"
              onClick={() => setRenameTarget(template)}
            >
              {t('common.rename')}
            </button>
            <button
              type="button"
              className="btn px-2 py-1 text-[11px] text-rose-600 dark:text-rose-400"
              onClick={() => setPendingDelete(template)}
            >
              {t('common.delete')}
            </button>
          </>
        ) : (
          <span className="ml-auto text-[10px] text-slate-400 dark:text-zinc-500">
            {t('templates.readOnly')}
          </span>
        )}
      </div>
    </article>
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">
            {mode === 'standard' ? t('templates.standardTitle') : t('templates.myTitle')}
          </h2>
          <p className="mt-0.5 text-[11px] text-slate-500 dark:text-zinc-400">
            {mode === 'standard' ? t('templates.standardHint') : t('templates.myHint')}
          </p>
        </div>
        <button type="button" className="btn btn-primary" onClick={() => setUploadOpen(true)}>
          {t('templates.upload')}
        </button>
      </div>

      <div className="rounded-xl border border-indigo-300/30 bg-indigo-50/40 px-3 py-2 text-[11px] text-indigo-800 dark:border-indigo-800/30 dark:bg-indigo-950/20 dark:text-indigo-200">
        {t('templates.pdfNote')}
      </div>

      {error ? (
        <div className="rounded-lg bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
          {error}
        </div>
      ) : null}
      {notice ? (
        <div className="rounded-lg bg-emerald-50 px-3 py-2 text-xs text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
          {notice}
        </div>
      ) : null}

      {loading ? (
        <p className="text-xs text-slate-500">{t('common.loading')}</p>
      ) : primary.length === 0 ? (
        <div className="rounded-xl border border-dashed p-5 text-center text-[11px] text-slate-500 dark:text-zinc-400">
          <div className="mb-2 text-2xl opacity-60">📋</div>
          <p>{mode === 'standard' ? t('templates.emptyStandard') : t('templates.emptyMine')}</p>
        </div>
      ) : (
        <div className="grid gap-2 md:grid-cols-2">
          {primary.map((template) => renderCard(template, false))}
        </div>
      )}

      {mode === 'user' && standard.length > 0 ? (
        <section className="space-y-2 border-t border-white/20 pt-3">
          <div>
            <h3 className="text-sm font-semibold">{t('templates.standardTitle')}</h3>
            <p className="mt-0.5 text-[11px] text-slate-500 dark:text-zinc-400">
              {t('templates.standardReadOnlyHint')}
            </p>
          </div>
          <div className="grid gap-2 md:grid-cols-2">
            {standard.map((template) => renderCard(template, true))}
          </div>
        </section>
      ) : null}

      <Modal
        open={uploadOpen}
        title={mode === 'standard' ? t('templates.uploadStandardTitle') : t('templates.uploadTitle')}
        subtitle={t('templates.uploadHint')}
        onClose={closeUpload}
        footer={
          <>
            <button type="button" className="btn" onClick={closeUpload} disabled={busy}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || !file}
              onClick={() => void upload()}
            >
              {busy ? t('common.working') : t('templates.upload')}
            </button>
          </>
        }
      >
        <div className="space-y-3">
          <label className="block text-xs">
            <span className="mb-1 block font-medium">{t('templates.file')}</span>
            <input
              ref={fileRef}
              type="file"
              accept=".docx,.xlsx,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              className="input text-xs"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </label>
          <label className="block text-xs">
            <span className="mb-1 block font-medium">{t('templates.name')}</span>
            <input
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t('templates.namePlaceholder')}
            />
          </label>
          <label className="block text-xs">
            <span className="mb-1 block font-medium">{t('templates.description')}</span>
            <textarea
              className="input min-h-20 resize-y"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder={t('templates.descriptionPlaceholder')}
            />
          </label>
          <p className="text-[11px] text-slate-500 dark:text-zinc-400">{t('templates.aiMarkupHint')}</p>
          {error ? (
            <p className="rounded-lg bg-rose-50 px-2 py-1.5 text-[11px] text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
              {error}
            </p>
          ) : null}
        </div>
      </Modal>

      <Modal
        open={fillTarget !== null}
        title={t('templates.fillTitle', { name: fillTarget?.name ?? '' })}
        subtitle={t('templates.fillHint')}
        width="max-w-2xl"
        onClose={() => {
          if (!filling) setFillTarget(null);
        }}
        footer={
          <>
            <button type="button" className="btn" disabled={filling} onClick={() => setFillTarget(null)}>
              {t('common.cancel')}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              disabled={filling || !fillProject}
              onClick={() => void fill()}
            >
              {filling ? t('common.working') : t('templates.createDocument')}
            </button>
          </>
        }
      >
        {fillTarget ? (
          <div className="space-y-3">
            <div className="grid gap-2 sm:grid-cols-2">
              <label className="block text-xs">
                <span className="mb-1 block font-medium">{t('templates.project')}</span>
                <select
                  className="input"
                  value={fillProject}
                  onChange={(event) => setFillProject(event.target.value)}
                >
                  {fillProjects.length === 0 ? <option value="">{t('templates.noProjects')}</option> : null}
                  {fillProjects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="block text-xs">
                <span className="mb-1 block font-medium">{t('templates.outputName')}</span>
                <input
                  className="input"
                  value={fillName}
                  onChange={(event) => setFillName(event.target.value)}
                />
              </label>
            </div>
            {fillTarget.fields.map((field) => (
              <label key={field.name} className="block text-xs">
                <span className="mb-1 block font-medium">
                  {field.label || field.name}
                  {field.required ? ' *' : ''}
                </span>
                {field.type === 'array' || field.type === 'object' ? (
                  <textarea
                    className="input min-h-20 font-mono text-[11px]"
                    value={fillValues[field.name] ?? ''}
                    placeholder={field.type === 'array' ? '[{"name":"..."}]' : '{"key":"value"}'}
                    onChange={(event) =>
                      setFillValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                    }
                  />
                ) : (
                  <input
                    className="input"
                    value={fillValues[field.name] ?? ''}
                    onChange={(event) =>
                      setFillValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                    }
                  />
                )}
                {field.type === 'array' ? (
                  <span className="mt-1 block text-[10px] text-slate-400">
                    {t('templates.arrayHint')}
                  </span>
                ) : null}
              </label>
            ))}
            {fillError ? (
              <p className="rounded-lg bg-rose-50 px-2 py-1.5 text-[11px] text-rose-700 dark:bg-rose-950/40 dark:text-rose-300">
                {fillError}
              </p>
            ) : null}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={details !== null}
        title={details?.name ?? t('templates.fields')}
        subtitle={details ? `${details.kind.toUpperCase()} · ${t('templates.fields')}` : undefined}
        onClose={() => setDetails(null)}
      >
        {details ? (
          <div className="space-y-3">
            <p className="text-xs text-slate-500 dark:text-zinc-400">
              {details.description || t('templates.noDescription')}
            </p>
            {details.fields.length === 0 ? (
              <p className="text-xs text-amber-600 dark:text-amber-400">{t('templates.noFields')}</p>
            ) : (
              <div className="grid gap-1 sm:grid-cols-2">
                {details.fields.map((field) => (
                  <div key={field.name} className="rounded-lg border border-white/15 p-2 text-[11px]">
                    <div className="font-mono font-medium">{field.name}</div>
                    <div className="text-slate-500 dark:text-zinc-400">
                      {field.type}{field.required ? ' · required' : ''}
                    </div>
                  </div>
                ))}
              </div>
            )}
            <p className="text-[11px] text-slate-500 dark:text-zinc-400">{t('templates.chatHint')}</p>
          </div>
        ) : null}
      </Modal>

      <PromptModal
        open={renameTarget !== null}
        title={t('templates.renameTitle')}
        initialValue={renameTarget?.name ?? ''}
        confirmLabel={t('common.rename')}
        busy={busy}
        onClose={() => setRenameTarget(null)}
        onConfirm={(value) => void rename(value)}
      />

      <ConfirmModal
        open={pendingDelete !== null}
        title={t('templates.deleteTitle')}
        message={t('templates.confirmDelete', { name: pendingDelete?.name ?? '' })}
        confirmLabel={t('common.delete')}
        danger
        busy={busy}
        onClose={() => setPendingDelete(null)}
        onConfirm={() => void remove()}
      />
    </div>
  );
}
