import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useRef, useState } from 'react';
import { type CanvasEdgeDef, type CanvasNodeDef, type ChatDelegateResult, type RunRecord, api } from '../api';
import { useT } from '../i18n';
import { onPreferredModelChange, preferredModel, setPreferredModel } from '../modelPref';
import { isTerminalRunStatus } from '../runstatus';
import { sdk } from '../sdk';

// W7: NL delegation entry — prompt → NLWorkflowGenerator (/api/ai/generate-workflow)
// → preview → save as workflow (+ optional run). Real generation needs fusion-mlx
// live; errors surface as 502 toasts.
// #144: 800ms input debounce generates the preview instantly; nodes (label/params/delete)
// and edges (delete/add) are editable in place on the preview card, committed only on confirm.
// #148: chat-first default mode — one line runs directly / produces a deliverable; complex tasks return an
// internal plan (DAG still behind the scenes), approve to run; DAG editing stays optional in "advanced" mode.

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

// #148: chat-first result card — auto mode polls run progress, terminal state shows result + artifacts;
// plan mode shows the internal plan with one-click approve to run (workflow already persisted, canvas editing optional).
function ChatResultCard({
    result,
    memory,
    onGoRuns,
}: {
    result: ChatDelegateResult;
    memory: boolean;
    onGoRuns: () => void;
}) {
    const t = useT();
    const [run, setRun] = useState<RunRecord | null>(null);
    const [starting, setStarting] = useState(false);

    useEffect(() => {
        const runId = result.run_id;
        if (!runId) return;
        let stopped = false;
        async function poll() {
            try {
                const rec = await api.run(runId as string);
                if (stopped) return;
                setRun(rec);
                if (isTerminalRunStatus(rec.status)) stopped = true;
            } catch (e) {
                console.error('[cowork-web] chat-delegate poll failed', e);
            }
        }
        void poll();
        const timer = window.setInterval(() => void poll(), 1500);
        return () => {
            stopped = true;
            window.clearInterval(timer);
        };
    }, [result.run_id]);

    async function approve() {
        if (starting) return;
        setStarting(true);
        try {
            const started = await api.startRun({ workflow_id: result.workflow_id, memory });
            setRun(await api.run(started.run_id));
        } catch (e) {
            console.error('[cowork-web] chat-delegate approve failed', e);
            sdk.toast.show({ level: 'error', message: t('执行启动失败', 'Failed to start run') }); // i18n: user-facing
        } finally {
            setStarting(false);
        }
    }

    const running = run && !isTerminalRunStatus(run.status);
    return (
        <div className="rounded-lg border border-border bg-bg-elevated p-3">
            <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-text-primary">{result.plan.name}</div>
                <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-muted">
                    {
                        result.mode === 'auto'
                            ? t('直接执行', 'Auto-run') // i18n: user-facing
                            : t('待批准计划', 'Plan') /* i18n: user-facing */
                    }
                </span>
            </div>
            <div className="mt-1 space-y-0.5">
                {result.plan.steps.map((s, i) => (
                    <div key={i} className="flex items-center gap-1.5 text-xs text-text-secondary">
                        <span className="text-text-muted">{i + 1}.</span>
                        <span>{s.label || s.display || s.node}</span>
                        {s.node ? <span className="font-mono text-[10px] text-text-muted">{s.node}</span> : null}
                    </div>
                ))}
            </div>
            {result.plan.warnings && result.plan.warnings.length > 0 ? (
                <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5">
                    {result.plan.warnings.map((w, i) => (
                        <div key={i} className="text-xs text-amber-600 dark:text-amber-400">
                            {w}
                        </div>
                    ))}
                </div>
            ) : null}
            {!run && result.mode === 'plan' ? (
                <div className="mt-2 flex items-center justify-end gap-2">
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                        onClick={() => {
                            window.location.hash = `#/canvas/${result.workflow_id}`;
                        }}
                    >
                        {t('在画布查看', 'Open in canvas') /* i18n: user-facing */}
                    </button>
                    <button
                        type="button"
                        className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                        disabled={starting}
                        onClick={() => void approve()}
                    >
                        {
                            starting
                                ? t('启动中…', 'Starting…') // i18n: user-facing
                                : t('批准并执行', 'Approve & run') /* i18n: user-facing */
                        }
                    </button>
                </div>
            ) : null}
            {run ? (
                <div className="mt-2 border-t border-border pt-2">
                    <div className="flex items-center gap-2 text-xs text-text-secondary">
                        <StatusBadge status={run.status as never} />
                        <span>
                            {t('步骤', 'Steps') /* i18n: user-facing */} {run.steps?.length ?? 0}
                        </span>
                        {running ? (
                            <span className="animate-pulse text-text-muted">
                                {t('执行中…', 'Running…') /* i18n: user-facing */}
                            </span>
                        ) : null}
                    </div>
                    {run.result?.summary ? (
                        <div className="mt-1 text-xs text-text-primary">{run.result.summary}</div>
                    ) : null}
                    {run.error ? <div className="mt-1 text-xs text-error">{run.error}</div> : null}
                    {!isTerminalRunStatus(run.status) ? (
                        <button
                            type="button"
                            className="mt-2 rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted"
                            onClick={onGoRuns}
                        >
                            {t('查看运行详情', 'View run') /* i18n: user-facing */}
                        </button>
                    ) : (
                        <button
                            type="button"
                            className="mt-2 rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted"
                            onClick={onGoRuns}
                        >
                            {t('查看运行详情', 'View run') /* i18n: user-facing */}
                        </button>
                    )}
                </div>
            ) : null}
        </div>
    );
}

