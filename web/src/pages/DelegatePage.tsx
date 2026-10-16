import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useState } from 'react';
import { type CanvasEdgeDef, type CanvasNodeDef, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W7: NL delegation entry — prompt → NLWorkflowGenerator (/api/ai/generate-workflow)
// → preview → save as workflow (+ optional run). Real generation needs fusion-mlx
// live; errors surface as 502 toasts.

const CATEGORY_COLOR: Record<string, string> = {
    tool: '#3b82f6',
    macos_system: '#8b5cf6',
    file_operation: '#10b981',
    ai_processing: '#f59e0b',
    io: '#06b6d4',
    logic: '#ef4444',
    fusion_ecosystem: '#ec4899',
    browser: '#6366f1',
};

interface GeneratedWorkflow {
    name?: string;
    description?: string;
    nodes: CanvasNodeDef[];
    edges: CanvasEdgeDef[];
}

export function DelegatePage() {
    const t = useT();
    const [prompt, setPrompt] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [wf, setWf] = useState<GeneratedWorkflow | null>(null);
    const [saving, setSaving] = useState(false);

    async function generate() {
        if (busy || !prompt.trim()) return;
        setBusy(true);
        setError('');
        setWf(null);
        try {
            const res = await api.generateWorkflow({ prompt: prompt.trim() });
            setWf(res.workflow);
        } catch (e) {
            console.error('[cowork-web] nl generate failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function save(alsoRun: boolean) {
        if (!wf || saving) return;
        setSaving(true);
        try {
            const created = await api.createWorkflow({
                name: wf.name || t('未命名委托', 'Untitled delegation'),
                description: wf.description || '',
                workflow: { name: wf.name || '', nodes: wf.nodes, edges: wf.edges },
            });
            if (alsoRun) {
                await api.startRun({ workflow_id: created.id, name: created.name });
                sdk.toast.show({ level: 'success', message: t('已保存并开始运行', 'Saved and run started') });
                window.location.hash = '#/runs';
            } else {
                sdk.toast.show({ level: 'success', message: t('已保存, 可在画布中调整', 'Saved — refine in canvas') });
                window.location.hash = `#/canvas/${created.id}`;
            }
        } catch (e) {
            console.error('[cowork-web] delegate save failed', e);
            sdk.toast.show({ level: 'error', message: t('保存失败', 'Save failed') });
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-end gap-2 pb-4">
                <textarea
                    className="min-h-16 flex-1 rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary"
                    placeholder={t(
                        '用自然语言描述任务, 例如: 每天整理下载文件夹并汇报',
                        'Describe the task in natural language, e.g.: tidy the Downloads folder daily and report'
                    )}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                />
                <button
                    type="button"
                    className="rounded-md bg-brand px-3 py-2 text-sm text-white hover:opacity-90 disabled:opacity-50"
                    disabled={busy || !prompt.trim()}
                    onClick={() => void generate()}
                >
                    {busy ? t('生成中…', 'Generating…') : t('生成工作流', 'Generate')}
                </button>
            </div>

            {error ? <ErrorState message={error} onRetry={() => void generate()} /> : null}

            {!wf && !error && !busy ? (
                <EmptyState
                    title={t('还没有委托', 'No delegation yet')}
                    description={t('描述目标, AI 生成工作流草稿', 'Describe a goal — AI drafts the workflow')}
                />
            ) : null}
            {busy ? <SkeletonLines rows={4} /> : null}

            {wf ? (
                <div className="rounded-lg border border-border bg-bg-elevated p-3">
                    <div className="text-sm font-medium text-text-primary">{wf.name || t('未命名', 'Untitled')}</div>
                    {wf.description ? <div className="mt-1 text-xs text-text-secondary">{wf.description}</div> : null}
                    <div className="mt-2 flex flex-wrap gap-1">
                        {(wf.nodes ?? []).map((n) => (
                            <span
                                key={n.id}
                                className="flex items-center gap-1 rounded border border-border bg-bg px-1.5 py-0.5 text-[10px] text-text-primary"
                            >
                                <span
                                    className="h-1.5 w-1.5 rounded-full"
                                    style={{ background: CATEGORY_COLOR[n.category ?? ''] ?? '#6b7280' }}
                                />
                                {n.config?.label || n.id}
                            </span>
                        ))}
                    </div>
                    {(wf.edges ?? []).length > 0 ? (
                        <div className="mt-2 font-mono text-[10px] text-text-muted">
                            {(wf.edges ?? []).map((e) => `${e.source_id} → ${e.target_id}`).join('  ')}
                        </div>
                    ) : null}
                    <div className="mt-3 flex justify-end gap-2">
                        <button
                            type="button"
                            className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                            disabled={saving}
                            onClick={() => void save(false)}
                        >
                            {t('保存到画布', 'Save to canvas')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                            disabled={saving}
                            onClick={() => void save(true)}
                        >
                            {t('保存并运行', 'Save & run')}
                        </button>
                    </div>
                </div>
            ) : null}
        </div>
    );
}
