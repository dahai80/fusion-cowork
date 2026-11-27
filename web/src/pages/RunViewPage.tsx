import { useAutoRetry } from '@fusion/web-ui';
import {
    Composer,
    type ComposerActionItem,
    type ComposerChip,
    EmptyState,
    ErrorState,
    SkeletonLines,
    StatusBadge,
} from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import {
    type ArtifactCardData,
    type GuardApproval,
    type RunRecord,
    type RunStep,
    type SpaceArtifact,
    api,
    statusKind,
} from '../api';
import { GateProgress, gateProgress } from '../components/GateProgress';
import {
    type RefSource,
    buildContextChips,
    buildReferenceItems,
    composerApprovalVariant,
    insertRefToken,
} from '../composer';
import { useT } from '../i18n';
import { isPipelineRun } from '../pipeline';
import { runHistoryActions } from '../runstatus';
import {
    applyMatchOverrides,
    capabilitiesInUse,
    computeWaves,
    matchVerdictView,
    parseParamsDraft,
    stepArtifactRefs,
    stepCapability,
} from '../runview';
import type { MatchVerdictView } from '../runview';
import { sdk, streamSse } from '../sdk';
import { HitlCountdown, RunArtifactPane, StepDrawer } from './RunsPage';

// issue #224: unified run view (#/runs/<id>) — one screen to watch and steer
// a run: wave-grouped timeline (parallel branches side-by-side, M4), user
// correction lines from #227 inject markers, capability annotations (M10),
// interrupt composer + pause (M5), workspace drawer (M6), capabilities-in-use
// rail, dismissible first-run hint. RunsPage keeps the list + inline detail.

const POLL_MS = 5000;
const HINT_KEY = 'cowork.runview.hintDismissed';

