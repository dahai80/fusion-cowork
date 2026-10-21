import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useRef, useState } from 'react';
import { type CanvasEdgeDef, type CanvasNodeDef, api } from '../api';
import { useT } from '../i18n';
import { onPreferredModelChange, preferredModel, setPreferredModel } from '../modelPref';
import { sdk } from '../sdk';

// W7: NL delegation entry — prompt → NLWorkflowGenerator (/api/ai/generate-workflow)
// → preview → save as workflow (+ optional run). Real generation needs fusion-mlx
// live; errors surface as 502 toasts.
// #144: 输入防抖 (800ms) 即时生成预览; 预览卡内可就地编辑节点 (标签/参数/删除)
// 与边 (删除/新增), 确认后才提交落库。

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
    const [model, setModel] = useState(preferredModel);
    const models = useAutoRetry(() => api.models(), []);
    // #144: 防抖即时预览 — 序号守卫丢弃过期响应; 编辑器开关与参数草稿
    const genSeq = useRef(0);
    const [editNode, setEditNode] = useState('');
    const [paramsDraft, setParamsDraft] = useState('');
    const [edgeFrom, setEdgeFrom] = useState('');
    const [edgeTo, setEdgeTo] = useState('');

    useEffect(() => onPreferredModelChange(setModel), []);

    async function generate() {
        const text = prompt.trim();
        if (busy || !text) return;
        const seq = ++genSeq.current;
        setBusy(true);
        setError('');
        try {
            const res = await api.generateWorkflow({ prompt: text, model: model || undefined });
            if (seq !== genSeq.current) return;
            setWf(res.workflow);
            setEditNode('');
        } catch (e) {
            if (seq !== genSeq.current) return;
            console.error('[cowork-web] nl generate failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            if (seq === genSeq.current) setBusy(false);
        }
    }

    // #144: 输入停顿 800ms 即自动生成预览 (最短 6 字符), 手动按钮仍可用。
    // generate 是每次渲染重建的闭包, 读到的恒为最新 prompt/model — 无需进 deps。
    // biome-ignore lint/correctness/useExhaustiveDependencies: debounce 只随 prompt/model 触发
    useEffect(() => {
        const text = prompt.trim();
        if (text.length < 6) return;
        const timer = window.setTimeout(() => void generate(), 800);
        return () => window.clearTimeout(timer);
    }, [prompt, model]);

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

    // #144: 就地编辑 helpers — 节点标签/参数/删除, 边删除/新增 (确认前不落库)
    function patchWf(patch: Partial<GeneratedWorkflow>) {
        setWf((prev) => (prev ? { ...prev, ...patch } : prev));
    }
    function updateNodeLabel(id: string, label: string) {
        setWf((prev) =>
            prev
                ? {
                      ...prev,
                      nodes: prev.nodes.map((n) => (n.id === id ? { ...n, config: { ...n.config, label } } : n)),
                  }
                : prev
        );
    }
    function removeNode(id: string) {
        setWf((prev) =>
            prev
                ? {
                      ...prev,
                      nodes: prev.nodes.filter((n) => n.id !== id),
                      edges: prev.edges.filter((e) => e.source_id !== id && e.target_id !== id),
                  }
                : prev
        );
        if (editNode === id) setEditNode('');
    }
    function removeEdge(idx: number) {
        setWf((prev) => (prev ? { ...prev, edges: prev.edges.filter((_, i) => i !== idx) } : prev));
    }
    function addEdge() {
        if (!wf || !edgeFrom || !edgeTo) return;
        if (edgeFrom === edgeTo) {
            sdk.toast.show({ level: 'warning', message: t('不能连接节点自身', 'Cannot connect a node to itself') });
            return;
        }
        if (wf.edges.some((e) => e.source_id === edgeFrom && e.target_id === edgeTo)) {
            sdk.toast.show({ level: 'warning', message: t('该边已存在', 'Edge already exists') });
            return;
        }
        setWf({ ...wf, edges: [...wf.edges, { source_id: edgeFrom, target_id: edgeTo }] });
        setEdgeFrom('');
        setEdgeTo('');
    }
    function openParams(n: CanvasNodeDef) {
        if (editNode === n.id) {
            setEditNode('');
            return;
        }
        setEditNode(n.id);
        setParamsDraft(JSON.stringify(n.config?.params ?? {}, null, 4));
    }
    function applyParams(id: string) {
        let parsed: Record<string, unknown>;
        try {
            parsed = JSON.parse(paramsDraft || '{}') as Record<string, unknown>;
        } catch {
            sdk.toast.show({ level: 'error', message: t('参数 JSON 解析失败', 'Invalid params JSON') });
            return;
        }
        setWf((prev) =>
            prev
                ? {
                      ...prev,
                      nodes: prev.nodes.map((n) =>
                          n.id === id ? { ...n, config: { ...n.config, params: parsed } } : n
                      ),
                  }
                : prev
        );
        setEditNode('');
        sdk.toast.show({ level: 'success', message: t('参数已更新 (尚未保存)', 'Params updated (not saved yet)') });
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center gap-2 pb-2">
                <span className="text-xs text-text-secondary">{t('模型', 'Model')}</span>
                <select
                    className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                    value={model}
                    onChange={(e) => {
                        setModel(e.target.value);
                        setPreferredModel(e.target.value);
                    }}
                >
                    <option value="">{t('自动选择', 'Auto')}</option>
                    {(models.data?.models ?? []).map((m) => (
                        <option key={m.id} value={m.id}>
                            {m.id}
                        </option>
                    ))}
                </select>
                {models.loading && !models.data ? (
                    <span className="text-xs text-text-muted">{t('加载中…', 'Loading…')}</span>
                ) : null}
                {models.error ? (
                    <span className="text-xs text-text-muted">{t('模型列表不可用', 'Models unavailable')}</span>
                ) : null}
            </div>
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
                    <div className="text-[10px] text-text-muted">
                        {t('预览可就地编辑, 确认后提交', 'Preview is editable — confirm to submit')}
                    </div>
                    <input
                        className="mt-1 w-full rounded-md border border-transparent bg-transparent px-1 py-0.5 text-sm font-medium text-text-primary hover:border-border focus:border-accent focus:outline-none"
                        value={wf.name ?? ''}
                        placeholder={t('未命名', 'Untitled')}
                        onChange={(e) => patchWf({ name: e.target.value })}
                    />
                    <input
                        className="mt-0.5 w-full rounded-md border border-transparent bg-transparent px-1 py-0.5 text-xs text-text-secondary hover:border-border focus:border-accent focus:outline-none"
                        value={wf.description ?? ''}
                        placeholder={t('描述 (可选)', 'Description (optional)')}
                        onChange={(e) => patchWf({ description: e.target.value })}
                    />
                    <div className="mt-2 space-y-1">
                        {(wf.nodes ?? []).map((n) => (
                            <div key={n.id} className="rounded-md border border-border bg-bg px-1.5 py-1">
                                <div className="flex items-center gap-1.5">
                                    <span
                                        className="h-1.5 w-1.5 shrink-0 rounded-full"
                                        style={{ background: CATEGORY_COLOR[n.category ?? ''] ?? '#6b7280' }}
                                    />
                                    <span className="shrink-0 font-mono text-[10px] text-text-muted">{n.name}</span>
                                    <input
                                        className="min-w-0 flex-1 rounded border border-transparent bg-transparent px-1 text-xs text-text-primary hover:border-border focus:border-accent focus:outline-none"
                                        value={n.config?.label || ''}
                                        placeholder={n.id}
                                        onChange={(e) => updateNodeLabel(n.id, e.target.value)}
                                    />
                                    <button
                                        type="button"
                                        className={`shrink-0 rounded px-1 text-[10px] ${editNode === n.id ? 'bg-brand/15 text-brand' : 'text-text-muted hover:bg-bg-muted'}`}
                                        title={t('编辑参数', 'Edit params')}
                                        onClick={() => openParams(n)}
                                    >
                                        {t('参数', 'params')}
                                    </button>
                                    <button
                                        type="button"
                                        className="shrink-0 rounded px-1 text-[10px] text-error hover:bg-error/10"
                                        title={t('删除节点', 'Delete node')}
                                        onClick={() => removeNode(n.id)}
                                    >
                                        {t('删除', 'del')}
                                    </button>
                                </div>
                                {editNode === n.id ? (
                                    <div className="mt-1 border-t border-border pt-1">
                                        <textarea
                                            className="min-h-24 w-full rounded border border-border bg-bg px-1.5 py-1 font-mono text-[11px] text-text-primary"
                                            value={paramsDraft}
                                            onChange={(e) => setParamsDraft(e.target.value)}
                                        />
                                        <div className="mt-1 flex justify-end gap-1">
                                            <button
                                                type="button"
                                                className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-primary hover:bg-bg-muted"
                                                onClick={() => setEditNode('')}
                                            >
                                                {t('取消', 'Cancel')}
                                            </button>
                                            <button
                                                type="button"
                                                className="rounded bg-brand px-1.5 py-0.5 text-[10px] text-white hover:opacity-90"
                                                onClick={() => applyParams(n.id)}
                                            >
                                                {t('应用', 'Apply')}
                                            </button>
                                        </div>
                                    </div>
                                ) : null}
                            </div>
                        ))}
                    </div>
                    <div className="mt-2 space-y-0.5">
                        {(wf.edges ?? []).map((e, i) => (
                            <div
                                key={`${e.source_id}-${e.target_id}-${i}`}
                                className="flex items-center gap-1.5 font-mono text-[10px] text-text-muted"
                            >
                                <span className="min-w-0 flex-1 truncate">{`${e.source_id} → ${e.target_id}`}</span>
                                <button
                                    type="button"
                                    className="shrink-0 rounded px-1 text-[10px] text-error hover:bg-error/10"
                                    onClick={() => removeEdge(i)}
                                >
                                    {t('删除', 'del')}
                                </button>
                            </div>
                        ))}
                        <div className="flex items-center gap-1 pt-1">
                            <select
                                className="rounded border border-border bg-bg px-1 py-0.5 text-[10px] text-text-primary"
                                value={edgeFrom}
                                onChange={(e) => setEdgeFrom(e.target.value)}
                            >
                                <option value="">{t('源节点', 'Source')}</option>
                                {(wf.nodes ?? []).map((n) => (
                                    <option key={n.id} value={n.id}>
                                        {n.config?.label || n.id}
                                    </option>
                                ))}
                            </select>
                            <span className="text-[10px] text-text-muted">→</span>
                            <select
                                className="rounded border border-border bg-bg px-1 py-0.5 text-[10px] text-text-primary"
                                value={edgeTo}
                                onChange={(e) => setEdgeTo(e.target.value)}
                            >
                                <option value="">{t('目标节点', 'Target')}</option>
                                {(wf.nodes ?? []).map((n) => (
                                    <option key={n.id} value={n.id}>
                                        {n.config?.label || n.id}
                                    </option>
                                ))}
                            </select>
                            <button
                                type="button"
                                className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                disabled={!edgeFrom || !edgeTo}
                                onClick={addEdge}
                            >
                                {t('加边', 'Add')}
                            </button>
                        </div>
                    </div>
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