export function DelegatePage() {
    const t = useT();
    const [prompt, setPrompt] = useState('');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [wf, setWf] = useState<GeneratedWorkflow | null>(null);
    const [saving, setSaving] = useState(false);
    const [model, setModel] = useState(preferredModel);
    // #148: chat-first default; advanced keeps the #144 DAG preview-edit path
    const [mode, setMode] = useState<'chat' | 'advanced'>('chat');
    const [chatResult, setChatResult] = useState<ChatDelegateResult | null>(null);
    const models = useAutoRetry(() => api.models(), []);
    // #144: debounced instant preview — sequence guard discards stale responses; editor toggles and params draft
    const genSeq = useRef(0);
    const [editNode, setEditNode] = useState('');
    const [paramsDraft, setParamsDraft] = useState('');
    const [edgeFrom, setEdgeFrom] = useState('');
    const [edgeTo, setEdgeTo] = useState('');
    // issue #157: per-run memory toggle (on by default)
    const [memoryEnabled, setMemoryEnabled] = useState(true);

    useEffect(() => onPreferredModelChange(setModel), []);
    // #147: after a model-list failure retry slowly every 30s until recovery (backfills automatically once the service is back),
    // replacing the old fail-and-stay-dead behavior; manual retry button sits next to the dropdown.
    const modelsRetryRef = useRef(models.manualRetry);
    modelsRetryRef.current = models.manualRetry;
    useEffect(() => {
        if (!models.error) return;
        const timer = window.setInterval(() => modelsRetryRef.current(), 30000);
        return () => window.clearInterval(timer);
    }, [models.error]);

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

    // #148: chat-first submit — server-side deterministic triage (simple runs directly / complex returns a plan)
    async function chatSubmit() {
        const text = prompt.trim();
        if (busy || !text) return;
        const seq = ++genSeq.current;
        setBusy(true);
        setError('');
        try {
            const res = await api.chatDelegate({ prompt: text, model: model || undefined, memory: memoryEnabled });
            if (seq !== genSeq.current) return;
            setChatResult(res);
        } catch (e) {
            if (seq !== genSeq.current) return;
            console.error('[cowork-web] chat delegate failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            if (seq === genSeq.current) setBusy(false);
        }
    }

    // #144: an 800ms input pause auto-generates the preview (minimum 6 chars), manual button still works.
    // chat mode debounces into a direct submit the same way.
    // generate is a closure rebuilt on every render, always reading the latest prompt/model — no need in deps.
    // biome-ignore lint/correctness/useExhaustiveDependencies: debounce only re-arms on prompt/model changes
    useEffect(() => {
        const text = prompt.trim();
        if (text.length < 6) return;
        const timer = window.setTimeout(() => void (mode === 'chat' ? chatSubmit() : generate()), 800);
        return () => window.clearTimeout(timer);
    }, [prompt, model, mode]);

    async function save(alsoRun: boolean) {
        if (!wf || saving) return;
        setSaving(true);
        try {
            const created = await api.createWorkflow({
                name: wf.name || t('未命名委托', 'Untitled delegation'), // i18n: user-facing
                description: wf.description || '',
                workflow: { name: wf.name || '', nodes: wf.nodes, edges: wf.edges },
            });
            if (alsoRun) {
                await api.startRun({ workflow_id: created.id, name: created.name, memory: memoryEnabled });
                sdk.toast.show({ level: 'success', message: t('已保存并开始运行', 'Saved and run started') }); // i18n: user-facing
                window.location.hash = '#/runs';
            } else {
                sdk.toast.show({ level: 'success', message: t('已保存, 可在画布中调整', 'Saved — refine in canvas') }); // i18n: user-facing
                window.location.hash = `#/canvas/${created.id}`;
            }
        } catch (e) {
            console.error('[cowork-web] delegate save failed', e);
            sdk.toast.show({ level: 'error', message: t('保存失败', 'Save failed') }); // i18n: user-facing
        } finally {
            setSaving(false);
        }
    }

    // #144: in-place edit helpers — node label/params/delete, edge delete/add (nothing persisted before confirm)
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
            sdk.toast.show({ level: 'warning', message: t('不能连接节点自身', 'Cannot connect a node to itself') }); // i18n: user-facing
            return;
        }
        if (wf.edges.some((e) => e.source_id === edgeFrom && e.target_id === edgeTo)) {
            sdk.toast.show({ level: 'warning', message: t('该边已存在', 'Edge already exists') }); // i18n: user-facing
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
            sdk.toast.show({ level: 'error', message: t('参数 JSON 解析失败', 'Invalid params JSON') }); // i18n: user-facing
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
        sdk.toast.show({ level: 'success', message: t('参数已更新 (尚未保存)', 'Params updated (not saved yet)') }); // i18n: user-facing
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center gap-2 pb-2">
                <div className="mr-2 flex rounded-md border border-border p-0.5">
                    <button
                        type="button"
                        className={`rounded px-2 py-0.5 text-xs ${mode === 'chat' ? 'bg-brand text-white' : 'text-text-secondary hover:bg-bg-muted'}`}
                        onClick={() => {
                            setMode('chat');
                            setWf(null);
                        }}
                    >
                        {t('对话委托', 'Chat') /* i18n: user-facing */}
                    </button>
                    <button
                        type="button"
                        className={`rounded px-2 py-0.5 text-xs ${mode === 'advanced' ? 'bg-brand text-white' : 'text-text-secondary hover:bg-bg-muted'}`}
                        onClick={() => {
                            setMode('advanced');
                            setChatResult(null);
                        }}
                    >
                        {t('高级·DAG预览', 'Advanced') /* i18n: user-facing */}
                    </button>
                </div>
                <span className="text-xs text-text-secondary">{t('模型', 'Model') /* i18n: user-facing */}</span>
                <select
                    className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                    value={model}
                    onChange={(e) => {
                        setModel(e.target.value);
                        setPreferredModel(e.target.value);
                    }}
                >
                    <option value="">{t('自动选择', 'Auto') /* i18n: user-facing */}</option>
                    {(models.data?.models ?? []).map((m) => (
                        <option key={m.id} value={m.id}>
                            {m.id}
                        </option>
                    ))}
                </select>
                {models.loading && !models.data ? (
                    <span className="text-xs text-text-muted">{t('加载中…', 'Loading…') /* i18n: user-facing */}</span>
                ) : null}
                {models.data?.stale ? (
                    <span
                        className="text-xs text-text-muted"
                        title={t('模型服务暂时不可用, 显示缓存列表', 'Model service unavailable — cached list shown')} // i18n: user-facing
                    >
                        {t('缓存', 'Cached') /* i18n: user-facing */}
                    </span>
                ) : null}
                {models.error ? (
                    <span className="flex items-center gap-1 text-xs text-text-muted">
                        {t('模型列表不可用', 'Models unavailable') /* i18n: user-facing */}
                        <button
                            type="button"
                            className="rounded border border-border px-1 hover:bg-bg-muted"
                            onClick={() => models.manualRetry()}
                        >
                            {t('重试', 'Retry') /* i18n: user-facing */}
                        </button>
                    </span>
                ) : null}
            </div>
            <div className="flex items-end gap-2 pb-4">
                <textarea
                    className="min-h-16 flex-1 rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary"
                    placeholder={t(
                        '用自然语言描述任务, 例如: 每天整理下载文件夹并汇报', // i18n: user-facing
                        'Describe the task in natural language, e.g.: tidy the Downloads folder daily and report'
                    )}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                />
                <button
                    type="button"
                    className="rounded-md bg-brand px-3 py-2 text-sm text-white hover:opacity-90 disabled:opacity-50"
                    disabled={busy || !prompt.trim()}
                    onClick={() => void (mode === 'chat' ? chatSubmit() : generate())}
                >
                    {
                        busy
                            ? t('处理中…', 'Working…') // i18n: user-facing
                            : mode === 'chat'
                              ? t('发送任务', 'Send task') /* i18n: user-facing */
                              : t('生成工作流', 'Generate') /* i18n: user-facing */
                    }
                </button>
            </div>

            {error ? (
                <ErrorState message={error} onRetry={() => void (mode === 'chat' ? chatSubmit() : generate())} />
            ) : null}

            {mode === 'chat' ? (
                <>
                    {!chatResult && !error && !busy ? (
                        <EmptyState
                            title={t('一句话交差', 'One line, one deliverable')} // i18n: user-facing
                            description={t(
                                '描述目标: 简单任务直接执行交付, 复杂任务先给执行计划', // i18n: user-facing
                                'Simple tasks run directly; complex ones show a plan first'
                            )}
                        />
                    ) : null}
                    {busy ? <SkeletonLines rows={4} /> : null}
                    {chatResult ? (
                        <ChatResultCard
                            result={chatResult}
                            memory={memoryEnabled}
                            onGoRuns={() => {
                                window.location.hash = '#/runs';
                            }}
                        />
                    ) : null}
                </>
            ) : (
                <>
                    {!wf && !error && !busy ? (
                        <EmptyState
                            title={t('还没有委托', 'No delegation yet')} // i18n: user-facing
                            description={t('描述目标, AI 生成工作流草稿', 'Describe a goal — AI drafts the workflow')} // i18n: user-facing
                        />
                    ) : null}
                    {busy ? <SkeletonLines rows={4} /> : null}

                    {wf ? (
                        <div className="rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="text-[10px] text-text-muted">
                                {
                                    t(
                                        '预览可就地编辑, 确认后提交', // i18n: user-facing
                                        'Preview is editable — confirm to submit'
                                    ) /* i18n: user-facing */
                                }
                            </div>
                            <input
                                className="mt-1 w-full rounded-md border border-transparent bg-transparent px-1 py-0.5 text-sm font-medium text-text-primary hover:border-border focus:border-accent focus:outline-none"
                                value={wf.name ?? ''}
                                placeholder={t('未命名', 'Untitled')} // i18n: user-facing
                                onChange={(e) => patchWf({ name: e.target.value })}
                            />
                            <input
                                className="mt-0.5 w-full rounded-md border border-transparent bg-transparent px-1 py-0.5 text-xs text-text-secondary hover:border-border focus:border-accent focus:outline-none"
                                value={wf.description ?? ''}
                                placeholder={t('描述 (可选)', 'Description (optional)')} // i18n: user-facing
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
                                            <span className="shrink-0 font-mono text-[10px] text-text-muted">
                                                {n.name}
                                            </span>
                                            <input
                                                className="min-w-0 flex-1 rounded border border-transparent bg-transparent px-1 text-xs text-text-primary hover:border-border focus:border-accent focus:outline-none"
                                                value={n.config?.label || ''}
                                                placeholder={n.id}
                                                onChange={(e) => updateNodeLabel(n.id, e.target.value)}
                                            />
                                            <button
                                                type="button"
                                                className={`shrink-0 rounded px-1 text-[10px] ${editNode === n.id ? 'bg-brand/15 text-brand' : 'text-text-muted hover:bg-bg-muted'}`}
                                                title={t('编辑参数', 'Edit params')} // i18n: user-facing
                                                onClick={() => openParams(n)}
                                            >
                                                {t('参数', 'params') /* i18n: user-facing */}
                                            </button>
                                            <button
                                                type="button"
                                                className="shrink-0 rounded px-1 text-[10px] text-error hover:bg-error/10"
                                                title={t('删除节点', 'Delete node')} // i18n: user-facing
                                                onClick={() => removeNode(n.id)}
                                            >
                                                {t('删除', 'del') /* i18n: user-facing */}
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
                                                        {t('取消', 'Cancel') /* i18n: user-facing */}
                                                    </button>
                                                    <button
                                                        type="button"
                                                        className="rounded bg-brand px-1.5 py-0.5 text-[10px] text-white hover:opacity-90"
                                                        onClick={() => applyParams(n.id)}
                                                    >
                                                        {t('应用', 'Apply') /* i18n: user-facing */}
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
                                            {t('删除', 'del') /* i18n: user-facing */}
                                        </button>
                                    </div>
                                ))}
                                <div className="flex items-center gap-1 pt-1">
                                    <select
                                        className="rounded border border-border bg-bg px-1 py-0.5 text-[10px] text-text-primary"
                                        value={edgeFrom}
                                        onChange={(e) => setEdgeFrom(e.target.value)}
                                    >
                                        <option value="">{t('源节点', 'Source') /* i18n: user-facing */}</option>
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
                                        <option value="">{t('目标节点', 'Target') /* i18n: user-facing */}</option>
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
                                        {t('加边', 'Add') /* i18n: user-facing */}
                                    </button>
                                </div>
                            </div>
                            <div className="mt-3 flex items-center justify-end gap-2">
                                <label
                                    className="mr-auto flex items-center gap-1 text-sm text-text-secondary"
                                    title={t(
                                        '运行结束后把产出写入 fusion-memory 长期记忆', // i18n: user-facing
                                        'Write run outcomes to fusion-memory after the run ends'
                                    )}
                                >
                                    <input
                                        type="checkbox"
                                        checked={memoryEnabled}
                                        onChange={(e) => setMemoryEnabled(e.target.checked)}
                                        disabled={saving}
                                    />
                                    {t('记忆', 'Memory') /* i18n: user-facing */}
                                </label>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                    disabled={saving}
                                    onClick={() => void save(false)}
                                >
                                    {t('保存到画布', 'Save to canvas') /* i18n: user-facing */}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                                    disabled={saving}
                                    onClick={() => void save(true)}
                                >
                                    {t('保存并运行', 'Save & run') /* i18n: user-facing */}
                                </button>
                            </div>
                        </div>
                    ) : null}
                </>
            )}
        </div>
    );
}
