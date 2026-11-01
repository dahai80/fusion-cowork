import { useAutoRetry } from '@fusion/web-ui';
import { EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useState } from 'react';
import { type RunListItem, type WorkspaceInfo, api, statusKind } from '../api';
import { useT } from '../i18n';
import { preferredModel } from '../modelPref';
import { sdk } from '../sdk';

// issue #223: workspace-first + task-first Cowork home.
// 态零 (no active workspace): directory picker — recent/shared Space cards +
// create-new (invite members inline). 态一 (workspace active): topbar context,
// big NL task input with capability picker, DAG plan preview (M3), recent-task
// cards scoped to the workspace, approvals bell. Anthropic "working
// directory" maps to a fusion Space — no new backend entity.

const ACTIVE_KEY = 'cowork.activeWorkspace';

function fmtTime(ts: number | null | undefined, lang: 'zh' | 'en'): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

function activityLabel(w: WorkspaceInfo, t: (zh: string, en: string) => string): string {
    if (w.last_run_at) return fmtTime(w.last_run_at, sdk.i18n.lang());
    if (w.updated_at) return w.updated_at.slice(0, 16).replace('T', ' ');
    return t('暂无活动', 'no activity');
}

function readActive(): string {
    try {
        return localStorage.getItem(ACTIVE_KEY) || '';
    } catch {
        return '';
    }
}

export function HomePage() {
    const [activeId, setActiveId] = useState(readActive);
    if (!activeId) {
        return <WorkspacePicker onEnter={(id) => setActiveId(id)} />;
    }
    return (
        <TaskHome
            activeId={activeId}
            onSwitch={() => {
                try {
                    localStorage.removeItem(ACTIVE_KEY);
                } catch {
                    // ignore storage errors — switching still works in-session
                }
                setActiveId('');
            }}
        />
    );
}

// ── 态零: 选择工作目录 ──

