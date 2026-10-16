import { createPoller } from '@fusion/web-sdk';
import { DataTable, EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type RunListItem, api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

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

    useEffect(() => {
        if (data?.status === 'running' || data?.status === 'pending') {
            const timer = window.setTimeout(() => setReloadKey((k) => k + 1), POLL_MS);
            return () => window.clearTimeout(timer);
        }
    }, [data]);

    if (loading && !data) return <SkeletonLines rows={3} />;
    if (error)
        return (
            <ErrorState
                message={error.message}
                retryLabel={t('重试', 'Retry')}
                onRetry={() => setReloadKey((k) => k + 1)}
            />
        );
    if (!data) return null;

    return (
        <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
                <div className="font-mono text-sm text-text-primary">{data.run_id}</div>
                <StatusBadge status={statusKind(data.status) as StatusKind} label={data.status} />
            </div>
            {data.error ? <div className="mt-2 text-xs text-error">{data.error}</div> : null}
            {data.result?.summary ? (
                <div className="mt-2 text-xs text-text-secondary">{data.result.summary}</div>
            ) : null}
            <div className="mt-3 space-y-1">
                {(data.steps ?? []).map((s) => (
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
        </div>
    );
}
