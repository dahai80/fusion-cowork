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
import { useCallback, useEffect, useRef, useState } from 'react';
import { type CanvasEdgeDef, type CanvasNodeDef, type NodeInfo, api } from '../api';
import { fieldKind, isTrueish, runStatusByNode, schemaFields, toneColor } from '../canvas';
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
    runStatus?: string;
}

function CoworkNode({ data, selected }: NodeProps<Node<FlowNodeData>>) {
    // issue #261: run 进行中按节点终态着色 (blocked=闸门等待, failed=红, passed=绿)
    const runBorder = data.runStatus ? toneColor(data.runStatus) : '';
    return (
        <div
            className={`min-w-36 rounded-lg border bg-bg-elevated px-3 py-2 ${
                data.selectedForEdit || selected ? 'border-brand' : 'border-border'
            }`}
            style={runBorder ? { borderColor: runBorder, borderWidth: 2 } : undefined}
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
            runStatus: undefined,
        },
    } as Node<FlowNodeData>;
}

export function CanvasPage({ workflowId: rawId }: { workflowId: string }) {
    const t = useT();
    // W3: `#/canvas/<id>?view=1` opens view-only (share/link without editing).
    const [workflowId, viewParam] = (() => {
        const [id, qs = ''] = rawId.split('?');
        return [id, new URLSearchParams(qs).get('view') === '1'];
    })();
    const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width: 767px)').matches);
    const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
    const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
    const [selectedId, setSelectedId] = useState('');
    const [name, setName] = useState('');
    const [saving, setSaving] = useState(false);
    const [running, setRunning] = useState(false);
    // issue #157: per-run 记忆开关 (默认开)
    const [memoryEnabled, setMemoryEnabled] = useState(true);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [loadPending, setLoadPending] = useState(!!workflowId);
    // issue #261: 画布内实时运行 — run 提交后轮询状态, 节点按终态着色, 闸门等待出横幅
    const [runId, setRunId] = useState('');
    const [runState, setRunState] = useState('');
    const [hitlPending, setHitlPending] = useState(false);
    const [publishedId, setPublishedId] = useState('');
    const [publishing, setPublishing] = useState(false);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const applyRunSnapshot = useCallback(
        (rec: { status: string; steps: { node_id: string; status: string }[]; hitl_pending?: boolean }) => {
            setRunState(rec.status);
            setHitlPending(!!rec.hitl_pending);
            const byNode = runStatusByNode(rec.steps);
            setNodes((ns) => ns.map((n) => ({ ...n, data: { ...n.data, runStatus: byNode[n.id] } })));
        },
        [setNodes]
    );

    useEffect(() => {
        if (!runId) return;
        const active = runState === 'running' || runState === 'pending';
        if (!active) {
            if (pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
            }
            return;
        }
        if (pollRef.current) return;
        pollRef.current = setInterval(() => {
            api.run(runId)
                .then((rec) => applyRunSnapshot(rec))
                .catch((e: Error) => console.error('[cowork-web] canvas run poll failed', e));
        }, 2000);
        return () => {
            if (pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
            }
        };
    }, [applyRunSnapshot, runId, runState]);

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

    const isReadonly = isMobile || viewParam;

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
                    runStatus: undefined,
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
                memory: memoryEnabled,
            });
            console.info(`[cowork-web] run started: ${res.run_id}`);
            // issue #261: 不再跳转 — 留在画布轮询, 节点实时着色; 运行视图入口见横幅
            setRunId(res.run_id);
            setRunState('running');
            sdk.toast.show({ level: 'success', message: t('已提交运行', 'Run submitted') });
        } catch (e) {
            console.error('[cowork-web] run failed', e);
            sdk.toast.show({ level: 'error', message: t('运行提交失败', 'Failed to submit run') });
        } finally {
            setRunning(false);
        }
    }

    // issue #261: 画布流程发布为命名模板 (custom)
    async function publish() {
        if (isReadonly || publishing) return;
        if (!name.trim()) {
            sdk.toast.show({ level: 'warning', message: t('请填写工作流名称', 'Workflow name required') });
            return;
        }
        setPublishing(true);
        const nodeDefs: CanvasNodeDef[] = nodes.map((n) => ({
            id: n.id,
            name: n.data.nodeType,
            category: n.data.category,
            position: n.position,
            config: { label: n.data.label, params: n.data.params },
        }));
        const edgeDefs: CanvasEdgeDef[] = edges.map((e) => ({ source_id: e.source, target_id: e.target }));
        try {
            const res = await api.publishTemplate({
                name: name.trim(),
                description: '',
                category: 'custom',
                workflow: { name: name.trim(), nodes: nodeDefs, edges: edgeDefs },
            });
            setPublishedId(res.id);
            sdk.toast.show({ level: 'success', message: t(`已发布模板: ${res.id}`, `Template published: ${res.id}`) });
        } catch (e) {
            console.error('[cowork-web] publish failed', e);
            sdk.toast.show({ level: 'error', message: t('发布失败', 'Publish failed') });
        } finally {
            setPublishing(false);
        }
    }

    // issue #261: 导出已发布模板 JSON (先发布再导出)
    async function exportPublished() {
        if (!publishedId) return;
        try {
            const url = await api.templateExport(publishedId);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${publishedId}.json`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            console.error('[cowork-web] export failed', e);
            sdk.toast.show({ level: 'error', message: t('导出失败', 'Export failed') });
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
    const fields = schemaFields(schemaProps);

    return (
        <div className="flex h-full min-h-0 flex-col">
            {isReadonly ? (
                <ReadOnlyBanner
                    text={
                        viewParam
                            ? t('查看模式 — 链接分享只读打开', 'View mode — shared link opens read-only')
                            : t('只读模式 — 画布编辑仅桌面端支持', 'Read-only — canvas editing is desktop-only')
                    }
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
                    <label
                        className="flex items-center gap-1 text-sm text-text-secondary"
                        title={t(
                            '运行结束后把产出写入 fusion-memory 长期记忆',
                            'Write run outcomes to fusion-memory after the run ends'
                        )}
                    >
                        <input
                            type="checkbox"
                            checked={memoryEnabled}
                            onChange={(e) => setMemoryEnabled(e.target.checked)}
                            disabled={isReadonly || running}
                        />
                        {t('记忆', 'Memory')}
                    </label>
                    <button
                        type="button"
                        className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                        disabled={isReadonly || running}
                        onClick={() => void run()}
                    >
                        {running ? t('提交中…', 'Submitting…') : t('运行', 'Run')}
                    </button>
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                        disabled={isReadonly || publishing}
                        onClick={() => void publish()}
                    >
                        {publishing ? t('发布中…', 'Publishing…') : t('发布模板', 'Publish template')}
                    </button>
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-1 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                        disabled={!publishedId}
                        title={publishedId ? publishedId : t('先发布模板后可导出', 'Publish a template first')}
                        onClick={() => void exportPublished()}
                    >
                        {t('导出', 'Export')}
                    </button>
                </div>
            </div>
            {runId ? (
                <div className="flex items-center gap-2 border-b border-border px-3 py-1.5 text-xs">
                    <span
                        className="h-2 w-2 rounded-full"
                        style={{ background: toneColor(runState === 'running' ? 'running' : runState) }}
                    />
                    <span className="text-text-secondary">
                        {t(`运行 ${runId}`, `Run ${runId}`)} · {runState}
                        {hitlPending ? ` · ${t('等待审批', 'awaiting approval')}` : ''}
                    </span>
                    <a className="ml-auto text-brand hover:underline" href={`#/runs/${runId}`}>
                        {t('打开运行视图', 'Open run view')}
                    </a>
                </div>
            ) : null}
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
                            {fields.length > 0
                                ? fields.map((f) => {
                                      const raw = sel.data.params[f.key];
                                      const kind = fieldKind(f.prop);
                                      return (
                                          <div key={f.key} className="space-y-0.5">
                                              <div className="flex items-center gap-1 text-[10px] text-text-secondary">
                                                  <span className="font-mono">{f.key}</span>
                                                  {f.required ? <span className="text-error">*</span> : null}
                                                  {f.prop.description ? (
                                                      <span
                                                          className="truncate text-text-muted"
                                                          title={f.prop.description}
                                                      >
                                                          — {f.prop.description}
                                                      </span>
                                                  ) : null}
                                              </div>
                                              {kind === 'checkbox' ? (
                                                  <input
                                                      type="checkbox"
                                                      checked={isTrueish(raw)}
                                                      disabled={isReadonly}
                                                      onChange={(e) =>
                                                          updateSelectedParam(
                                                              f.key,
                                                              e.target.checked ? 'true' : 'false'
                                                          )
                                                      }
                                                  />
                                              ) : kind === 'select' ? (
                                                  <select
                                                      className="w-full rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                                                      value={String(raw ?? '')}
                                                      disabled={isReadonly}
                                                      onChange={(e) => updateSelectedParam(f.key, e.target.value)}
                                                  >
                                                      <option value="">—</option>
                                                      {(f.prop.enum ?? []).map((opt) => (
                                                          <option key={String(opt)} value={String(opt)}>
                                                              {String(opt)}
                                                          </option>
                                                      ))}
                                                  </select>
                                              ) : kind === 'textarea' ? (
                                                  <textarea
                                                      className="min-h-16 w-full rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                                      value={
                                                          typeof raw === 'string'
                                                              ? raw
                                                              : raw == null
                                                                ? ''
                                                                : JSON.stringify(raw)
                                                      }
                                                      placeholder='["a","b"]'
                                                      disabled={isReadonly}
                                                      onChange={(e) => updateSelectedParam(f.key, e.target.value)}
                                                  />
                                              ) : (
                                                  <input
                                                      className="w-full rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                                      type={kind === 'number' ? 'number' : 'text'}
                                                      value={raw == null ? '' : String(raw)}
                                                      disabled={isReadonly}
                                                      onChange={(e) => updateSelectedParam(f.key, e.target.value)}
                                                  />
                                              )}
                                          </div>
                                      );
                                  })
                                : Object.entries(sel.data.params).map(([k, v]) => (
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
                            {fields.length > 0 ? (
                                <details className="pt-1">
                                    <summary className="cursor-pointer text-[10px] text-text-secondary">
                                        {t('额外参数 / JSON 形式', 'Extra / raw params')}
                                    </summary>
                                    <div className="mt-1 space-y-1">
                                        {Object.entries(sel.data.params)
                                            .filter(([k]) => !fields.some((f) => f.key === k))
                                            .map(([k, v]) => (
                                                <div key={k} className="flex items-center gap-1">
                                                    <input
                                                        className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                                        value={String(v)}
                                                        disabled={isReadonly}
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
                                </details>
                            ) : (
                                <ParamAdder onAdd={addParam} />
                            )}
                        </div>
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
