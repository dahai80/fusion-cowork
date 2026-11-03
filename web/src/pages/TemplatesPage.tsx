import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type TemplateInfo, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W5: template catalog (builtin + custom). "Open in canvas" fetches the
// template's workflow body and saves a copy as a user workflow, then jumps
// to the canvas. Data: GET /api/templates (new in this change).

export function TemplatesPage() {
    const t = useT();
    const [category, setCategory] = useState('');
    const [creating, setCreating] = useState('');
    const retry = useAutoRetry(() => api.templates(category), [category]);

    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const templates = retry.data?.templates ?? [];
    const categories = Array.from(new Set(templates.map((x) => x.category ?? ''))).filter(Boolean);
    const [importing, setImporting] = useState(false);

    // issue #261: 导入模板 JSON 文件 → POST /api/templates/import → 刷新列表
    async function onImportFile(file: File) {
        if (importing) return;
        setImporting(true);
        try {
            const body = JSON.parse(await file.text());
            const res = await api.importTemplate(body);
            sdk.toast.show({
                level: 'success',
                message: t(`已导入模板: ${res.name}`, `Template imported: ${res.name}`),
            });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] template import failed', e);
            sdk.toast.show({
                level: 'error',
                message: t(
                    '导入失败 — 须为含 workflow.nodes 的 JSON',
                    'Import failed — JSON with workflow.nodes required'
                ),
            });
        } finally {
            setImporting(false);
        }
    }

    // issue #261: 导出模板 JSON (浏览器下载)
    async function exportTemplate(tpl: TemplateInfo) {
        try {
            const url = await api.templateExport(tpl.id);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${tpl.id}.json`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            console.error('[cowork-web] template export failed', e);
            sdk.toast.show({ level: 'error', message: t('导出失败', 'Export failed') });
        }
    }

    async function removeTemplate(tpl: TemplateInfo) {
        try {
            await api.deleteTemplate(tpl.id);
            sdk.toast.show({ level: 'success', message: t('已删除', 'Deleted') });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] template delete failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('删除失败 — 仅自定义模板可删除', 'Delete failed — custom templates only'),
            });
        }
    }

    function openInCanvas(tpl: TemplateInfo) {
        if (creating) return;
        setCreating(tpl.id);
        api.template(tpl.id)
            .then((detail) => {
                const body = (detail as unknown as { workflow?: unknown }).workflow ?? { nodes: [], edges: [] };
                return api.createWorkflow({ name: tpl.name, description: tpl.description ?? '', workflow: body });
            })
            .then((created) => {
                sdk.toast.show({
                    level: 'success',
                    message: t('已创建副本，请在画布中完善', 'Copy created — refine in canvas'),
                });
                window.location.hash = `#/canvas/${created.id}`;
            })
            .catch((e: Error) => {
                console.error('[cowork-web] template copy failed', e);
                sdk.toast.show({ level: 'error', message: t('创建副本失败', 'Failed to create copy') });
            })
            .finally(() => setCreating(''));
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center gap-2 pb-3">
                <select
                    className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                >
                    <option value="">{t('全部分类', 'All categories')}</option>
                    {categories.map((c) => (
                        <option key={c} value={c}>
                            {c}
                        </option>
                    ))}
                </select>
                <div className="text-xs text-text-muted">{templates.length}</div>
                <label className="ml-auto cursor-pointer rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted">
                    {importing ? t('导入中…', 'Importing…') : t('导入模板', 'Import JSON')}
                    <input
                        type="file"
                        accept=".json,application/json"
                        className="hidden"
                        disabled={importing}
                        onChange={(e) => {
                            const f = e.target.files?.[0];
                            if (f) void onImportFile(f);
                            e.target.value = '';
                        }}
                    />
                </label>
            </div>
            {templates.length === 0 ? (
                <EmptyState title={t('该分类暂无模板', 'No templates in this category')} />
            ) : (
                <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                    {templates.map((tpl) => (
                        <div key={tpl.id} className="flex flex-col rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="flex items-center gap-2">
                                <span className="text-sm font-medium text-text-primary">{tpl.name}</span>
                                {tpl.category ? (
                                    <span className="ml-auto rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                        {tpl.category}
                                    </span>
                                ) : null}
                            </div>
                            {tpl.description ? (
                                <div className="mt-1 text-xs text-text-secondary">{tpl.description}</div>
                            ) : null}
                            {tpl.tags && tpl.tags.length > 0 ? (
                                <div className="mt-1 flex flex-wrap gap-1">
                                    {tpl.tags.map((tag) => (
                                        <span
                                            key={tag}
                                            className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-muted"
                                        >
                                            #{tag}
                                        </span>
                                    ))}
                                </div>
                            ) : null}
                            <div className="mt-auto flex justify-end gap-1 pt-2">
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => void exportTemplate(tpl)}
                                >
                                    {t('导出', 'Export')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-error hover:bg-error/10"
                                    onClick={() => void removeTemplate(tpl)}
                                >
                                    {t('删除', 'Delete')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                    disabled={creating === tpl.id}
                                    onClick={() => openInCanvas(tpl)}
                                >
                                    {creating === tpl.id
                                        ? t('创建中…', 'Creating…')
                                        : t('用画布打开', 'Open in canvas')}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
