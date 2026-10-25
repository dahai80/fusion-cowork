import { createPoller } from '@fusion/web-sdk';
import { DataTable, EmptyState, ErrorState, SkeletonLines, StatusBadge, StreamingMarkdown } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import {
    type FileReadResult,
    type GuardApproval,
    type RunArtifact,
    type RunListItem,
    type RunRecord,
    type RunReview,
    type RunStep,
    api,
    statusKind,
} from '../api';
import { useT } from '../i18n';
import { sdk, streamSse } from '../sdk';

const POLL_MS = 5000; // spec §6.3: monitoring poll 5s

function fmtTime(ts: number | null | undefined, lang: 'zh' | 'en'): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

// issue #187: HITL 超时倒计时 (服务端 hitl_deadline, 到点自动拒绝)
function HitlCountdown({ deadline }: { deadline: number }) {
    const [now, setNow] = useState(Math.floor(Date.now() / 1000));
    useEffect(() => {
        const timer = window.setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1000);
        return () => window.clearInterval(timer);
    }, []);
    const left = Math.max(0, deadline - now);
    const mm = Math.floor(left / 60);
    const ss = left % 60;
    return (
        <span className="font-mono text-[11px] text-warning">
            {mm}:{ss.toString().padStart(2, '0')}
        </span>
    );
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
                    {['running', 'pending', 'paused', 'completed', 'completed_with_denials', 'failed', 'cancelled'].map(
                        (s) => (
                            <option key={s} value={s}>
                                {s}
                            </option>
                        )
                    )}
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
    const [openStep, setOpenStep] = useState('');

    // issue #187: HITL — run 命中待审批节点时轮询审批卡 (approve once / approve node / deny)
    const [hitlApprovals, setHitlApprovals] = useState<GuardApproval[]>([]);
    const [hitlBusy, setHitlBusy] = useState('');

    async function hitlConfirm(actionId: string, approved: boolean, once: boolean) {
        setHitlBusy(actionId);
        try {
            await api.confirmApproval(actionId, approved, undefined, undefined, once);
            sdk.toast.show({
                level: 'success',
                message: approved
                    ? once
                        ? t('已批准 (本次)', 'Approved (once)')
                        : t('已批准并记住', 'Approved and remembered')
                    : t('已拒绝', 'Denied'),
            });
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] hitl confirm failed', e);
            sdk.toast.show({ level: 'error', message: t('审批失败', 'Approval failed') });
        } finally {
            setHitlBusy('');
        }
    }

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

    // issue #187: HITL 审批卡数据源 — hitl_pending 时轮询 pending 队列 (按 execution_id 过滤)
    useEffect(() => {
        if (!record?.hitl_pending) {
            setHitlApprovals([]);
            return;
        }
        let alive = true;
        const load = async () => {
            try {
                const res = await api.pendingApprovals();
                if (!alive) return;
                setHitlApprovals((res.approvals ?? []).filter((a) => a.execution_id === runId));
            } catch (e) {
                console.warn('[cowork-web] hitl pending approvals fetch failed', e);
            }
        };
        void load();
        const timer = window.setInterval(load, 2000);
        return () => {
            alive = false;
            window.clearInterval(timer);
        };
    }, [record?.hitl_pending, runId]);

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

    // #143: run 头部总 token — 瀑布各步聚合
    const totalTokens = (record.steps ?? []).reduce((n, s) => n + (s.tokens?.total_tokens ?? 0), 0);

    return (
        <div className="rounded-lg border border-border p-3">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <div className="font-mono text-sm text-text-primary">{record.run_id}</div>
                    {totalTokens > 0 ? (
                        <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
                            Σ {totalTokens} tok
                        </span>
                    ) : null}
                </div>
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
            {/* issue #187: HITL 审批卡 — 高风险节点暂停等待人工决定 */}
            {record.hitl_pending
                ? hitlApprovals.map((a) => (
                      <div key={a.action_id} className="mt-3 rounded-md border border-warning/60 bg-warning/5 p-2">
                          <div className="flex items-center justify-between text-xs">
                              <span className="font-medium text-text-primary">
                                  {t('等待审批', 'Awaiting approval')} — {a.node_id ?? a.tool_name}
                              </span>
                              {record.hitl_deadline ? <HitlCountdown deadline={record.hitl_deadline} /> : null}
                          </div>
                          <div className="mt-1 font-mono text-[11px] text-text-secondary">
                              {a.tool_name}({JSON.stringify(a.params ?? {})})
                          </div>
                          <div className="mt-2 flex items-center gap-2">
                              <button
                                  type="button"
                                  disabled={hitlBusy === a.action_id}
                                  className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10 disabled:opacity-50"
                                  onClick={() => void hitlConfirm(a.action_id, true, true)}
                              >
                                  {t('本次允许', 'Approve once')}
                              </button>
                              <button
                                  type="button"
                                  disabled={hitlBusy === a.action_id}
                                  className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                  onClick={() => void hitlConfirm(a.action_id, true, false)}
                              >
                                  {t('总是允许', 'Always allow')}
                              </button>
                              <button
                                  type="button"
                                  disabled={hitlBusy === a.action_id}
                                  className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10 disabled:opacity-50"
                                  onClick={() => void hitlConfirm(a.action_id, false, false)}
                              >
                                  {t('拒绝', 'Deny')}
                              </button>
                          </div>
                      </div>
                  ))
                : null}
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
                    {(record.steps ?? []).map((s) => {
                        const maxTime = Math.max(...(record.steps ?? []).map((x) => x.execution_time), 0.001);
                        const barPct = Math.max(4, Math.round((s.execution_time / maxTime) * 100));
                        const denied = s.status === 'denied';
                        return (
                            <div
                                key={s.node_id}
                                className={`rounded-md border bg-bg-elevated ${denied ? 'border-dashed border-warning' : 'border-border'}`}
                            >
                                <button
                                    type="button"
                                    className="flex w-full items-center justify-between px-2 py-1.5 text-left"
                                    onClick={() => setOpenStep(openStep === s.node_id ? '' : s.node_id)}
                                >
                                    <div className="min-w-0 flex-1">
                                        <div className="flex items-center gap-2">
                                            <span className="truncate text-xs text-text-primary">
                                                {s.node_display_name || s.node_name}
                                            </span>
                                            {denied ? (
                                                <span className="shrink-0 rounded border border-warning px-1 text-[10px] text-warning">
                                                    {t('被阻塞', 'Blocked')}
                                                </span>
                                            ) : null}
                                            <span className="truncate font-mono text-[10px] text-text-muted">
                                                {s.summary || s.error || `#${s.node_id}`}
                                            </span>
                                        </div>
                                        <div className="mt-1 h-1 w-full overflow-hidden rounded bg-bg-muted">
                                            <div
                                                className={`h-full rounded ${denied ? 'bg-warning' : statusKind(s.status) === 'error' ? 'bg-error' : statusKind(s.status) === 'success' ? 'bg-success' : 'bg-brand'}`}
                                                style={{ width: `${barPct}%` }}
                                            />
                                        </div>
                                    </div>
                                    <div className="ml-2 flex shrink-0 items-center gap-2">
                                        {s.tokens?.total_tokens ? (
                                            <span className="font-mono text-[10px] text-text-muted">
                                                {s.tokens.total_tokens} tok
                                            </span>
                                        ) : null}
                                        <span className="font-mono text-[10px] text-text-muted">
                                            {s.execution_time.toFixed(2)}s
                                        </span>
                                        <StatusBadge status={statusKind(s.status) as StatusKind} label={s.status} />
                                    </div>
                                </button>
                                {openStep === s.node_id ? <StepDrawer step={s} /> : null}
                            </div>
                        );
                    })}
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

// #138 G7/C2: 逐节点 waterfall 详情抽屉 — input/output/error 渐进披露
function StepDrawer({ step }: { step: RunStep }) {
    const t = useT();
    const fmt = (v: Record<string, unknown> | undefined) =>
        v && Object.keys(v).length > 0 ? JSON.stringify(v, null, 4) : '';
    const input = fmt(step.input_data);
    const output = fmt(step.output_data);
    return (
        <div className="border-t border-border px-2 py-2">
            {input ? (
                <div>
                    <div className="text-[10px] text-text-muted">{t('输入', 'Input')}</div>
                    <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-text-primary">
                        {input}
                    </pre>
                </div>
            ) : null}
            {output ? (
                <div className="mt-1.5">
                    <div className="text-[10px] text-text-muted">{t('输出', 'Output')}</div>
                    <pre className="mt-0.5 max-h-60 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-text-primary">
                        {output}
                    </pre>
                </div>
            ) : null}
            {step.error ? (
                <div className="mt-1.5">
                    <div className="text-[10px] text-error">{t('错误', 'Error')}</div>
                    <pre className="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-error">
                        {step.error}
                    </pre>
                </div>
            ) : null}
        </div>
    );
}

// #142: unified diff 逐行着色 (+ 绿 / - 红 / @@ 定位 / 头部行灰)
function DiffView({ content }: { content: string }) {
    const lines = content.split('\n').slice(0, 400);
    return (
        <pre className="max-h-72 overflow-auto font-mono text-[11px] leading-4">
            {lines.map((ln, i) => (
                <div
                    key={i}
                    className={
                        ln.startsWith('+++') || ln.startsWith('---') || ln.startsWith('diff ')
                            ? 'text-text-muted'
                            : ln.startsWith('@@')
                              ? 'text-accent'
                              : ln.startsWith('+')
                                ? 'bg-success/10 text-success'
                                : ln.startsWith('-')
                                  ? 'bg-error/10 text-error'
                                  : 'text-text-secondary'
                    }
                >
                    {ln || ' '}
                </div>
            ))}
        </pre>
    );
}

// #150: 引用溯源卡 — 点击 chip 拉取源文件内容 (沙箱内只读)
function CitationCards({ citations }: { citations: NonNullable<RunArtifact['citations']> }) {
    const t = useT();
    const [openPath, setOpenPath] = useState('');
    const [file, setFile] = useState<FileReadResult | null>(null);
    const [error, setError] = useState('');
    if (citations.length === 0) return null;

    async function open(path: string) {
        if (openPath === path && file) {
            setOpenPath('');
            setFile(null);
            return;
        }
        setOpenPath(path);
        setError('');
        try {
            setFile(await api.readFile(path));
        } catch (e) {
            console.error('[cowork-web] citation file read failed', e);
            setError(e instanceof Error ? e.message : String(e));
            setFile(null);
        }
    }

    return (
        <div className="mt-1 border-t border-border pt-1">
            <div className="mb-1 text-[10px] text-text-muted">
                {t('引用来源', 'Citations')} ({citations.length})
            </div>
            <div className="flex flex-wrap gap-1">
                {citations.map((c, i) => (
                    <button
                        key={`${c.path || c.message_id || i}`}
                        type="button"
                        title={c.path || c.message_id || ''}
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${
                            openPath === c.path
                                ? 'border-accent text-text-primary'
                                : 'border-border text-text-secondary hover:bg-bg-muted'
                        }`}
                        onClick={() => c.path && void open(c.path)}
                    >
                        📄 {c.name || c.path?.split('/').pop() || c.message_id}
                    </button>
                ))}
            </div>
            {error ? <div className="mt-1 text-[10px] text-error">{error}</div> : null}
            {openPath && file ? (
                <div className="mt-1 rounded border border-border bg-bg p-1">
                    <div className="mb-1 font-mono text-[10px] text-text-muted">
                        {file.path} [{file.line_start}-{file.line_end}/{file.total_lines}]
                    </div>
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] text-text-secondary">
                        {file.content}
                    </pre>
                </div>
            ) : null}
        </div>
    );
}

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
                            ) : selected.type === 'diff' ? (
                                // #142: 文件产物 unified diff 卡 — 编辑型步骤 / .diff/.patch 文件
                                <DiffView content={selected.content} />
                            ) : (
                                <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">
                                    {selected.content || selected.path}
                                </pre>
                            )}
                            {selected.citations && selected.citations.length > 0 ? (
                                <CitationCards citations={selected.citations} />
                            ) : null}
                        </div>
                    ) : null}
                </div>
            )}
        </div>
    );
}
