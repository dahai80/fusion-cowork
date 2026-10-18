import { createPoller } from '@fusion/web-sdk';
import { DataTable, EmptyState, ErrorState, SkeletonLines, StatusBadge, StreamingMarkdown } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type RunListItem, type RunRecord, type RunReview, api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk, streamSse } from '../sdk';

const POLL_MS = 5000; // spec §6.3: monitoring poll 5s

function fmtTime(ts: number | null | undefined, lang: 'zh' | 'en'): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

export function RunsPage() {
    const t = useT();
    const lang = sdk.i18n.lang();
    const [reloadKey, setReloadKey] = useState(0);
    const [selected, setSelected] = useState<string>('');
    const [statusFilter, setStatusFilter] = useState('');
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(() => api.runs(), [reloadKey]);

    // Live refresh while a run is active (poller pauses with tab hidden — SDK visibility-aware)
    useEffect(() => {
        const hasActive = (data?.runs ?? []).some((r) => r.status === 'running' || r.status === 'pending');
        if (!hasActive) return;
        const poller = createPoller({
            intervalMs: POLL_MS,
            onTick: async () => {
                setReloadKey((k) => k + 1);
            },
        });
        poller.start();
        return () => poller.stop();
    }, [data]);

    async function cancel(row: RunListItem) {
        try {
            await api.cancelRun(row.run_id);
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] cancel failed', e);
            sdk.toast.show({ level: 'error', message: t('取消失败', 'Cancel failed') });
        }
    }

    // W8: run redirection — pause holds at the next node checkpoint, resume continues
    async function pauseRun(row: RunListItem) {
        try {
            await api.pauseRun(row.run_id);
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] pause failed', e);
            sdk.toast.show({ level: 'error', message: t('暂停失败', 'Pause failed') });
        }
    }

    async function resumeRun(row: RunListItem) {
        try {
            await api.resumeRun(row.run_id);
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] resume failed', e);
            sdk.toast.show({ level: 'error', message: t('恢复失败', 'Resume failed') });
        }
    }

    if (loading && !data) return <SkeletonLines rows={5} />;
    if (error)
        return (
            <ErrorState
                message={error.message}
                retryLabel={t('重试', 'Retry')}
                onRetry={manualRetry}
                retrying={attempt > 0 ? attempt : undefined}
            />
        );

    // W4: client-side status filter over the runs list (all data already fetched)
    const rows = (data?.runs ?? []).filter((r) => !statusFilter || r.status === statusFilter);
    return (
        <div className="flex flex-col gap-4">
            <div className="flex items-end justify-between">
                <div>
                    <div className="text-lg font-medium text-text-primary">{t('运行记录', 'Runs')}</div>
                    <div className="text-xs text-text-muted">
                        {t('工作流运行状态与步骤结果', 'Workflow run status and step results')}
                    </div>
                </div>
                <select
                    className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                    value={statusFilter}
                    onChange={(e) => setStatusFilter(e.target.value)}
                >
                    <option value="">{t('全部状态', 'All statuses')}</option>
                    {['running', 'pending', 'paused', 'completed', 'failed', 'cancelled'].map((s) => (
                        <option key={s} value={s}>
                            {s}
                        </option>
                    ))}
                </select>
            </div>
            <DataTable<RunListItem>
                columns={[
                    {
                        key: 'run_id',
                        header: 'Run ID',
                        sortable: true,
                        render: (r) => <span className="font-mono text-xs">{r.run_id}</span>,
                    },
                    { key: 'workflow_name', header: t('工作流', 'Workflow'), sortable: true },
                    {
                        key: 'status',
                        header: t('状态', 'Status'),
                        sortable: true,
                        render: (r) => <StatusBadge status={statusKind(r.status) as StatusKind} label={r.status} />,
                    },
                    {
                        key: 'created_at',
                        header: t('开始时间', 'Started'),
                        sortable: true,
                        render: (r) => <span className="font-mono text-xs">{fmtTime(r.created_at, lang)}</span>,
                    },
                    {
                        key: 'actions',
                        header: '',
                        render: (r) => (
                            <div className="flex justify-end gap-2">
                                {r.status === 'running' ? (
                                    <button
                                        type="button"
                                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                        onClick={() => void pauseRun(r)}
                                    >
                                        {t('暂停', 'Pause')}
                                    </button>
                                ) : null}
                                {r.status === 'paused' ? (
                                    <button
                                        type="button"
                                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                        onClick={() => void resumeRun(r)}
                                    >
                                        {t('恢复', 'Resume')}
                                    </button>
                                ) : null}
                                {r.status === 'running' || r.status === 'pending' || r.status === 'paused' ? (
                                    <button
                                        type="button"
                                        className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10"
                                        onClick={() => void cancel(r)}
                                    >
                                        {t('停止', 'Stop')}
                                    </button>
                                ) : null}
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => setSelected(r.run_id === selected ? '' : r.run_id)}
                                >
                                    {selected === r.run_id ? t('收起', 'Collapse') : t('详情', 'Detail')}
                                </button>
                            </div>
                        ),
                    },
                ]}
                rows={rows}
                rowKey={(r) => r.run_id}
                emptyState={
                    <EmptyState
                        title={t('暂无运行', 'No runs yet')}
                        description={t(
                            '在工作流列表或画布中点击「运行」开始一次执行',
                            'Hit "Run" on the workflow list or canvas to start one'
                        )}
                    />
                }
            />
            {selected ? <RunDetail runId={selected} /> : null}
        </div>
    );
}

