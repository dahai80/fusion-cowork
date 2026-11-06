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
import { capabilitiesInUse, computeWaves, parseParamsDraft, stepArtifactRefs, stepCapability } from '../runview';
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
    // issue #240 R1: ＠ 面板补齐目录/任务引用 — 与首页一致的三类来源
    const workspacesRes = useAutoRetry(() => api.workspaces(), []);
    const recentRunsRes = useAutoRetry(() => api.runs(), []);
    const [openStep, setOpenStep] = useState('');
    const [drawerOpen, setDrawerOpen] = useState(false);
    const [hintDismissed, setHintDismissed] = useState(true);
    const [instruction, setInstruction] = useState('');
    const [sending, setSending] = useState(false);
    // issue #187/#224: HITL 审批卡 + 交付审查在 run 视图内闭环 (UC-2 打断全链路)
    const [hitlApprovals, setHitlApprovals] = useState<GuardApproval[]>([]);
    const [hitlBusy, setHitlBusy] = useState('');
    const [reviewNote, setReviewNote] = useState('');

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

    // W13/B2: 步骤 SSE 实时推送, 失败回落轮询 (与 RunsPage 同源数据)
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

    // issue #187: HITL 等待时轮询审批卡 (按 execution_id 过滤), 2s
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
                retryLabel={t('重试', 'Retry')}
                onRetry={() => setReloadKey((k) => k + 1)}
            />
        );
    if (!record) return null;

    const steerable = record.status === 'running' || record.status === 'paused';
    // issue #226: 终态 run 只读 — 重跑/派生动作仅在终态开放 (集合与 WorkflowStatus 对齐)
    const historyActions = runHistoryActions(record.status);
    const totalTokens = (record.steps ?? []).reduce((n, s) => n + (s.tokens?.total_tokens ?? 0), 0);
    const waves = computeWaves(record.workflow_def ?? {});
    // issue #303: 闸门进度条 + blocked→终态 同节点步骤合并状态机
    const gates = gateProgress(record.workflow_def, record.steps ?? []);
    const caps = capabilitiesInUse(record.steps ?? []);
    const correctionLines = (record.steps ?? []).filter((s) => s.user_correction);
    // issue #225: 审批卡内联到被闸节点位置; 无法定位的卡片浮在顶部 (fallback)
    const waveNodeIds = new Set(waves.flatMap((w) => w.nodes.map((n) => n.id)));
    const inlineApprovals = hitlApprovals.filter((a) => a.node_id && waveNodeIds.has(a.node_id));
    const floatingApprovals = hitlApprovals.filter((a) => !a.node_id || !waveNodeIds.has(a.node_id));

    // issue #231 (FCS-1 §6): 审批等待变体 — 上下文条徽标 + composer 禁用, 不另造替代 UI
    const approvalVariant = composerApprovalVariant(!!record.hitl_pending);
    const runContextChips: ComposerChip[] = buildContextChips({
        waitingApproval: approvalVariant.waitingApproval,
        waitingLabel: t('等待审批', 'Awaiting approval'),
    });
    // issue #240 R1: ＠ 引用面板 — 目录/任务/产物三类 (首页同款), 产物来自本 run 步骤
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
                            ? t('已批准并应用编辑参数', 'Approved with edited params')
                            : t('已批准 (本次)', 'Approved (once)')
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

    // W9: 交付审查 — pending_review → approved/rejected
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

    // issue #226: 从指定步骤重跑 — 新 run 链回原 run, 跳转新 run 视图
    async function resumeFromStep(nodeId: string) {
        try {
            const r = await api.resumeRunFrom(runId, nodeId);
            sdk.toast.show({
                level: 'success',
                message: t(
                    `已从该步骤重跑 (${r.snapshot_steps} 步快照续跑)`,
                    `Resumed (${r.snapshot_steps} steps replayed)`
                ),
            });
            window.location.hash = `#/runs/${r.run_id}`;
        } catch (e) {
            console.error('[cowork-web] resume-from failed', e);
            sdk.toast.show({ level: 'error', message: t('重跑失败', 'Resume failed') });
        }
    }

    // issue #226: 派生新任务 — 预填首页 composer (任务 + 上次结果上下文)
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
            // storage 不可用 — 直接回首页不带预填
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
                message: t('指令已排队，下一节点边界生效', 'Queued — lands at the next node boundary'),
            });
            setInstruction('');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] inject failed', e);
            sdk.toast.show({ level: 'error', message: t('指令发送失败', 'Failed to send') });
        } finally {
            setSending(false);
        }
    }

    return (
        <div className="mx-auto flex h-full max-w-6xl flex-col gap-3">
            {/* 头部: 返回 + run 身份 + 控制 */}
            <div className="flex flex-wrap items-center gap-2">
                <button
                    type="button"
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    onClick={() => {
                        window.location.hash = '#/runs';
                    }}
                >
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
                        {t(`已修正 ×${correctionLines.length}`, `${correctionLines.length} corrections`)}
                    </span>
                ) : null}
                <div className="flex-1" />
                {record.status === 'running' ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => void steer(() => api.pauseRun(runId), t('暂停失败', 'Pause failed'))}
                    >
                        ⏸ {t('暂停', 'Pause')}
                    </button>
                ) : null}
                {record.status === 'paused' ? (
                    <button
                        type="button"
                        className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10"
                        onClick={() => void steer(() => api.resumeRun(runId), t('恢复失败', 'Resume failed'))}
                    >
                        ▶ {t('继续', 'Resume')}
                    </button>
                ) : null}
                {record.space_id ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => setDrawerOpen(!drawerOpen)}
                    >
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
                        ⇶ {t('管线视图', 'Pipeline view')}
                    </button>
                ) : null}
                {historyActions.deriveNewTask ? (
                    <button
                        type="button"
                        className="rounded-md border border-accent px-2 py-1 text-xs text-accent hover:bg-accent/10"
                        onClick={deriveNewTask}
                    >
                        ✚ {t('派生新任务', 'Derive new task')}
                    </button>
                ) : null}
                {record.labels?.artifact_ref ? (
                    <span
                        title={record.labels.artifact_ref}
                        className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-secondary"
                    >
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
                        ↻ {t('重跑自', 'resumed from')} {record.labels.resume_of.slice(0, 12)}
                    </button>
                ) : null}
            </div>
            {record.error ? <div className="text-xs text-error">{record.error}</div> : null}
            {record.result?.summary ? <div className="text-xs text-text-secondary">{record.result.summary}</div> : null}

            {/* 首次教育提示 (可关) */}
            {!hintDismissed ? (
                <div className="flex items-center justify-between rounded-md border border-accent/50 bg-accent/5 px-3 py-1.5 text-xs text-text-secondary">
                    <span>
                        {t('每一步可见 · 随时可追加指令或打断', 'Every step visible · redirect or interrupt any time')}
                    </span>
                    <button type="button" className="text-text-muted hover:text-text-primary" onClick={dismissHint}>
                        ✕
                    </button>
                </div>
            ) : null}

            {/* issue #187/#225: HITL 审批卡 — 内联到被闸步骤位, 定位不到的浮在顶部 */}
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

            {/* W9: 交付审查 — pending_review 在 run 视图内通过/退回 */}
            {record.review?.status === 'pending_review' ? (
                <div className="rounded-md border border-border p-2">
                    <div className="flex items-center gap-2">
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
                </div>
            ) : null}

            {/* 主体: 时间线 + 右栏 (能力清单/工作区抽屉) */}
            <div className="flex min-h-0 flex-1 gap-3">
                <div className="min-w-0 flex-1 space-y-3 overflow-y-auto pr-1">
                    {waves.length === 0 ? (
                        <EmptyState title={t('暂无步骤', 'No steps yet')} />
                    ) : (
                        waves.map((wave) => (
                            <div key={wave.depth}>
                                <div className="pb-1 text-[10px] font-medium uppercase tracking-wide text-text-muted">
                                    {t(
                                        wave.nodes.length > 1
                                            ? `并行 ${wave.nodes.length} 路`
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
                                        // issue #303: 同节点 blocked→终态 合并为一行状态机
                                        // (queued→waiting→passed/failed) — 闸门节点不再出两行
                                        const nodeSteps = (record.steps ?? []).filter(
                                            (s) => s.node_id === wn.id && !s.user_correction
                                        );
                                        const blockedStep = nodeSteps.find((s) => s.status === 'blocked');
                                        const terminalStep = nodeSteps.find((s) => s.status !== 'blocked');
                                        const step = terminalStep ?? blockedStep;
                                        // issue #225: 审批卡内联在暂停步骤位 (M7 badge)
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
                                                    {wn.label || wn.name} · {t('等待', 'pending')}
                                                </div>
                                            );
                                        }
                                        // issue #303: 等待审批且无审批卡可内联 → 等待行 (倒计时 + 去批入口)
                                        if (step.status === 'blocked' && record.hitl_pending && !approval) {
                                            return (
                                                <WaitingGateCard
                                                    key={wn.id}
                                                    step={step}
                                                    deadline={record.hitl_deadline ?? undefined}
                                                />
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
                    {/* 修正行 — #227 inject 标记步渲染为对话线 */}
                    {correctionLines.map((s, i) => (
                        <div
                            key={`corr-${i}`}
                            className="rounded-md border border-accent/40 bg-accent/5 px-3 py-1.5 text-xs"
                        >
                            <span className="text-accent">👤 {t('用户修正', 'User correction')}</span>
                            <span className="pl-2 text-text-secondary">{s.summary}</span>
                        </div>
                    ))}
                </div>

                {/* 右栏: 能力清单 (M10 rail) */}
                <div className="w-56 shrink-0 space-y-2 overflow-y-auto">
                    <div className="rounded-md border border-border p-2">
                        <div className="pb-1 text-[10px] font-medium uppercase tracking-wide text-text-muted">
                            {t('使用中的能力', 'Capabilities in use')}
                        </div>
                        {caps.length === 0 ? (
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
                    {/* W10/B5: run 产物预览 — 右栏实时随步骤输出刷新 */}
                    <RunArtifactPane
                        runId={runId}
                        active={record.status === 'running' || record.status === 'pending'}
                        reviewStatus={record.review?.status ?? null}
                    />
                </div>

                {/* 工作区抽屉 (M6) — run 归属目录的 Space 产物 */}
                {drawerOpen && record.space_id ? (
                    <WorkspaceDrawer spaceId={record.space_id} onClose={() => setDrawerOpen(false)} />
                ) : null}
            </div>

            {/* M5: 底部打断 composer — issue #231: FCS-1 五区标准;
                §6 审批等待变体: record.hitl_pending 时禁用 + 上下文条 "等待审批" 徽标 */}
            {steerable ? (
                <Composer
                    value={instruction}
                    onChange={setInstruction}
                    onSubmit={() => void sendInstruction()}
                    busy={sending}
                    disabled={approvalVariant.disabled}
                    placeholder={
                        record.hitl_pending
                            ? t('等待审批处理后再输入指令', 'Composing resumes after the approval is resolved')
                            : t(
                                  '追加指令，如「跳过差旅类目」— 在下一节点边界并入',
                                  'Add an instruction, e.g. "skip travel" — lands at the next node boundary'
                              )
                    }
                    ariaLabel={t('运行指令输入框', 'Run instruction composer')}
                    hint={
                        record.hitl_pending
                            ? t('等待审批 — 处理后恢复输入', 'Awaiting approval — composing resumes after resolution')
                            : t('Enter 发送 · Shift+Enter 换行', 'Enter to send · Shift+Enter new line')
                    }
                    sendLabel={t('发送指令', 'Send instruction')}
                    stopLabel={t('停止', 'Stop')}
                    contextChips={runContextChips}
                    references={{
                        items: runReferenceItems,
                        emptyLabel: t('暂无可引用项', 'No references yet'),
                    }}
                />
            ) : null}
        </div>
    );
}

// issue #225: 内联审批卡 — 渲染在被闸步骤位 (M7), 四动作: 拒绝 / 编辑参数 / 本次允许 / 总是允许
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
    // issue #328: 拒绝附评审意见 — 修订回路 (on_reject=revise) 重跑时并入产出节点输入
    const [rejecting, setRejecting] = useState(false);
    const [rejectNote, setRejectNote] = useState('');

    function startEdit() {
        setDraft(JSON.stringify(approval.params ?? {}, null, 4));
        setDraftError('');
        setEditing(true);
    }

    function submitReject() {
        void onConfirm(approval.action_id, false, false, undefined, rejectNote.trim() || undefined);
    }

    function submitEdit() {
        const parsed = parseParamsDraft(draft);
        if (!parsed.ok) {
            setDraftError(
                parsed.error === 'invalid_json'
                    ? t('JSON 解析失败', 'Invalid JSON')
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
                    {approval.node_id ?? approval.tool_name} · {t('等待审批', 'Awaiting approval')}
                </span>
                {deadline ? <HitlCountdown deadline={deadline} /> : null}
            </div>
            <div className="mt-1 font-mono text-[11px] text-text-secondary">
                {approval.tool_name}({JSON.stringify(approval.params ?? {})})
            </div>
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
                            {t('按此参数执行', 'Run with these params')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                            onClick={() => setEditing(false)}
                        >
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
                            {t('确认拒绝', 'Confirm reject')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                            onClick={() => setRejecting(false)}
                        >
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
                    {t('拒绝', 'Reject')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                    onClick={startEdit}
                >
                    {t('编辑参数', 'Edit params')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-success px-2 py-1 text-xs text-success hover:bg-success/10 disabled:opacity-50"
                    onClick={() => void onConfirm(approval.action_id, true, true)}
                >
                    {t('本次允许', 'Allow once')}
                </button>
                <button
                    type="button"
                    disabled={busy}
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                    onClick={() => void onConfirm(approval.action_id, true, false)}
                >
                    {t('总是允许', 'Always allow')}
                </button>
            </div>
        </div>
    );
}

// issue #233: artifact 卡片 — 渲染时重新拉取 (版本 bump 保持新鲜),
// 引擎不可用时降级为纯文本引用 (不阻塞时间线); 点击跨 iframe 深链到产物详情
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

// issue #303: 等待审批行 — 状态机 waiting 态 (倒计时 + 去批入口), 无审批卡时的兜底
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
                {t('去批 ↗', 'Review approvals ↗')}
            </button>
        </div>
    );
}

// 步骤卡: 状态/耗时 + M10 能力标注 + 展开 input/output (复用 RunsPage StepDrawer)
// issue #226: 终态 run 可从本步骤重跑 (新 run 链回原 run)
// issue #233: 步骤输出含 artifact 引用时渲染卡片 (payload 保持轻量 ref)
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
    // issue #303: 决定来源 — webhook/web 审批写入 blocked 步骤 output_data
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
                            {t('曾等待审批', 'waited for approval')}
                        </span>
                    ) : null}
                </div>
                {cap ? (
                    <div className="truncate text-[10px] text-text-muted">
                        {t('经由', 'via')}{' '}
                        {cap.kind === 'connector'
                            ? t('连接器', 'Connector')
                            : cap.kind === 'subagent'
                              ? t('子代理', 'Sub-agent')
                              : cap.kind === 'skill'
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
                        ↻ {t('从这里重跑', 'Resume from this step')}
                    </button>
                </div>
            ) : null}
        </div>
    );
}

// M6 工作区抽屉 — run 归属 Space 的产物列表 + 预览 + 全量入口
function WorkspaceDrawer({ spaceId, onClose }: { spaceId: string; onClose: () => void }) {
    const t = useT();
    const { data, error } = useAutoRetry(() => api.spaceArtifacts(spaceId), [spaceId]);
    const [selectedId, setSelectedId] = useState('');
    const artifacts: SpaceArtifact[] = data ?? [];
    const selected = artifacts.find((a) => a.id === (selectedId || artifacts[0]?.id));

    return (
        <div className="flex w-72 shrink-0 flex-col rounded-md border border-border p-2">
            <div className="flex items-center justify-between pb-1">
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
                {t('打开完整空间 →', 'Open full space →')}
            </button>
            {error ? (
                <ErrorState message={error.message} retryLabel={t('重试', 'Retry')} onRetry={() => undefined} />
            ) : artifacts.length === 0 ? (
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