function WorkspacePicker({ onEnter }: { onEnter: (id: string) => void }) {
    const t = useT();
    const lang = sdk.i18n.lang();
    const retry = useAutoRetry(() => api.workspaces(), []);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [invite, setInvite] = useState('');
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState('');

    async function createWorkspace() {
        if (!name.trim() || creating) return;
        setCreating(true);
        setCreateError('');
        try {
            const sp = await api.createSpace({ name: name.trim(), description: description.trim() });
            const ids = invite
                .split(/[,，\s]+/)
                .map((s) => s.trim())
                .filter(Boolean);
            for (const uid of ids) {
                try {
                    await api.addSpaceMember(sp.id, { user_id: uid });
                } catch (e) {
                    console.error('[cowork-web] invite member failed', uid, e);
                }
            }
            onEnter(sp.id);
        } catch (e) {
            setCreateError(e instanceof Error ? e.message : String(e));
        } finally {
            setCreating(false);
        }
    }

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const workspaces = retry.data?.workspaces ?? [];
    const recent = [...workspaces]
        .sort(
            (a, b) =>
                (b.last_run_at ?? 0) - (a.last_run_at ?? 0) || (b.updated_at ?? '').localeCompare(a.updated_at ?? '')
        )
        .slice(0, 4);
    const recentIds = new Set(recent.map((w) => w.id));
    const shared = workspaces.filter((w) => !recentIds.has(w.id));

    function card(w: WorkspaceInfo) {
        return (
            <button
                type="button"
                key={w.id}
                className="flex w-full flex-col gap-1.5 rounded-lg border border-border bg-bg-elevated p-4 text-left hover:bg-bg-muted"
                onClick={() => onEnter(w.id)}
            >
                <div className="truncate text-sm font-medium text-text-primary">📁 {w.name}</div>
                <div className="text-xs text-text-secondary">
                    {t(
                        `${w.member_count} 成员 · ${w.task_count} 任务`,
                        `${w.member_count} members · ${w.task_count} tasks`
                    )}
                </div>
                <div className="flex items-center justify-between text-[10px] text-text-muted">
                    <span>{activityLabel(w, t)}</span>
                    <span className="rounded border border-border px-1.5 py-0.5 text-text-secondary">
                        {t('进入', 'Enter')}
                    </span>
                </div>
            </button>
        );
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="pb-6 pt-8 text-center">
                <div className="text-lg font-medium text-text-primary">
                    {t('选择一个工作目录开始', 'Choose a working directory to start')}
                </div>
                <div className="pt-1 text-xs text-text-muted">
                    {t(
                        '所有任务、会话与产物都会保存在所选目录内，团队成员共享',
                        'All tasks, sessions and artifacts stay in the directory, shared with your team'
                    )}
                </div>
            </div>
            {retry.data && retry.data.available === false ? (
                <EmptyState
                    title={t('工作区不可用', 'Workspaces unavailable')}
                    description={t(
                        '协作空间未装配 — 用 [web] extra 启动并设置空间数据目录',
                        'Space stack not assembled — start with the [web] extra and a space data dir'
                    )}
                />
            ) : (
                <>
                    <div className="pb-2 text-xs font-medium text-text-secondary">{t('最近目录', 'Recent')}</div>
                    {recent.length === 0 ? (
                        <div className="pb-4 text-xs text-text-muted">{t('暂无', 'None yet')}</div>
                    ) : (
                        <div className="grid grid-cols-1 gap-3 pb-4 sm:grid-cols-2">{recent.map(card)}</div>
                    )}
                    {shared.length > 0 ? (
                        <>
                            <div className="pb-2 text-xs font-medium text-text-secondary">
                                {t('团队共享目录', 'Team-shared directories')}
                            </div>
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">{shared.map(card)}</div>
                        </>
                    ) : null}
                    <div className="mt-6 rounded-lg border border-border bg-bg-elevated p-4">
                        <div className="pb-2 text-xs font-medium text-text-secondary">
                            {t('＋ 新建工作目录（可邀请成员）', '＋ New working directory (invite members)')}
                        </div>
                        <div className="flex flex-col gap-2">
                            <input
                                className="rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary"
                                placeholder={t(
                                    '目录名称，如 "Q3 财务对账"',
                                    'Directory name, e.g. "Q3 reconciliation"'
                                )}
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                            />
                            <input
                                className="rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary"
                                placeholder={t('描述（可选）', 'Description (optional)')}
                                value={description}
                                onChange={(e) => setDescription(e.target.value)}
                            />
                            <input
                                className="rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary"
                                placeholder={t(
                                    '邀请成员用户 ID（逗号分隔，可选）',
                                    'Invite member user ids (comma-separated, optional)'
                                )}
                                value={invite}
                                onChange={(e) => setInvite(e.target.value)}
                            />
                            {createError ? <div className="text-xs text-error">{createError}</div> : null}
                            <button
                                type="button"
                                disabled={!name.trim() || creating}
                                className="self-start rounded-md bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50"
                                onClick={() => void createWorkspace()}
                            >
                                {creating ? t('创建中…', 'Creating…') : t('创建并进入', 'Create & enter')}
                            </button>
                        </div>
                    </div>
                    <div className="pt-3 text-center text-[10px] text-text-muted">
                        {t('语言', 'Lang')}: {lang === 'en' ? 'EN' : '中文'}
                    </div>
                </>
            )}
        </div>
    );
}

// ── 态一: task-first 首页 ──

interface PlanPreview {
    name: string;
    nodes: { id: string; name: string; label?: string }[];
    edges: { source_id: string; target_id: string }[];
    workflow: Record<string, unknown>;
}