function RunDetail({ runId }: { runId: string }) {
    const t = useT();
    const [reloadKey, setReloadKey] = useState(0);
    const { data, error, loading } = useAutoRetry(() => api.run(runId), [runId, reloadKey]);
    const [live, setLive] = useState<RunRecord | null>(null);
    const [sseOk, setSseOk] = useState(false);
    const [action, setAction] = useState<'set_params' | 'insert_node' | 'skip_remaining'>('set_params');
    const [nodeId, setNodeId] = useState('');
    const [payload, setPayload] = useState('{}');
    const [sending, setSending] = useState(false);
    const [reviewNote, setReviewNote] = useState('');

    // W9: 交付审查 — pending_review → approved/rejected (节点级 guard 审批另属 W19)
    async function reviewRun(approved: boolean) {
        try {
            await api.reviewRun(runId, approved, reviewNote);
            sdk.toast.show({
                level: 'success',
                message: approved ? t('审查通过', 'Approved') : t('已退回', 'Rejected'),
            });
            setReviewNote('');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] run review failed', e);
            sdk.toast.show({ level: 'error', message: t('审查失败', 'Review failed') });
        }
    }

    // W8/B1: 暂停态改向 — 换参 / 插节点 / 跳过剩余, 在下一节点边界生效
    async function sendDirective() {
        let parsed: Record<string, unknown> = {};
        const raw = payload.trim();
        if (raw && raw !== '{}') {
            try {
                parsed = JSON.parse(raw) as Record<string, unknown>;
            } catch {
                sdk.toast.show({ level: 'error', message: t('JSON 解析失败', 'Invalid JSON') });
                return;
            }
        }
        const body: Parameters<typeof api.directive>[1] = { action };
        if (action === 'set_params') {
            body.node_id = nodeId || undefined;
            body.params = parsed;
        } else if (action === 'insert_node') {
            body.node = parsed;
            body.after_node_id = nodeId || undefined;
        }
        setSending(true);
        try {
            await api.directive(runId, body);
            sdk.toast.show({ level: 'success', message: t('指令已排队', 'Directive queued') });
            setPayload('{}');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] directive failed', e);
            sdk.toast.show({ level: 'error', message: t('指令发送失败', 'Directive failed') });
        } finally {
            setSending(false);
        }
    }

    // W13/B2: 步骤日志 SSE 实时推送; 连接失败时回落到 5s 轮询
    useEffect(() => {
        const ac = new AbortController();
        setLive(null);
        setSseOk(false);
        streamSse(
            `/api/runs/${encodeURIComponent(runId)}/events`,
            (event, payload) => {
                if (event === 'ping') return;
                const run = (payload as { run?: RunRecord } | null)?.run;
                if (run) {
                    setLive(run);
                    setSseOk(true);
                }
            },
            ac.signal
        )
            .then(() => setSseOk(false))
            .catch((e) => {
                if (!ac.signal.aborted) {
                    console.warn('[cowork-web] run SSE unavailable, falling back to polling', e);
                    setSseOk(false);
                }
            });
        return () => ac.abort();
    }, [runId]);

    const record = live ?? data;

    useEffect(() => {
        if (sseOk) return;
        if (record?.status === 'running' || record?.status === 'pending') {
            const timer = window.setTimeout(() => setReloadKey((k) => k + 1), POLL_MS);
            return () => window.clearTimeout(timer);
        }
    }, [record, sseOk]);

    if (loading && !record) return <SkeletonLines rows={3} />;
    if (error && !record)
        return (
            <ErrorState
                message={error.message}
                retryLabel={t('重试', 'Retry')}
                onRetry={() => setReloadKey((k) => k + 1)}
            />
        );
    if (!record) return null;

    return (
        <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
                <div className="font-mono text-sm text-text-primary">{record.run_id}</div>
                <StatusBadge status={statusKind(record.status) as StatusKind} label={record.status} />
            </div>
            {record.error ? <div className="mt-2 text-xs text-error">{record.error}</div> : null}
            {record.result?.summary ? (
                <div className="mt-2 text-xs text-text-secondary">{record.result.summary}</div>
            ) : null}
            {record.review ? (
                <div className="mt-2 rounded-md border border-border p-2">
                    <div className="flex items-center justify-between text-xs">
                        <span className="text-text-primary">{t('交付审查', 'Delivery review')}</span>
                        <StatusBadge
                            status={
                                record.review.status === 'approved'
                                    ? 'success'
                                    : record.review.status === 'rejected'
                                      ? 'error'
                                      : 'warning'
                            }
                            label={
                                record.review.status === 'approved'
                                    ? t('已通过', 'Approved')
                                    : record.review.status === 'rejected'
                                      ? t('已退回', 'Rejected')
                                      : t('待审查', 'Pending review')
                            }
                        />
                    </div>
                    {record.review.note ? (
                        <div className="mt-1 text-xs text-text-secondary">{record.review.note}</div>
                    ) : null}
                    {record.review.status === 'pending_review' ? (
                        <div className="mt-2 flex items-center gap-2">
                            <input
                                className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                                placeholder={t('审查意见 (可选)', 'Review note (optional)')}
                                value={reviewNote}
                                onChange={(e) => setReviewNote(e.target.value)}
                            />
                            <button
                                type="button"
                                className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10"
                                onClick={() => void reviewRun(true)}
                            >
                                {t('通过交付', 'Approve')}
                            </button>
                            <button
                                type="button"
                                className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10"
                                onClick={() => void reviewRun(false)}
                            >
                                {t('退回', 'Reject')}
                            </button>
                        </div>
                    ) : null}
                </div>
            ) : null}
            {record.status === 'running' || record.status === 'paused' ? (
                <div className="mt-3 rounded-md border border-border p-2">
                    <div className="mb-2 text-xs font-medium text-text-primary">{t('运行改向', 'Run directive')}</div>
                    <div className="flex flex-wrap items-center gap-2">
                        <select
                            className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                            value={action}
                            onChange={(e) => setAction(e.target.value as typeof action)}
                        >
                            <option value="set_params">{t('换参数', 'Set params')}</option>
                            <option value="insert_node">{t('插节点', 'Insert node')}</option>
                            <option value="skip_remaining">{t('跳过剩余', 'Skip remaining')}</option>
                        </select>
                        {action !== 'skip_remaining' ? (
                            <input
                                className="w-40 rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                placeholder={
                                    action === 'set_params'
                                        ? t('节点 ID (空=下一节点)', 'Node ID (empty = next)')
                                        : t('源节点 ID (可选)', 'After node ID (optional)')
                                }
                                value={nodeId}
                                onChange={(e) => setNodeId(e.target.value)}
                            />
                        ) : null}
                        {action !== 'skip_remaining' ? (
                            <input
                                className="min-w-[240px] flex-1 rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-primary"
                                placeholder={
                                    action === 'set_params'
                                        ? t('参数 JSON', 'Params JSON')
                                        : t('节点 JSON: {"name": "...", "params": {}}', 'Node JSON')
                                }
                                value={payload}
                                onChange={(e) => setPayload(e.target.value)}
                            />
                        ) : null}
                        <button
                            type="button"
                            disabled={sending}
                            className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                            onClick={() => void sendDirective()}
                        >
                            {sending ? t('发送中…', 'Sending…') : t('发送', 'Send')}
                        </button>
                    </div>
                </div>
            ) : null}
            <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
                <div className="space-y-1">
                    {(record.steps ?? []).map((s) => (
                        <div
                            key={s.node_id}
                            className="flex items-center justify-between rounded-md border border-border px-2 py-1.5"
                        >
                            <div className="min-w-0">
                                <div className="truncate text-xs text-text-primary">
                                    {s.node_display_name || s.node_name}
                                </div>
                                <div className="truncate font-mono text-[10px] text-text-muted">
                                    {s.summary || s.error || `#${s.node_id}`}
                                </div>
                            </div>
                            <div className="flex shrink-0 items-center gap-2">
                                <span className="font-mono text-[10px] text-text-muted">
                                    {s.execution_time.toFixed(2)}s
                                </span>
                                <StatusBadge status={statusKind(s.status) as StatusKind} label={s.status} />
                            </div>
                        </div>
                    ))}
                </div>
                <RunArtifactPane
                    runId={runId}
                    active={record.status === 'running' || record.status === 'pending'}
                    reviewStatus={record.review?.status ?? null}
                />
            </div>
        </div>
    );
}