export function RunViewPage({ runId }: { runId: string }) {
    const t = useT();
    const [reloadKey, setReloadKey] = useState(0);
    const { data, error, loading } = useAutoRetry(() => api.run(runId), [runId, reloadKey]);
    const [live, setLive] = useState<RunRecord | null>(null);
    const [sseOk, setSseOk] = useState(false);
    // issue #240 R1: @ reference panel covers directory/task/artifact refs — same three source kinds as the home page
    const workspacesRes = useAutoRetry(() => api.workspaces(), []);
    const recentRunsRes = useAutoRetry(() => api.runs(), []);
    const [openStep, setOpenStep] = useState('');
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [hintDismissed, setHintDismissed] = useState(true);
    const [instruction, setInstruction] = useState('');
    const [sending, setSending] = useState(false);
    // issue #187/#224: HITL approval cards + delivery review closed-loop inside the run view (UC-2 interrupt full chain)
    const [hitlApprovals, setHitlApprovals] = useState<GuardApproval[]>([]);
    const [hitlBusy, setHitlBusy] = useState('');
    const [reviewNote, setReviewNote] = useState('');
    // issue #422 F-1: historical project match verdict card — re-verdict button submits an override_output redirect directive
    const [matchBusy, setMatchBusy] = useState('');

    useEffect(() => {
        try {
            setHintDismissed(localStorage.getItem(HINT_KEY) === '1');
        } catch {
            setHintDismissed(false);
        }
    }, []);

    function dismissHint() {
        setHintDismissed(true);
        try {
            localStorage.setItem(HINT_KEY, '1');
        } catch {
            // storage unavailable — in-session dismiss only
        }
    }

    // W13/B2: step SSE live push, falls back to polling on failure (same data source as RunsPage)
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
                    console.warn('[cowork-web] run view SSE unavailable, falling back to polling', e);
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

    // issue #187: poll approval cards while HITL is pending (filtered by execution_id), 2s
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

    if (loading && !record) return <SkeletonLines rows={6} />;
    if (error && !record)
        return (
            <ErrorState
                message={error.message}
                // i18n: user-facing
                retryLabel={t('重试', 'Retry')}
                onRetry={() => setReloadKey((k) => k + 1)}
            />
        );
    if (!record) return null;

    const steerable = record.status === 'running' || record.status === 'paused';
    // issue #226: terminal runs are read-only — resume/derive actions only in terminal states (set aligned with WorkflowStatus)
    const historyActions = runHistoryActions(record.status);
    const totalTokens = (record.steps ?? []).reduce((n, s) => n + (s.tokens?.total_tokens ?? 0), 0);
    const waves = computeWaves(record.workflow_def ?? {});
    // issue #303: gate progress bar + blocked→terminal same-node step merged state machine
    const gates = gateProgress(record.workflow_def, record.steps ?? []);
    const caps = capabilitiesInUse(record.steps ?? []);
    const correctionLines = (record.steps ?? []).filter((s) => s.user_correction);
    // issue #422 F-1: re-verdict directive echo — override_output payload overrides the verdict card display (engine boundary semantics)
    const matchOverrides = new Map<string, Record<string, unknown>>();
    for (const d of record.directives ?? []) {
        const payload = d.payload as { node_id?: unknown; merge?: unknown } | undefined;
        if (
            d.action === 'override_output' &&
            payload &&
            typeof payload.node_id === 'string' &&
            payload.merge &&
            typeof payload.merge === 'object'
        ) {
            matchOverrides.set(payload.node_id, payload.merge as Record<string, unknown>);
        }
    }
    // issue #225: approval cards inline at the gated node position; unlocatable cards float at the top (fallback)
    const waveNodeIds = new Set(waves.flatMap((w) => w.nodes.map((n) => n.id)));
    const inlineApprovals = hitlApprovals.filter((a) => a.node_id && waveNodeIds.has(a.node_id));
    const floatingApprovals = hitlApprovals.filter((a) => !a.node_id || !waveNodeIds.has(a.node_id));

    // issue #231 (FCS-1 §6): approval-waiting variant — context bar badge + composer disabled, no separate fallback UI
    const approvalVariant = composerApprovalVariant(!!record.hitl_pending);
    const runContextChips: ComposerChip[] = buildContextChips({
        waitingApproval: approvalVariant.waitingApproval,
        // i18n: user-facing
        waitingLabel: t('等待审批', 'Awaiting approval'),
    });
    // issue #240 R1: @ reference panel — directory/task/artifact kinds (same as home), artifacts from this run's steps
    function insertRunRef(token: string) {
        setInstruction((p) => insertRefToken(p, token));
    }
    const runReferenceItems: ComposerActionItem[] = buildReferenceItems(
        (() => {
            const seen = new Set<string>();
            const sources: RefSource[] = [];
            for (const w of workspacesRes.data?.workspaces ?? []) {
                if (seen.has(`directory:${w.id}`)) continue;
                seen.add(`directory:${w.id}`);
                sources.push({ kind: 'directory', id: w.id, name: w.name });
            }
            for (const r of (recentRunsRes.data?.runs ?? []).slice(0, 8)) {
                if (seen.has(`task:${r.run_id}`)) continue;
                seen.add(`task:${r.run_id}`);
                sources.push({
                    kind: 'task',
                    id: r.run_id,
                    name: r.workflow_name || r.run_id.slice(0, 8),
                });
            }
            for (const s of record.steps ?? []) {
                for (const r of stepArtifactRefs(s)) {
                    if (seen.has(`artifact:${r.artifact_id}`)) continue;
                    seen.add(`artifact:${r.artifact_id}`);
                    sources.push({ kind: 'artifact', id: r.artifact_id, name: r.name || r.artifact_id });
                }
            }
            return sources;
        })(),
        insertRunRef
    );

    async function steer(fn: () => Promise<unknown>, failMsg: string) {
        try {
            await fn();
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] steer failed', e);
            sdk.toast.show({ level: 'error', message: failMsg });
        }
    }

    async function hitlConfirm(
        actionId: string,
        approved: boolean,
        once: boolean,
        editedParams?: Record<string, unknown>,
        comment?: string
    ) {
        setHitlBusy(actionId);
        try {
            await api.confirmApproval(actionId, approved, undefined, undefined, once, editedParams, comment);
            sdk.toast.show({
                level: 'success',
                message: approved
                    ? once
                        ? editedParams
                            // i18n: user-facing
                            ? t('已批准并应用编辑参数', 'Approved with edited params')
                            // i18n: user-facing
                            : t('已批准 (本次)', 'Approved (once)')
                        // i18n: user-facing
                        : t('已批准并记住', 'Approved and remembered')
                    // i18n: user-facing
                    : t('已拒绝', 'Denied'),
            });
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] hitl confirm failed', e);
            // i18n: user-facing
            sdk.toast.show({ level: 'error', message: t('审批失败', 'Approval failed') });
        } finally {
            setHitlBusy('');
        }
    }

    // issue #422 F-1: re-verdict — extend/base verdict flips to new (downstream interpolation takes the new value);
    // new cannot flip back to extend (the card has no candidate project to attach) and base→extend violates the cross-customer red line — neither is offered
    async function reverdictMatch(nodeId: string) {
        setMatchBusy(nodeId);
        try {
            await api.directive(runId, {
                action: 'override_output',
                node_id: nodeId,
                merge: { verdict: 'new', matched_repo: '' },
            });
            sdk.toast.show({
                level: 'success',
                // i18n: user-facing
                message: t('已改判: 新建项目', 'Re-verdicted: new project'),
            });
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] re-verdict failed', e);
            // i18n: user-facing
            sdk.toast.show({ level: 'error', message: t('改判失败', 'Re-verdict failed') });
        } finally {
            setMatchBusy('');
        }
    }

    // W9: delivery review — pending_review → approved/rejected
    async function reviewRun(approved: boolean) {
        try {
            await api.reviewRun(runId, approved, reviewNote);
            sdk.toast.show({
                level: 'success',
                // i18n: user-facing
                message: approved ? t('审查通过', 'Approved') : t('已退回', 'Rejected'),
            });
            setReviewNote('');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] run review failed', e);
            // i18n: user-facing
            sdk.toast.show({ level: 'error', message: t('审查失败', 'Review failed') });
        }
    }

    // issue #226: resume from a step — the new run chains back to the original run, jump to the new run view
    async function resumeFromStep(nodeId: string) {
        try {
            const r = await api.resumeRunFrom(runId, nodeId);
            sdk.toast.show({
                level: 'success',
                message: t(
                    // i18n: user-facing
                    `已从该步骤重跑 (${r.snapshot_steps} 步快照续跑)`,
                    `Resumed (${r.snapshot_steps} steps replayed)`
                ),
            });
            window.location.hash = `#/runs/${r.run_id}`;
        } catch (e) {
            console.error('[cowork-web] resume-from failed', e);
            // i18n: user-facing
            sdk.toast.show({ level: 'error', message: t('重跑失败', 'Resume failed') });
        }
    }

    // issue #226: derive new task — prefill the home composer (task + last result context)
    function deriveNewTask() {
        if (!record) return;
        try {
            localStorage.setItem(
                'cowork.derivePrefill',
                JSON.stringify({
                    task: record.workflow_name || record.workflow_id || '',
                    outcome: record.result?.summary || '',
                    run_id: runId,
                })
            );
        } catch {
            // storage unavailable — go straight home without prefill
        }
        window.location.hash = '#/home';
    }

    async function sendInstruction() {
        const text = instruction.trim();
        if (!text || sending) return;
        setSending(true);
        try {
            await api.directive(runId, { action: 'inject', instruction: text });
            sdk.toast.show({
                level: 'success',
                // i18n: user-facing
                message: t('指令已排队，下一节点边界生效', 'Queued — lands at the next node boundary'),
            });
            setInstruction('');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] inject failed', e);
            // i18n: user-facing
            sdk.toast.show({ level: 'error', message: t('指令发送失败', 'Failed to send') });
        } finally {
            setSending(false);
        }
    }

    return (
        <div className="mx-auto flex h-full max-w-6xl flex-col gap-3">
            {/* Header: back + run identity + controls */}
            <div className="flex flex-wrap items-center gap-2">
                <button
                    type="button"
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    onClick={() => {
                        window.location.hash = '#/runs';
                    }}
                >
                    {/* i18n: user-facing */}
                    ← {t('运行列表', 'Runs')}
                </button>
                <span className="font-mono text-sm text-text-primary">{record.run_id}</span>
                <span className="text-xs text-text-secondary">{record.workflow_name || record.workflow_id}</span>
                <StatusBadge status={statusKind(record.status) as StatusKind} label={record.status} />
                <GateProgress gates={gates} />
                {totalTokens > 0 ? (
                    <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-muted">
                        Σ {totalTokens} tok
                    </span>
                ) : null}
                {correctionLines.length > 0 ? (
                    <span className="rounded border border-accent px-1.5 py-0.5 text-[10px] text-accent">
                        {/* i18n: user-facing */}
                        {t(`已修正 ×${correctionLines.length}`, `${correctionLines.length} corrections`)}
                    </span>
                ) : null}
                <div className="flex-1" />
                {record.status === 'running' ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        // i18n: user-facing
                        onClick={() => void steer(() => api.pauseRun(runId), t('暂停失败', 'Pause failed'))}
                    >
                        {/* i18n: user-facing */}
                        ⏸ {t('暂停', 'Pause')}
                    </button>
                ) : null}
                {record.status === 'paused' ? (
                    <button
                        type="button"
                        className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10"
                        // i18n: user-facing
                        onClick={() => void steer(() => api.resumeRun(runId), t('恢复失败', 'Resume failed'))}
                    >
                        {/* i18n: user-facing */}
                        ▶ {t('继续', 'Resume')}
                    </button>
                ) : null}
                {record.space_id ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => setDrawerOpen(!drawerOpen)}
                    >
                        {/* i18n: user-facing */}
                        📁 {t('工作区', 'Workspace')}
                    </button>
                ) : null}
                {isPipelineRun(record.workflow_name) ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => {
                            window.location.hash = `#/pipeline/${runId}`;
                        }}
                    >
                        {/* i18n: user-facing */}
                        ⇶ {t('管线视图', 'Pipeline view')}
                    </button>
                ) : null}
                {historyActions.deriveNewTask ? (
                    <button
                        type="button"
                        className="rounded-md border border-accent px-2 py-1 text-xs text-accent hover:bg-accent/10"
                        onClick={deriveNewTask}
                    >
                        {/* i18n: user-facing */}
                        ✚ {t('派生新任务', 'Derive new task')}
                    </button>
                ) : null}
                {record.labels?.artifact_ref ? (
                    <span
                        title={record.labels.artifact_ref}
                        className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-secondary"
                    >
                        {/* i18n: user-facing */}
                        ▦ {t('来自 Artifacts', 'From Artifacts')} ·{' '}
                        {record.labels.artifact_name || record.labels.artifact_ref.slice(0, 12)}
                    </span>
                ) : null}
                {record.labels?.resume_of ? (
                    <button
                        type="button"
                        title={record.labels.resume_of}
                        className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-text-muted hover:text-text-primary"
                        onClick={() => {
                            window.location.hash = `#/runs/${record.labels?.resume_of}`;
                        }}
                    >
                        {/* i18n: user-facing */}
                        ↻ {t('重跑自', 'resumed from')} {record.labels.resume_of.slice(0, 12)}
                    </button>
                ) : null}
            </div>
            {record.error ? <div className="text-xs text-error">{record.error}</div> : null}
            {record.failure_attribution?.handoff_candidates?.length ? (
                <div className="rounded-md border border-border bg-bg-surface px-3 py-2 text-xs">
                    <div className="text-[10px] font-medium text-text-muted">
                        {t(
                            // i18n: user-facing
                            '失败归因 · 边级候选 (kernel 确定性启发)',
                            'Failure attribution · handoff candidates (kernel heuristics)'
                        )}
                    </div>
                    <ul className="mt-1 space-y-0.5">
                        {record.failure_attribution.handoff_candidates.slice(0, 5).map((c, i) => (
                            <li key={i} className="text-text-secondary">
                                <span className="font-mono text-text-primary">{c.from_node}</span>
                                {' → '}
                                <span className="font-mono text-error">{c.to_node}</span>
                                <span className="text-text-muted"> · {c.reason}</span>
                            </li>
                        ))}
                    </ul>
                </div>
            ) : null}
            {record.result?.summary ? <div className="text-xs text-text-secondary">{record.result.summary}</div> : null}

            {/* First-run hint (dismissible) */}
            {!hintDismissed ? (
                <div className="flex items-center justify-between rounded-md border border-accent/50 bg-accent/5 px-3 py-1.5 text-xs text-text-secondary">
                    <span>
                        {/* i18n: user-facing */}
                        {t('每一步可见 · 随时可追加指令或打断', 'Every step visible · redirect or interrupt any time')}
                    </span>
                    <button type="button" className="text-text-muted hover:text-text-primary" onClick={dismissHint}>
                        ✕
                    </button>
                </div>
            ) : null}

            {/* issue #187/#225: HITL approval cards — inline at the gated step position, unlocatable ones float at the top */}
            {record.hitl_pending
                ? floatingApprovals.map((a) => (
                      <InlineApprovalCard
                          key={a.action_id}
                          approval={a}
                          deadline={record.hitl_deadline ?? undefined}
                          busy={hitlBusy === a.action_id}
                          onConfirm={hitlConfirm}
                      />
                  ))
                : null}

            {/* W9: delivery review — approve/reject pending_review inside the run view */}
            {record.review?.status === 'pending_review' ? (
                <div className="rounded-md border border-border p-2">
                    <div className="flex items-center gap-2">
                        <input
                            className="flex-1 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                            // i18n: user-facing
                            placeholder={t('审查意见 (可选)', 'Review note (optional)')}
                            value={reviewNote}
                            onChange={(e) => setReviewNote(e.target.value)}
                        />
                        <button
                            type="button"
                            className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10"
                            onClick={() => void reviewRun(true)}
                        >
                            {/* i18n: user-facing */}
                            {t('通过交付', 'Approve')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10"
                            onClick={() => void reviewRun(false)}
                        >
                            {/* i18n: user-facing */}
                            {t('退回', 'Reject')}
                        </button>
                    </div>
                </div>
            ) : null}

            {/* Body: timeline + right rail (capabilities in use / workspace drawer) */}
            <div className="flex min-h-0 flex-1 gap-3">
                <div className="min-w-0 flex-1 space-y-3 overflow-y-auto pr-1">
                    {waves.length === 0 ? (
                        // i18n: user-facing
                        <EmptyState title={t('暂无步骤', 'No steps yet')} />
                    ) : (
                        waves.map((wave) => (
                            <div key={wave.depth}>
                                <div className="pb-1 text-[10px] font-medium uppercase tracking-wide text-text-muted">
                                    {t(
                                        wave.nodes.length > 1
                                            // i18n: user-facing
                                            ? `并行 ${wave.nodes.length} 路`
                                            // i18n: user-facing
                                            : `阶段 ${wave.depth + 1}`,
                                        wave.nodes.length > 1
                                            ? `parallel ×${wave.nodes.length}`
                                            : `stage ${wave.depth + 1}`
                                    )}
                                </div>
                                <div
                                    className="grid gap-2"
                                    style={{ gridTemplateColumns: `repeat(${wave.nodes.length}, minmax(0, 1fr))` }}
                                >
                                    {wave.nodes.map((wn) => {
                                        // issue #303: same-node blocked→terminal merged into one state-machine row
                                        // (queued→waiting→passed/failed) — gate nodes no longer render two rows
                                        const nodeSteps = (record.steps ?? []).filter(
                                            (s) => s.node_id === wn.id && !s.user_correction
                                        );
                                        const blockedStep = nodeSteps.find((s) => s.status === 'blocked');
                                        const terminalStep = nodeSteps.find((s) => s.status !== 'blocked');
                                        const step = terminalStep ?? blockedStep;
                                        // issue #225: approval card inline at the paused step position (M7 badge)
                                        const approval = record.hitl_pending
                                            ? inlineApprovals.find((a) => a.node_id === wn.id)
                                            : undefined;
                                        if (approval) {
                                            return (
                                                <InlineApprovalCard
                                                    key={wn.id}
                                                    approval={approval}
                                                    deadline={record.hitl_deadline ?? undefined}
                                                    busy={hitlBusy === approval.action_id}
                                                    onConfirm={hitlConfirm}
                                                />
                                            );
                                        }
                                        if (!step) {
                                            return (
                                                <div
                                                    key={wn.id}
                                                    className="rounded-md border border-dashed border-border p-2 text-[11px] text-text-muted"
                                                >
                                                    {/* i18n: user-facing */}
                                                    {wn.label || wn.name} · {t('等待', 'pending')}
                                                </div>
                                            );
                                        }
                                        // issue #303: waiting on approval with no inline card → waiting row (countdown + approvals entry)
                                        if (step.status === 'blocked' && record.hitl_pending && !approval) {
                                            return (
                                                <WaitingGateCard
                                                    key={wn.id}
                                                    step={step}
                                                    deadline={record.hitl_deadline ?? undefined}
                                                />
                                            );
                                        }
                                        // issue #422 F-1: historical project match step renders as a verdict card
                                        const matchView = applyMatchOverrides(
                                            matchVerdictView(step),
                                            matchOverrides.get(wn.id)
                                        );
                                        if (matchView) {
                                            return (
                                                <div key={wn.id}>
                                                    <MatchVerdictCard
                                                        view={matchView}
                                                        steerable={steerable}
                                                        busy={matchBusy === wn.id}
                                                        onReverdict={() => void reverdictMatch(wn.id)}
                                                    />
                                                </div>
                                            );
                                        }
                                        return (
                                            <div key={wn.id}>
                                                <StepCard
                                                    step={step}
                                                    waited={Boolean(blockedStep && terminalStep)}
                                                    expanded={openStep === wn.id}
                                                    onToggle={() => setOpenStep(openStep === wn.id ? '' : wn.id)}
                                                    canResume={historyActions.resumeFromStep}
                                                    onResumeFrom={resumeFromStep}
                                                />
                                            </div>
                                        );
                                    })}
                                </div>
                            </div>
                        ))
                    )}
                    {/* Correction lines — #227 inject marker steps render as conversation lines */}
                    {correctionLines.map((s, i) => (
                        <div
                            key={`corr-${i}`}
                            className="rounded-md border border-accent/40 bg-accent/5 px-3 py-1.5 text-xs"
                        >
                            {/* i18n: user-facing */}
                            <span className="text-accent">👤 {t('用户修正', 'User correction')}</span>
                            <span className="pl-2 text-text-secondary">{s.summary}</span>
                        </div>
                    ))}
                </div>

                {/* Right rail: capabilities in use (M10 rail) */}
                <div className="w-56 shrink-0 space-y-2 overflow-y-auto">
                    <div className="rounded-md border border-border p-2">
                        <div className="pb-1 text-[10px] font-medium uppercase tracking-wide text-text-muted">
                            {/* i18n: user-facing */}
                            {t('使用中的能力', 'Capabilities in use')}
                        </div>
                        {caps.length === 0 ? (
                            {/* i18n: user-facing */}
                            <div className="text-[11px] text-text-muted">{t('暂无', 'None yet')}</div>
                        ) : (
                            caps.map((c) => (
                                <div key={`${c.kind}:${c.label}:${c.detail ?? ''}`} className="py-0.5 text-[11px]">
                                    <span className="text-text-primary">
                                        {c.kind === 'connector'
                                            ? '🔌'
                                            : c.kind === 'subagent'
                                              ? '🤖'
                                              : c.kind === 'skill'
                                                ? '⚙'
                                                : '✨'}{' '}
                                        {c.label}
                                    </span>
                                    {c.detail ? (
                                        <span className="pl-1 font-mono text-[10px] text-text-muted">{c.detail}</span>
                                    ) : null}
                                    {c.count > 1 ? (
                                        <span className="pl-1 text-[10px] text-text-muted">×{c.count}</span>
                                    ) : null}
                                </div>
                            ))
                        )}
                    </div>
                    {/* W10/B5: run artifact preview — right rail refreshes live with step outputs */}
                    <RunArtifactPane
                        runId={runId}
                        active={record.status === 'running' || record.status === 'pending'}
                        reviewStatus={record.review?.status ?? null}
                    />
                </div>

                {/* Workspace drawer (M6) — Space artifacts of the run's directory */}
                {drawerOpen && record.space_id ? (
                    <WorkspaceDrawer spaceId={record.space_id} onClose={() => setDrawerOpen(false)} />
                ) : null}
            </div>

            {/* M5: bottom interrupt composer — issue #231: FCS-1 five-zone standard;
                §6 approval-waiting variant: disabled while record.hitl_pending + context bar "awaiting approval" badge */}
            {steerable ? (
                <Composer
                    value={instruction}
                    onChange={setInstruction}
                    onSubmit={() => void sendInstruction()}
                    busy={sending}
                    disabled={approvalVariant.disabled}
                    placeholder={
                        record.hitl_pending
                            // i18n: user-facing
                            ? t('等待审批处理后再输入指令', 'Composing resumes after the approval is resolved')
                            : t(
                                  // i18n: user-facing
                                  '追加指令，如「跳过差旅类目」— 在下一节点边界并入',
                                  'Add an instruction, e.g. "skip travel" — lands at the next node boundary'
                              )
                    }
                    // i18n: user-facing
                    ariaLabel={t('运行指令输入框', 'Run instruction composer')}
                    hint={
                        record.hitl_pending
                            // i18n: user-facing
                            ? t('等待审批 — 处理后恢复输入', 'Awaiting approval — composing resumes after resolution')
                            // i18n: user-facing
                            : t('Enter 发送 · Shift+Enter 换行', 'Enter to send · Shift+Enter new line')
                    }
                    // i18n: user-facing
                    sendLabel={t('发送指令', 'Send instruction')}
                    // i18n: user-facing
                    stopLabel={t('停止', 'Stop')}
                    contextChips={runContextChips}
                    references={{
                        items: runReferenceItems,
                        // i18n: user-facing
                        emptyLabel: t('暂无可引用项', 'No references yet'),
                    }}
                />
            ) : null}
        </div>
    );
}

