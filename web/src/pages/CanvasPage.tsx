import { ErrorState, ReadOnlyBanner, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import {
    Background,
    Controls,
    type Edge,
    Handle,
    type Node,
    type NodeProps,
    Position,
    ReactFlow,
    useEdgesState,
    useNodesState,
} from '@xyflow/react';
import { useCallback, useEffect, useState } from 'react';
import { type CanvasEdgeDef, type CanvasNodeDef, type NodeInfo, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// DAG canvas editor (desktop). Mobile (<768px, manifest devices=mobile-readonly)
// renders the same canvas read-only: no palette, no param editing, no connect,
// no drag — spec §8.2. Desktop-only editing per PRD N-6.

interface FlowNodeData extends Record<string, unknown> {
    label: string;
    nodeType: string;
    category: string;
    params: Record<string, unknown>;
    readonly: boolean;
    selectedForEdit: boolean;
}

function CoworkNode({ data, selected }: NodeProps<Node<FlowNodeData>>) {
    return (
        <div
            className={`min-w-36 rounded-lg border bg-bg-elevated px-3 py-2 ${
                data.selectedForEdit || selected ? 'border-brand' : 'border-border'
            }`}
        >
            <Handle type="target" position={Position.Top} />
            <div className="text-xs font-medium text-text-primary">{data.label}</div>
            <div className="mt-0.5 text-[10px] text-text-muted">
                {data.nodeType} · {data.category}
            </div>
            <div className="mt-1 font-mono text-[10px] text-text-secondary">
                {Object.keys(data.params ?? {}).length} {data.readonly ? '' : 'params'}
            </div>
            <Handle type="source" position={Position.Bottom} />
        </div>
    );
}

const nodeTypes = { cowork: CoworkNode };

const CATEGORY_COLOR: Record<string, string> = {
    tool: '#3b82f6',
    macos_system: '#8b5cf6',
    file_operation: '#10b981',
    ai_processing: '#f59e0b',
    io: '#06b6d4',
    logic: '#ef4444',
    fusion_ecosystem: '#ec4899',
};

function nodeDefToFlow(def: CanvasNodeDef, selectedForEdit: boolean): Node<FlowNodeData> {
    return {
        id: def.id,
        type: 'cowork',
        position: def.position ?? { x: 120, y: 80 },
        data: {
            label: def.config?.label || def.id,
            nodeType: def.name,
            category: def.category ?? '',
            params: def.config?.params ?? {},
            readonly: false,
            selectedForEdit,
        },
    } as Node<FlowNodeData>;
}

export function CanvasPage({ workflowId }: { workflowId: string }) {
    const t = useT();
    const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width: 767px)').matches);
    const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
    const [selectedId, setSelectedId] = useState('');
    const [name, setName] = useState('');
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [loadPending, setLoadPending] = useState(!!workflowId);

    const nodesRetry = useAutoRetry(
        useCallback(() => api.nodes(), []),
        []
    );

    useEffect(() => {
        const mq = window.matchMedia('(max-width: 767px)');
        const onMq = () => setIsMobile(mq.matches);
        mq.addEventListener('change', onMq);
        return () => mq.removeEventListener('change', onMq);
    }, []);

    // Load existing workflow definition
    useEffect(() => {
        if (!workflowId) return;
        let cancelled = false;
        setLoadPending(true);
        api.workflow(workflowId)
            .then((rec) => {
                if (cancelled) return;
                setName(rec.name);
                setNodes((rec.workflow.nodes ?? []).map((d) => nodeDefToFlow(d, false)));
                setEdges(
                    (rec.workflow.edges ?? []).map<Edge>((e) => ({
                        id: `${e.source_id}->${e.target_id}`,
                        source: e.source_id,
                        target: e.target_id,
                        animated: true,
                    }))
                );
            })
            .catch((e: Error) => {
                if (!cancelled) setLoadError(e.message);
            })
            .finally(() => {
                if (!cancelled) setLoadPending(false);
            });
        return () => {
            cancelled = true;
        };
    }, [setEdges, setNodes, workflowId]);

    const isReadonly = isMobile;

    function addNode(info: NodeInfo) {
        if (isReadonly) return;
        const id = `n${Date.now().toString(36)}`;
        setNodes((ns) => [
            ...ns,
            {
                id,
                type: 'cowork',
                position: { x: 120 + Math.random() * 240, y: 80 + Math.random() * 160 },
                data: {
                    label: info.display_name || info.name,
                    nodeType: info.name,
                    category: info.category,
                    params: {},
                    readonly: false,
                    selectedForEdit: false,
                },
            },
        ]);
        setSelectedId(id);
    }

    function onConnect(params: { source: string; target: string }) {
        if (isReadonly || params.source === params.target) return;
        setEdges((es) => [
            ...es.filter((e) => !(e.source === params.source && e.target === params.target)),
            { id: `${params.source}->${params.target}`, source: params.source, target: params.target, animated: true },
        ]);
    }

    function selectedNode(): Node<FlowNodeData> | undefined {
        return nodes.find((n) => n.id === selectedId);
    }

    function updateSelectedParam(key: string, value: string) {
        setNodes((ns) =>
            ns.map((n) =>
                n.id === selectedId
                    ? {
                          ...n,
                          data: {
                              ...n.data,
                              params: { ...n.data.params, [key]: value },
                          },
                      }
                    : n
            )
        );
    }

    function addParam(key: string) {
        if (!key.trim()) return;
        setNodes((ns) =>
            ns.map((n) =>
                n.id === selectedId ? { ...n, data: { ...n.data, params: { ...n.data.params, [key.trim()]: '' } } } : n
            )
        );
    }

    function removeParam(key: string) {
        setNodes((ns) =>
            ns.map((n) => {
                if (n.id !== selectedId) return n;
                const next = { ...n.data.params };
                delete next[key];
                return { ...n, data: { ...n.data, params: next } };
            })
        );
    }

    async function save() {
        if (isReadonly || saving) return;
        if (!name.trim()) {
            sdk.toast.show({ level: 'warning', message: t('请填写工作流名称', 'Workflow name required') });
            return;
        }
        setSaving(true);
        const nodeDefs: CanvasNodeDef[] = nodes.map((n) => ({
            id: n.id,
            name: n.data.nodeType,
            category: n.data.category,
            position: n.position,
            config: { label: n.data.label, params: n.data.params },
        }));
        const edgeDefs: CanvasEdgeDef[] = edges.map((e) => ({
            source_id: e.source,
            target_id: e.target,
        }));
        try {
            if (workflowId) {
                await api.updateWorkflow(workflowId, {
                    name: name.trim(),
                    description: '',
                    workflow: { id: workflowId, name: name.trim(), nodes: nodeDefs, edges: edgeDefs },
                });
            } else {
                const created = await api.createWorkflow({
                    name: name.trim(),
                    description: '',
                    workflow: { name: name.trim(), nodes: nodeDefs, edges: edgeDefs },
                });
                window.location.hash = `#/canvas/${created.id}`;
            }
            sdk.toast.show({ level: 'success', message: t('已保存', 'Saved') });
        } catch (e) {
            console.error('[cowork-web] save failed', e);
            sdk.toast.show({ level: 'error', message: t('保存失败', 'Save failed') });
        } finally {
            setSaving(false);
        }
    }

    async function run() {
        if (isReadonly || running) return;
        setRunning(true);
        const nodeDefs: CanvasNodeDef[] = nodes.map((n) => ({
            id: n.id,
            name: n.data.nodeType,
            category: n.data.category,
            config: { label: n.data.label, params: n.data.params },
        }));
        const edgeDefs: CanvasEdgeDef[] = edges.map((e) => ({ source_id: e.source, target_id: e.target }));
        try {
            const res = await api.startRun({
                workflow: { name: name || 'canvas-run', nodes: nodeDefs, edges: edgeDefs },
                name: name,
            });
            console.info(`[cowork-web] run started: ${res.run_id}`);
            sdk.toast.show({ level: 'success', message: t('已提交运行', 'Run submitted') });
            window.location.hash = '#/runs';
        } catch (e) {
            console.error('[cowork-web] run failed', e);
            sdk.toast.show({ level: 'error', message: t('运行提交失败', 'Failed to submit run') });
        } finally {
            setRunning(false);
        }
    }

    if (loadPending) return <SkeletonLines rows={4} />;
    if (loadError)
        return (
            <ErrorState message={loadError} retryLabel={t('重试', 'Retry')} onRetry={() => window.location.reload()} />
        );

    const sel = selectedNode();
    const schemaProps =
        sel && nodesRetry.data
            ? (
                  nodesRetry.data.nodes.find((x) => x.name === sel.data.nodeType)?.params_schema as
                      | { properties?: Record<string, { type?: string; description?: string }> }
                      | undefined
              )?.properties
            : undefined;

    return (
        <div className="flex h-full min-h-0 flex-col">
            {isReadonly && isMobile ? (
                <ReadOnlyBanner
                    text={t('只读模式 — 画布编辑仅桌面端支持', 'Read-only — canvas editing is desktop-only')}
                />
            ) : null}
            <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
                <input
                    className="w-48 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary disabled:opacity-50"
                    placeholder={t('工作流名称', 'Workflow name')}
                    value={name}
                    disabled={isReadonly}
                    onChange={(e) => setName(e.target.value)}
                />
                <div className="flex gap-2">
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                        disabled={isReadonly || saving}
                        onClick={() => void save()}
                    >
                        {saving ? t('保存中…', 'Saving…') : t('保存', 'Save')}
                    </button>
                    <button
                        type="button"
                        className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                        disabled={isReadonly || running}
                        onClick={() => void run()}
                    >
                        {running ? t('提交中…', 'Submitting…') : t('运行', 'Run')}
                    </button>
                </div>
            </div>
            <div className="flex min-h-0 flex-1">
                {!isReadonly ? (
                    <aside className="w-44 shrink-0 overflow-y-auto border-r border-border p-2">
                        <div className="px-1 pb-1 text-xs font-medium text-text-secondary">
                            {t('节点面板', 'Node palette')}
                        </div>
                        {nodesRetry.loading && !nodesRetry.data ? (
                            <SkeletonLines rows={4} />
                        ) : nodesRetry.error ? (
                            <ErrorState message={nodesRetry.error.message} onRetry={nodesRetry.manualRetry} />
                        ) : (
                            (nodesRetry.data?.nodes ?? []).map((info) => (
                                <button
                                    type="button"
                                    key={info.name}
                                    className="mb-1 flex w-full items-center gap-2 rounded-md border border-border px-2 py-1.5 text-left text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => addNode(info)}
                                >
                                    <span
                                        className="h-2 w-2 shrink-0 rounded-full"
                                        style={{ background: CATEGORY_COLOR[info.category] ?? '#6b7280' }}
                                    />
                                    <span className="truncate">{info.display_name || info.name}</span>
                                </button>
                            ))
                        )}
                    </aside>
                ) : null}
                <div className="min-w-0 flex-1">
                    <ReactFlow
                        nodes={nodes}
                        edges={edges}
                        nodeTypes={nodeTypes}
                        onNodesChange={onNodesChange}
                        onEdgesChange={onEdgesChange}
                        onConnect={onConnect}
                        onNodeClick={(_, n) => setSelectedId(n.id)}
                        nodesDraggable={!isReadonly}
                        nodesConnectable={!isReadonly}
                        elementsSelectable={!isReadonly}
                        proOptions={{ hideAttribution: true }}
                        fitView
                    >
                        <Background />
                        <Controls showInteractive={!isReadonly} />
                    </ReactFlow>
                </div>
                {!isReadonly && sel ? (
                    <aside className="w-64 shrink-0 overflow-y-auto border-l border-border p-3">
                        <div className="text-sm font-medium text-text-primary">{sel.data.label}</div>
                        <div className="text-xs text-text-muted">
                            {sel.data.nodeType} · {sel.data.category}
                        </div>
                        <div className="mt-3 space-y-2">
                            {Object.entries(sel.data.params).map(([k, v]) => (
                                <div key={k} className="flex items-center gap-1">
                                    <input
                                        className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                        value={String(v)}
                                        onChange={(e) => updateSelectedParam(k, e.target.value)}
                                    />
                                    <button
                                        type="button"
                                        className="rounded px-1 text-xs text-error hover:bg-error/10"
                                        aria-label={`remove ${k}`}
                                        onClick={() => removeParam(k)}
                                    >
                                        ×
                                    </button>
                                </div>
                            ))}
                            <ParamAdder onAdd={addParam} />
                        </div>
                        {schemaProps ? (
                            <div className="mt-3 border-t border-border pt-2">
                                <div className="pb-1 text-xs font-medium text-text-secondary">
                                    {t('可用参数 (schema)', 'Schema params')}
                                </div>
                                {Object.entries(schemaProps).map(([k, p]) => (
                                    <div key={k} className="text-[10px] text-text-muted">
                                        <span className="font-mono text-text-secondary">{k}</span>
                                        {p.type ? ` · ${p.type}` : ''}
                                        {p.description ? ` — ${p.description}` : ''}
                                    </div>
                                ))}
                            </div>
                        ) : null}
                    </aside>
                ) : null}
            </div>
        </div>
    );
}

function ParamAdder({ onAdd }: { onAdd: (key: string) => void }) {
    const [key, setKey] = useState('');
    return (
        <div className="flex gap-1">
            <input
                className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                placeholder="param key"
                value={key}
                onChange={(e) => setKey(e.target.value)}
                onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                        onAdd(key);
                        setKey('');
                    }
                }}
            />
            <button
                type="button"
                className="rounded-md border border-border px-2 text-xs text-text-primary hover:bg-bg-muted"
                onClick={() => {
                    onAdd(key);
                    setKey('');
                }}
            >
                +
            </button>
        </div>
    );
}