// W10/B5: Runs 右栏产物预览 — 复用 Space 预览 (StreamingMarkdown), 审查即交付入口
function RunArtifactPane({
    runId,
    active,
    reviewStatus,
}: {
    runId: string;
    active: boolean;
    reviewStatus: RunReview['status'] | null;
}) {
    const t = useT();
    const [reloadKey, setReloadKey] = useState(0);
    const [selectedId, setSelectedId] = useState('');
    const { data, error } = useAutoRetry(() => api.runArtifacts(runId), [runId, reloadKey]);

    useEffect(() => {
        if (!active) return;
        const timer = window.setInterval(() => setReloadKey((k) => k + 1), POLL_MS);
        return () => window.clearInterval(timer);
    }, [active]);

    const artifacts = data?.artifacts ?? [];
    const selected = artifacts.find((a) => a.artifact_id === (selectedId || artifacts[0]?.artifact_id));
    if (error)
        return (
            <div className="rounded-md border border-border p-2">
                <ErrorState
                    message={error.message}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={() => setReloadKey((k) => k + 1)}
                />
            </div>
        );

    return (
        <div className="rounded-md border border-border p-2">
            <div className="mb-2 flex items-center justify-between">
                <div className="text-xs font-medium text-text-primary">{t('产出预览', 'Artifacts')}</div>
                {reviewStatus ? (
                    <span className="text-[10px] text-text-muted">
                        {reviewStatus === 'pending_review'
                            ? t('待审查 — 通过后才算交付', 'Pending review — approve to deliver')
                            : reviewStatus === 'approved'
                              ? t('已交付', 'Delivered')
                              : t('已退回', 'Rejected')}
                    </span>
                ) : null}
            </div>
            {artifacts.length === 0 ? (
                <EmptyState title={t('暂无产出', 'No artifacts yet')} />
            ) : (
                <div className="flex flex-col gap-2">
                    <div className="flex flex-wrap gap-1">
                        {artifacts.map((a) => (
                            <button
                                key={a.artifact_id}
                                type="button"
                                className={`rounded border px-1.5 py-0.5 text-[10px] ${
                                    selected?.artifact_id === a.artifact_id
                                        ? 'border-accent text-text-primary'
                                        : 'border-border text-text-secondary hover:bg-bg-muted'
                                }`}
                                onClick={() => setSelectedId(a.artifact_id)}
                            >
                                {a.name}
                            </button>
                        ))}
                    </div>
                    {selected ? (
                        <div className="rounded border border-border bg-bg p-2">
                            <div className="mb-1 flex items-center justify-between text-[10px] text-text-muted">
                                <span className="font-mono">{selected.path || selected.artifact_id}</span>
                                {selected.truncated ? <span>{t('已截断', 'truncated')}</span> : null}
                            </div>
                            {selected.type === 'markdown' ? (
                                <StreamingMarkdown
                                    text={selected.content}
                                    className="max-h-72 overflow-auto text-xs text-text-secondary"
                                />
                            ) : (
                                <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">
                                    {selected.content || selected.path}
                                </pre>
                            )}
                        </div>
                    ) : null}
                </div>
            )}
        </div>
    );
}