function TaskHome({ activeId, onSwitch }: { activeId: string; onSwitch: () => void }) {
    const t = useT();
    const lang = sdk.i18n.lang();
    const ws = useAutoRetry(() => api.workspaces(), []);
    const runs = useAutoRetry(() => api.runs(activeId), [activeId]);
    const skills = useAutoRetry(() => api.extensionSkills(), []);
    const approvals = useAutoRetry(() => api.pendingApprovals(), []);
    const [prompt, setPrompt] = useState('');
    const [preview, setPreview] = useState<PlanPreview | null>(null);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [caps, setCaps] = useState<string[]>([]);

    const space = (ws.data?.workspaces ?? []).find((w) => w.id === activeId);
    const pendingCount = (approvals.data?.approvals ?? []).length;
    const runList = runs.data?.runs ?? [];

    function promptWithContext(): string {
        if (caps.length === 0) return prompt;
        return `${prompt}\n\n(可用能力: ${caps.join(', ')})`;
    }

    async function generatePreview() {
        if (!prompt.trim() || busy) return;
        setBusy('preview');
        setError('');
        try {
            const r = await api.generateWorkflow({ prompt: promptWithContext(), model: preferredModel() || undefined });
            const wf = r.workflow as {
                name?: string;
                nodes: { id: string; name: string; config?: { label?: string } }[];
                edges: { source_id: string; target_id: string }[];
            };
            setPreview({
                name: wf.name || prompt.slice(0, 24),
                nodes: (wf.nodes ?? []).map((n) => ({ id: n.id, name: n.name, label: n.config?.label })),
                edges: wf.edges ?? [],
                workflow: r.workflow as Record<string, unknown>,
            });
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    async function runWorkflow() {
        if (!preview || busy) return;
        setBusy('run');
        setError('');
        try {
            await api.startRun({
                workflow: preview.workflow,
                name: preview.name,
                memory: true,
                source: 'home',
                space_id: activeId,
            });
            window.location.hash = '#/runs';
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    async function editInCanvas() {
        if (!preview || busy) return;
        setBusy('canvas');
        setError('');
        try {
            const saved = await api.createWorkflow({
                name: preview.name,
                description: t('首页计划预览生成', 'Generated from home plan preview'),
                workflow: preview.workflow,
            });
            window.location.hash = `#/canvas/${saved.id}`;
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    function followUp(r: RunListItem) {
        setPrompt(t(`基于任务 "${r.workflow_name}" 的结果继续: `, `Follow up on task "${r.workflow_name}": `));
        setPreview(null);
    }

    const skillNames = (skills.data?.skills ?? []).map((s) => s.name);

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center justify-between pb-4">
                <button
                    type="button"
                    className="flex items-center gap-2 rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-sm text-text-primary hover:bg-bg-muted"
                    onClick={onSwitch}
                    title={t('切换工作目录', 'Switch working directory')}
                >
                    📁 {space?.name ?? activeId} ▾
                    {space ? (
                        <span className="text-xs text-text-muted">
                            · {t(`${space.member_count} 成员共享`, `${space.member_count} members shared`)}
                        </span>
                    ) : null}
                </button>
                <button
                    type="button"
                    className="flex items-center gap-1.5 rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-sm text-text-primary hover:bg-bg-muted"
                    onClick={() => {
                        window.location.hash = '#/approvals';
                    }}
                >
                    🔔 {t('审批', 'Approvals')}
                    {pendingCount > 0 ? (
                        <span className="rounded-full bg-error px-1.5 text-[10px] leading-4 text-white">
                            {pendingCount}
                        </span>
                    ) : null}
                </button>
            </div>

            <div className="pb-2 pt-4 text-center text-lg font-medium text-text-primary">
                {t('今天想让 Cowork 做什么？', 'What should Cowork do today?')}
            </div>
            <div className="rounded-xl border border-border bg-bg-elevated p-3">
                <textarea
                    className="min-h-24 w-full resize-y rounded-lg border border-border bg-bg p-3 text-sm text-text-primary"
                    placeholder={t(
                        '描述任务，如 "把 10 月对账跑一遍，异常项整理成报告"',
                        'Describe a task, e.g. "run the October reconciliation and report anomalies"'
                    )}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                />
                <div className="flex flex-wrap items-center gap-2 pt-2">
                    {skillNames.length > 0 ? (
                        <select
                            multiple
                            aria-label={t('能力', 'Capabilities')}
                            className="max-h-16 rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                            value={caps}
                            onChange={(e) => setCaps(Array.from(e.target.selectedOptions).map((o) => o.value))}
                        >
                            {skillNames.map((n) => (
                                <option key={n} value={n}>
                                    ⚙ {n}
                                </option>
                            ))}
                        </select>
                    ) : null}
                    <div className="flex-1" />
                    <button
                        type="button"
                        disabled={!prompt.trim() || busy !== ''}
                        className="rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                        onClick={() => void generatePreview()}
                    >
                        {busy === 'preview' ? t('生成中…', 'Planning…') : t('⚙ 生成计划', '⚙ Plan')}
                    </button>
                </div>
            </div>

            {error ? <div className="pt-2 text-xs text-error">{error}</div> : null}

            {preview ? (
                <div className="mt-4 rounded-xl border border-border bg-bg-elevated p-4">
                    <div className="flex items-center justify-between">
                        <div className="text-sm font-medium text-text-primary">
                            {t('执行计划', 'Plan')} · {preview.name}
                        </div>
                        <div className="text-xs text-text-muted">
                            {t(
                                `${preview.nodes.length} 节点 · ${preview.edges.length} 连接`,
                                `${preview.nodes.length} nodes · ${preview.edges.length} edges`
                            )}
                        </div>
                    </div>
                    <div className="flex flex-wrap gap-1.5 pt-2">
                        {preview.nodes.map((n, i) => (
                            <span
                                key={n.id}
                                className="rounded-md border border-border bg-bg px-2 py-1 text-[11px] text-text-secondary"
                            >
                                {i + 1}. {n.label || n.name}
                            </span>
                        ))}
                    </div>
                    <div className="flex gap-2 pt-3">
                        <button
                            type="button"
                            disabled={busy !== ''}
                            className="rounded-md bg-brand px-3 py-1.5 text-sm text-white disabled:opacity-50"
                            onClick={() => void runWorkflow()}
                        >
                            {busy === 'run' ? t('启动中…', 'Starting…') : t('▶ 运行', '▶ Run')}
                        </button>
                        <button
                            type="button"
                            disabled={busy !== ''}
                            className="rounded-md border border-border bg-bg px-3 py-1.5 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                            onClick={() => void editInCanvas()}
                        >
                            {t('在画布中编辑', 'Edit in canvas')}
                        </button>
                        <button
                            type="button"
                            className="rounded-md px-3 py-1.5 text-sm text-text-muted hover:bg-bg-muted"
                            onClick={() => setPreview(null)}
                        >
                            {t('放弃', 'Discard')}
                        </button>
                    </div>
                </div>
            ) : null}

            <div className="flex items-center justify-between pb-2 pt-6">
                <div className="text-xs font-medium text-text-secondary">
                    {t('最近任务（本目录）', 'Recent tasks (this directory)')}
                </div>
                <button
                    type="button"
                    className="text-xs text-text-muted hover:text-text-primary"
                    onClick={() => {
                        window.location.hash = '#/runs';
                    }}
                >
                    {t('全部运行 →', 'All runs →')}
                </button>
            </div>
            {runs.error ? (
                <ErrorState message={runs.error.message} onRetry={runs.manualRetry} />
            ) : runList.length === 0 ? (
                <EmptyState title={t('本目录还没有任务', 'No tasks in this directory yet')} />
            ) : (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    {runList.slice(0, 6).map((r) => (
                        <div key={r.run_id} className="rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="flex items-center justify-between gap-2">
                                <div className="min-w-0 flex-1 truncate text-sm font-medium text-text-primary">
                                    {r.workflow_name || r.run_id}
                                </div>
                                <StatusBadge status={statusKind(r.status) as StatusKind} label={r.status} />
                            </div>
                            <div className="pt-1 text-[10px] text-text-muted">{fmtTime(r.created_at, lang)}</div>
                            <div className="flex gap-2 pt-2">
                                <button
                                    type="button"
                                    className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-muted"
                                    onClick={() => {
                                        window.location.hash = '#/runs';
                                    }}
                                >
                                    {t('打开', 'Open')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded border border-border px-2 py-0.5 text-[11px] text-text-secondary hover:bg-bg-muted"
                                    onClick={() => followUp(r)}
                                >
                                    {t('追问', 'Follow up')}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