// issue #225: inline approval card — rendered at the gated step position (M7), four actions: reject / edit params / allow once / always allow
function InlineApprovalCard({
    approval,
    deadline,
    busy,
    onConfirm,
}: {
    approval: GuardApproval;
    deadline?: number;
    busy: boolean;
    onConfirm: (
        actionId: string,
        approved: boolean,
        once: boolean,
        editedParams?: Record<string, unknown>,
        comment?: string
    ) => void;
}) {
    const t = useT();
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState('');
    const [draftError, setDraftError] = useState('');
    // issue #328: reject with review comments — the revision loop (on_reject=revise) merges them into the producing node's input on rerun
    const [rejecting, setRejecting] = useState(false);
    const [rejectNote, setRejectNote] = useState('');

    function startEdit() {
        setDraft(JSON.stringify(approval.params ?? {}, null, 4));
        setDraftError('');
        setEditing(true);
    }

    function submitReject() {
        // issue #337: reject is a one-time decision — once=true dequeues and wakes the run, no permanent deny rule
        // (otherwise a single deny rule would pre-reject the same HITL gate type for all future runs and the cards would never appear again)
        void onConfirm(approval.action_id, false, true, undefined, rejectNote.trim() || undefined);
    }

    function submitEdit() {
        const parsed = parseParamsDraft(draft);
        if (!parsed.ok) {
            setDraftError(
                parsed.error === 'invalid_json'
                    // i18n: user-facing
                    ? t('JSON 解析失败', 'Invalid JSON')
                    // i18n: user-facing
                    : t('参数须为 JSON 对象', 'Params must be a JSON object')
            );
            return;
        }
        void onConfirm(approval.action_id, true, true, parsed.params);
    }

    return (
        <div className="rounded-md border border-warning/60 bg-warning/5 p-2">
            <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-text-primary">
                    {/* i18n: user-facing */}
                    {approval.node_id ?? approval.tool_name} · {t('等待审批', 'Awaiting approval')}
                </span>
                {deadline ? <HitlCountdown deadline={deadline} /> : null}
            </div>
            <div className="mt-1 font-mono text-[11px] text-text-secondary">
                {approval.tool_name}({JSON.stringify(approval.params ?? {})})
            </div>
            {(approval.artifacts ?? []).length > 0 ? (
                <div className="mt-2 space-y-1.5">
                    {/* i18n: user-facing */}
                    <div className="text-[10px] text-text-muted">{t('上游产出', 'Upstream output')}</div>
                    {(approval.artifacts ?? []).map((ev) => (
                        <div key={ev.node_id} className="rounded-md border border-border bg-bg px-2 py-1.5">
                            <div className="text-[10px] font-medium text-text-secondary">
                                {ev.node_name || ev.node_id}
                            </div>
                            {Object.entries(ev.output ?? {}).map(([k, v]) => (
                                <div key={k} className="mt-1">
                                    <div className="text-[10px] text-text-muted">{k}</div>
                                    <pre className="mt-0.5 max-h-32 overflow-y-auto whitespace-pre-wrap break-all font-mono text-[11px] text-text-primary">
                                        {typeof v === 'string' ? v : JSON.stringify(v)}
                                    </pre>
                                </div>
                            ))}
                        </div>
                    ))}
                </div>
            ) : null}
            {editing ? (
                <div className="mt-2">
                    <textarea
                        className="min-h-20 w-full resize-y rounded-md border border-border bg-bg p-2 font-mono text-[11px] text-text-primary"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        spellCheck={false}
                    />
                    {draftError ? <div className="mt-1 text-[10px] text-error">{draftError}</div> : null}
                    <div className="mt-1 flex items-center gap-2">
                        <button
                            type="button"
                            disabled={busy}
                            className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10 disabled:opacity-50"
                            onClick={submitEdit}
                        >
                            {/* i18n: user-facing */}
                            {t('按此参数执行', 'Run with these params')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                            onClick={() => setEditing(false)}
                        >
                            {/* i18n: user-facing */}
                            {t('取消', 'Cancel')}
                        </button>
                    </div>
                </div>
            ) : null}
            {rejecting ? (
                <div className="mt-2">
                    <textarea
                        className="min-h-12 w-full resize-y rounded-md border border-border bg-bg p-2 font-mono text-[11px] text-text-primary"
                        value={rejectNote}
                        placeholder={t(
                            // i18n: user-facing
                            '评审意见 (修订重跑时带给产出节点)',
                            'Review comments (passed to the producing node on revision)'
                        )}
                        onChange={(e) => setRejectNote(e.target.value)}
                    />
                    <div className="mt-1 flex items-center gap-2">
                        <button
                            type="button"
                            disabled={busy}
                            className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10 disabled:opacity-50"
                            onClick={submitReject}
                        >
                            {/* i18n: user-facing */}
                            {t('确认拒绝', 'Confirm reject')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                            onClick={() => setRejecting(false)}
                        >
                            {/* i18n: user-facing */}
                            {t('取消', 'Cancel')}
                        </button>
                    </div>
                </div>
            ) : null}
            <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10 disabled:opacity-50"
                    onClick={() => setRejecting(true)}
                >
                    {/* i18n: user-facing */}
                    {t('拒绝', 'Reject')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                    onClick={startEdit}
                >
                    {/* i18n: user-facing */}
                    {t('编辑参数', 'Edit params')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10 disabled:opacity-50"
                    onClick={() => void onConfirm(approval.action_id, true, true)}
                >
                    {/* i18n: user-facing */}
                    {t('本次允许', 'Allow once')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                    onClick={() => void onConfirm(approval.action_id, true, false)}
                >
                    {/* i18n: user-facing */}
                    {t('总是允许', 'Always allow')}
                </button>
            </div>
        </div>
    );
}

// issue #233: artifact card — refetched on render (version bump keeps it fresh),
// degrades to a plain-text reference when the engine is unavailable (does not block the timeline); click deep-links across iframes to the artifact detail
function ArtifactCard({ artifactId, fallbackName }: { artifactId: string; fallbackName: string }) {
    const t = useT();
    const [card, setCard] = useState<ArtifactCardData | null>(null);
    const [failed, setFailed] = useState(false);
    useEffect(() => {
        let alive = true;
        api.artifactCard(artifactId).then(
            (r) => {
                if (alive) setCard(r.artifact);
            },
            () => {
                if (alive) setFailed(true);
            }
        );
        return () => {
            alive = false;
        };
    }, [artifactId]);
    if (failed) {
        return (
            <div className="rounded border border-dashed border-border px-2 py-1 text-[10px] text-text-muted">
                {/* i18n: user-facing */}
                ▦ {t('产物', 'Artifact')} {fallbackName || artifactId} · {t('卡片不可用', 'card unavailable')}
            </div>
        );
    }
    if (!card) {
        return <div className="h-9 animate-pulse rounded border border-border bg-bg-muted" />;
    }
    return (
        <button
            type="button"
            className="flex w-full flex-col gap-0.5 rounded border border-border bg-bg px-2 py-1.5 text-left hover:bg-bg-muted"
            onClick={() => window.open(card.deep_link, '_blank')}
        >
            <div className="flex items-center gap-1.5">
                <span className="text-[10px]">{card.is_starred ? '★' : '▦'}</span>
                <span className="truncate text-[11px] font-medium text-text-primary">{card.name || artifactId}</span>
                {card.type ? (
                    <span className="shrink-0 rounded-sm border border-border px-1 text-[9px] text-text-secondary">
                        {card.type}
                    </span>
                ) : null}
                <span className="shrink-0 font-mono text-[9px] text-text-muted">v{card.current_version}</span>
            </div>
            {card.summary ? <div className="truncate text-[10px] text-text-muted">{card.summary}</div> : null}
        </button>
    );
}

// issue #303: waiting-approval row — state machine waiting state (countdown + approvals entry), fallback when no approval card exists
function WaitingGateCard({ step, deadline }: { step: RunStep; deadline?: number }) {
    const t = useT();
    const [now, setNow] = useState(Date.now() / 1000);
    useEffect(() => {
        const timer = setInterval(() => setNow(Date.now() / 1000), 1000);
        return () => clearInterval(timer);
    }, []);
    const left = deadline ? Math.max(0, Math.round(deadline - now)) : null;
    const mm = left === null ? '' : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
    return (
        <div className="rounded-md border border-dashed border-warning bg-bg-elevated p-2">
            <div className="flex items-center gap-2">
                <span className="truncate text-xs text-text-primary">{step.node_display_name || step.node_name}</span>
                <span className="rounded border border-warning/40 px-1 text-[9px] text-warning">
                    {/* i18n: user-facing */}
                    {t('等待审批', 'waiting')}
                </span>
                {mm ? <span className="ml-auto font-mono text-[10px] text-text-muted">⏳ {mm}</span> : null}
            </div>
            <button
                type="button"
                className="mt-1 text-[11px] text-accent hover:underline"
                onClick={() => {
                    window.location.hash = '#/approvals';
                }}
            >
                {/* i18n: user-facing */}
                {t('去批 ↗', 'Review approvals ↗')}
            </button>
        </div>
    );
}

// Step card: status/duration + M10 capability annotations + expandable input/output (reuses RunsPage StepDrawer)
// issue #226: terminal runs can resume from this step (the new run chains back to the original run)
// issue #233: step outputs with artifact references render cards (payload stays a lightweight ref)
function StepCard({
    step,
    waited,
    expanded,
    onToggle,
    canResume,
    onResumeFrom,
}: {
    step: RunStep;
    waited?: boolean;
    expanded: boolean;
    onToggle: () => void;
    canResume?: boolean;
    onResumeFrom: (nodeId: string) => void;
}) {
    const t = useT();
    const cap = stepCapability(step);
    const denied = step.status === 'denied';
    const arefs = stepArtifactRefs(step);
    // issue #303: decision source — webhook/web approvals write into the blocked step's output_data
    const attribution = (step.output_data as { hitl_attribution?: Record<string, string> } | null)?.hitl_attribution;
    return (
        <div
            className={`rounded-md border bg-bg-elevated ${denied ? 'border-dashed border-warning' : 'border-border'}`}
        >
            <button type="button" className="flex w-full flex-col gap-1 px-2 py-1.5 text-left" onClick={onToggle}>
                <div className="flex items-center gap-2">
                    <span className="truncate text-xs text-text-primary">
                        {step.node_display_name || step.node_name}
                    </span>
                    <StatusBadge status={statusKind(step.status) as StatusKind} label={step.status} />
                    {waited ? (
                        <span className="rounded border border-border px-1 text-[9px] text-text-muted">
                            {/* i18n: user-facing */}
                            {t('曾等待审批', 'waited for approval')}
                        </span>
                    ) : null}
                </div>
                {cap ? (
                    <div className="truncate text-[10px] text-text-muted">
                        {/* i18n: user-facing */}
                        {t('经由', 'via')}{' '}
                        {cap.kind === 'connector'
                            // i18n: user-facing
                            ? t('连接器', 'Connector')
                            : cap.kind === 'subagent'
                              // i18n: user-facing
                              ? t('子代理', 'Sub-agent')
                              : cap.kind === 'skill'
                                // i18n: user-facing
                                ? t('技能', 'Skill')
                                : 'AI'}
                        {cap.detail ? ` · ${cap.detail}` : ''}
                    </div>
                ) : null}
                <div className="flex items-center justify-between text-[10px] text-text-muted">
                    <span className="truncate font-mono">{step.summary || step.error || `#${step.node_id}`}</span>
                    <span className="shrink-0 font-mono">{step.execution_time.toFixed(2)}s</span>
                </div>
                {step.tokens?.total_tokens ? (
                    <div className="font-mono text-[10px] text-text-muted">{step.tokens.total_tokens} tok</div>
                ) : null}
            </button>
            {expanded ? <StepDrawer step={step} /> : null}
            {attribution ? (
                <button
                    type="button"
                    className="w-full border-t border-border px-2 py-1 text-left text-[10px] text-accent hover:underline"
                    onClick={() => {
                        window.location.hash = '#/governance?tab=audit';
                    }}
                >
                    {/* i18n: user-facing */}
                    {t('审批台账', 'Approval ledger')} · {attribution.decided_by || 'unknown'}
                    {attribution.approval_id ? ` · ${attribution.approval_id}` : ''} ↗
                </button>
            ) : null}
            {arefs.length > 0 ? (
                <div className="space-y-1 border-t border-border px-2 py-1.5">
                    {arefs.map((r) => (
                        <ArtifactCard key={r.artifact_id} artifactId={r.artifact_id} fallbackName={r.name} />
                    ))}
                </div>
            ) : null}
            {canResume ? (
                <div className="border-t border-border px-2 py-1.5">
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-[11px] text-text-secondary hover:bg-bg-muted"
                        onClick={() => onResumeFrom(step.node_id)}
                    >
                        {/* i18n: user-facing */}
                        ↻ {t('从这里重跑', 'Resume from this step')}
                    </button>
                </div>
            ) : null}
        </div>
    );
}

// M6 workspace drawer — artifact list + preview + full-space entry for the run's Space
function WorkspaceDrawer({ spaceId, onClose }: { spaceId: string; onClose: () => void }) {
    const t = useT();
    const { data, error } = useAutoRetry(() => api.spaceArtifacts(spaceId), [spaceId]);
    const [selectedId, setSelectedId] = useState('');
    const artifacts: SpaceArtifact[] = data ?? [];
    const selected = artifacts.find((a) => a.id === (selectedId || artifacts[0]?.id));

    return (
        <div className="flex w-72 shrink-0 flex-col rounded-md border border-border p-2">
            <div className="flex items-center justify-between pb-1">
                {/* i18n: user-facing */}
                <div className="text-xs font-medium text-text-primary">{t('工作区产物', 'Workspace artifacts')}</div>
                <button type="button" className="text-xs text-text-muted hover:text-text-primary" onClick={onClose}>
                    ✕
                </button>
            </div>
            <button
                type="button"
                className="self-start pb-1 text-[11px] text-accent hover:underline"
                onClick={() => {
                    window.location.hash = '#/spaces';
                }}
            >
                {/* i18n: user-facing */}
                {t('打开完整空间 →', 'Open full space →')}
            </button>
            {error ? (
                // i18n: user-facing
                <ErrorState message={error.message} retryLabel={t('重试', 'Retry')} onRetry={() => undefined} />
            ) : artifacts.length === 0 ? (
                // i18n: user-facing
                <EmptyState title={t('暂无产物', 'No artifacts yet')} />
            ) : (
                <div className="min-h-0 flex-1 space-y-2 overflow-y-auto">
                    <div className="flex flex-wrap gap-1">
                        {artifacts.map((a) => (
                            <button
                                key={a.id}
                                type="button"
                                className={`rounded border px-1.5 py-0.5 text-[10px] ${
                                    selected?.id === a.id
                                        ? 'border-accent text-text-primary'
                                        : 'border-border text-text-secondary hover:bg-bg-muted'
                                }`}
                                onClick={() => setSelectedId(a.id)}
                            >
                                {a.name}
                            </button>
                        ))}
                    </div>
                    {selected ? (
                        <div className="rounded border border-border bg-bg p-2">
                            <div className="pb-1 font-mono text-[10px] text-text-muted">{selected.artifact_type}</div>
                            <pre className="max-h-60 overflow-auto whitespace-pre-wrap text-[11px] text-text-secondary">
                                {selected.content ?? ''}
                            </pre>
                        </div>
                    ) : null}
                </div>
            )}
        </div>
    );
}

// issue #422 F-1: historical project match verdict card — three-state verdict + score + gaps + detail expansion +
// try existing project (extend only) + re-verdict to new (extend/base, via an override_output directive).
// The base verdict shows only "matched another customer project" per the privacy red line — project name/capabilities are hidden.
function MatchVerdictCard({
    view,
    steerable,
    busy,
    onReverdict,
}: {
    view: MatchVerdictView;
    steerable: boolean;
    busy: boolean;
    onReverdict: () => void;
}) {
    const t = useT();
    const [detailOpen, setDetailOpen] = useState(false);
    const verdictLabel =
        view.verdict === 'extend'
            // i18n: user-facing
            ? t('扩展已有项目', 'Extend existing project')
            : view.verdict === 'base'
              // i18n: user-facing
              ? t('基于成熟方案定制', 'Customize from a mature solution')
              // i18n: user-facing
              : t('新建项目', 'New project');
    const pct = Math.round(view.score * 100);
    const repoUrl = view.matchedRepo ? `https://github.com/${view.matchedRepo}` : '';
    const visible = view.matchedVisible ?? {};
    const visibleCaps = Array.isArray(visible.capabilities) ? (visible.capabilities as unknown[]).map(String) : [];
    return (
        <div className="rounded-md border border-border bg-bg-muted/40 p-2">
            <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-text-primary">
                    {/* i18n: user-facing */}
                    🧭 {t('历史项目比对', 'Historical project match')}
                </span>
                <span className="rounded border border-border bg-bg px-1.5 py-0.5 text-[10px] text-text-secondary">
                    {verdictLabel}
                </span>
            </div>
            <div className="mt-1 text-[11px] text-text-secondary">
                {/* i18n: user-facing */}
                {t('匹配', 'Match')} <b className="text-text-primary">{pct}%</b>
                <span className="pl-1 text-text-muted">
                    {/* i18n: user-facing */}
                    ({t('阈值', 'threshold')} {Math.round(view.threshold * 100)}% {view.candidatesScanned}{' '}
                    {/* i18n: user-facing */}
                    {t('个候选', 'candidates')})
                </span>
            </div>
            {view.gaps.length > 0 ? (
                <div className="mt-1 text-[11px] text-text-secondary">
                    {/* i18n: user-facing */}
                    {t('能力缺口', 'Capability gaps')}:{' '}
                    <span className="font-mono text-text-primary">{view.gaps.join(', ')}</span>
                </div>
            ) : null}
            {view.verdict === 'base' ? (
                <div className="mt-1 text-[11px] text-warning">
                    {t(
                        // i18n: user-facing
                        '命中其他客户项目 — 项目名不可见; 公共能力沉淀基座后新仓继承, 不跨客户改仓',
                        'Matched another customer project — name hidden; shared capabilities distill into the base product, never cross-customer writes'
                    )}
                </div>
            ) : null}
            <div className="mt-1.5 flex flex-wrap gap-1.5">
                {view.verdict !== 'new' ? (
                    <button
                        type="button"
                        className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11px] text-text-secondary hover:text-text-primary"
                        onClick={() => setDetailOpen((v) => !v)}
                    >
                        {/* i18n: user-facing */}
                        {t('详细介绍已有项目', 'Project details')}
                    </button>
                ) : null}
                {view.verdict === 'extend' && repoUrl ? (
                    <a
                        href={repoUrl}
                        target="_blank"
                        rel="noreferrer"
                        className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11px] text-text-secondary hover:text-text-primary"
                    >
                        {/* i18n: user-facing */}
                        ▶ {t('体验已有项目', 'Try existing project')}
                    </a>
                ) : null}
                {view.verdict !== 'new' ? (
                    <button
                        type="button"
                        disabled={!steerable || busy}
                        title={
                            steerable
                                ? t(
                                      // i18n: user-facing
                                      '改判为新建项目, 下游步骤按新判定执行',
                                      'Re-verdict to a new project; downstream steps follow'
                                  )
                                // i18n: user-facing
                                : t('仅运行/暂停状态可改判', 'Only while running or paused')
                        }
                        className="rounded-md border border-border bg-bg px-2 py-0.5 text-[11px] text-text-secondary hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-50"
                        onClick={onReverdict}
                    >
                        {/* i18n: user-facing */}
                        {busy ? t('改判中…', 'Re-verdicting…') : t('改判：新建项目', 'Re-verdict: new project')}
                    </button>
                ) : null}
            </div>
            {detailOpen && view.verdict !== 'new' ? (
                <div className="mt-1.5 rounded-md border border-border bg-bg px-2 py-1.5 text-[11px] text-text-secondary">
                    {view.verdict === 'extend' && visible.slug ? (
                        <>
                            <div className="font-medium text-text-primary">{String(visible.slug)}</div>
                            {repoUrl ? <div className="font-mono text-[10px]">{repoUrl}</div> : null}
                            {visibleCaps.length > 0 ? (
                                <div className="mt-0.5">
                                    {/* i18n: user-facing */}
                                    {t('已有能力', 'Capabilities')}:{' '}
                                    <span className="font-mono">{visibleCaps.join(' · ')}</span>
                                </div>
                            ) : null}
                            {visible.stack ? (
                                <div className="mt-0.5">
                                    {/* i18n: user-facing */}
                                    {t('技术栈', 'Stack')}: <span className="font-mono">{String(visible.stack)}</span>
                                </div>
                            ) : null}
                            {visible.health ? (
                                <div className="mt-0.5">
                                    {/* i18n: user-facing */}
                                    {t('健康度', 'Health')}: {String(visible.health)}
                                </div>
                            ) : null}
                        </>
                    ) : (
                        {/* i18n: user-facing */}
                        <div>{t('项目详情已按隐私策略隐藏', 'Project details hidden by privacy policy')}</div>
                    )}
                </div>
            ) : null}
        </div>
    );
}
