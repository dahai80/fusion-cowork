import { useAutoRetry } from '@fusion/web-ui';
import {
    Composer,
    type ComposerActionItem,
    type ComposerAttachment,
    type ComposerChip,
    EmptyState,
    ErrorState,
    SkeletonLines,
    StatusBadge,
} from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type RunListItem, type WorkspaceInfo, api, statusKind } from '../api';
import {
    type ArtifactRef,
    artifactRefFailureKind,
    artifactRefFailureMessage,
    artifactRefFromCard,
    artifactRefPrompt,
    artifactRunContext,
    parseArtifactRefParam,
} from '../artifactRef';
import { poolExtraCapabilities, poolSkillNames } from '../capabilities';
import { buildContextChips, buildReferenceItems, insertRefToken } from '../composer';
import { useT } from '../i18n';
import { preferredModel, setPreferredModel } from '../modelPref';
import { sdk } from '../sdk';
import { breadcrumbSegments, dirBasename, selectableSystemDirs } from '../systemdirs';

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
    // issue #251: 新建目录名只能从系统工作目录选 (弹窗逐级下钻, 可选子目录) — 不收手输
    const [pickerOpen, setPickerOpen] = useState(false);
    const [browsePath, setBrowsePath] = useState('');
    const [picked, setPicked] = useState<{ name: string; relative: string } | null>(null);
    const dirs = useAutoRetry(() => api.systemDirectories(browsePath), [browsePath, pickerOpen]);
    const [description, setDescription] = useState('');
    const [invite, setInvite] = useState('');
    const [creating, setCreating] = useState(false);
    const [createError, setCreateError] = useState('');

    async function createWorkspace() {
        if (!picked || creating) return;
        setCreating(true);
        setCreateError('');
        try {
            const sp = await api.createSpace({ name: picked.name, description: description.trim() });
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
    // issue #251: 已有同名工作区 → 该目录不可选 (重名无意义)
    const usedNames = new Set(workspaces.map((w) => w.name));
    const dirOptions = dirs.data?.directories ?? [];
    const selectable = selectableSystemDirs(dirOptions, usedNames);
    const crumbs = breadcrumbSegments(dirs.data?.relative ?? '');
    const currentName = dirBasename(dirs.data?.relative ?? '', dirs.data?.root_name ?? '');
    const currentUsed = usedNames.has(currentName);

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
                            {dirs.error ? (
                                <div className="text-xs text-error">
                                    {t('系统工作目录加载失败', 'Failed to load system directories')} —{' '}
                                    {dirs.error.message}
                                </div>
                            ) : null}
                            {dirs.data && dirs.data.available === false ? (
                                <div className="text-xs text-text-secondary">
                                    {t(
                                        `系统工作目录根不存在: ${dirs.data.root} — 设置 FUSION_COWORK_WORKSPACE_ROOT 后重试`,
                                        `System directory root missing: ${dirs.data.root} — set FUSION_COWORK_WORKSPACE_ROOT and retry`
                                    )}
                                </div>
                            ) : null}
                            {picked ? (
                                <div className="flex items-center justify-between rounded-md border border-border bg-bg px-2 py-1.5 text-sm text-text-primary">
                                    <span className="truncate">
                                        📁 {picked.name}
                                        {picked.relative ? (
                                            <span className="text-[10px] text-text-muted"> /{picked.relative}</span>
                                        ) : null}
                                    </span>
                                    <button
                                        type="button"
                                        className="text-xs text-text-secondary hover:text-text-primary"
                                        onClick={() => setPicked(null)}
                                    >
                                        {t('清除', 'Clear')}
                                    </button>
                                </div>
                            ) : (
                                <button
                                    type="button"
                                    className="rounded-md border border-border bg-bg px-2 py-1.5 text-left text-sm text-text-secondary hover:border-accent/60"
                                    onClick={() => {
                                        setBrowsePath('');
                                        setPickerOpen(true);
                                    }}
                                >
                                    {t('选择系统工作目录…', 'Select a system working directory…')}
                                </button>
                            )}
                            {pickerOpen ? (
                                <div
                                    role="presentation"
                                    className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
                                    onClick={(e) => {
                                        if (e.target === e.currentTarget) setPickerOpen(false);
                                    }}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Escape') setPickerOpen(false);
                                    }}
                                >
                                    <div
                                        role="dialog"
                                        aria-modal="true"
                                        aria-label={t('选择系统工作目录', 'Select a system working directory')}
                                        className="flex max-h-[70vh] w-full max-w-md flex-col gap-3 rounded-xl border border-border bg-bg-elevated p-4 shadow-md dark:shadow-zinc-900/5"
                                    >
                                        <div className="flex items-center justify-between">
                                            <div className="text-sm font-medium text-text-primary">
                                                {t('选择系统工作目录', 'Select a system working directory')}
                                            </div>
                                            <button
                                                type="button"
                                                className="text-text-secondary hover:text-text-primary"
                                                aria-label={t('关闭', 'Close')}
                                                onClick={() => setPickerOpen(false)}
                                            >
                                                ×
                                            </button>
                                        </div>
                                        <div className="flex flex-wrap items-center gap-1 text-xs text-text-secondary">
                                            <button
                                                type="button"
                                                className="hover:text-text-primary"
                                                onClick={() => setBrowsePath('')}
                                            >
                                                📁 {dirs.data?.root_name ?? t('根', 'root')}
                                            </button>
                                            {crumbs.map((seg, i) => (
                                                <span key={`${seg}:${i}`} className="flex items-center gap-1">
                                                    <span>/</span>
                                                    <button
                                                        type="button"
                                                        className="hover:text-text-primary"
                                                        onClick={() => setBrowsePath(crumbs.slice(0, i + 1).join('/'))}
                                                    >
                                                        {seg}
                                                    </button>
                                                </span>
                                            ))}
                                        </div>
                                        {dirs.error ? (
                                            <div className="text-xs text-error">
                                                {t('目录加载失败', 'Failed to load directories')} — {dirs.error.message}
                                            </div>
                                        ) : null}
                                        {dirs.data && dirs.data.available === false ? (
                                            <div className="text-xs text-text-secondary">
                                                {t(
                                                    `系统工作目录根不存在: ${dirs.data.root} — 设置 FUSION_COWORK_WORKSPACE_ROOT 后重试`,
                                                    `System directory root missing: ${dirs.data.root} — set FUSION_COWORK_WORKSPACE_ROOT and retry`
                                                )}
                                            </div>
                                        ) : null}
                                        <div className="flex-1 overflow-y-auto">
                                            {dirs.loading && !dirs.data ? (
                                                <div className="py-4 text-center text-xs text-text-muted">
                                                    {t('加载中…', 'Loading…')}
                                                </div>
                                            ) : (
                                                <div className="flex flex-col gap-1">
                                                    {selectable.length === 0 ? (
                                                        <div className="py-4 text-center text-xs text-text-muted">
                                                            {t(
                                                                '没有子目录了 — 可直接选择当前目录',
                                                                'No subdirectories — pick the current directory'
                                                            )}
                                                        </div>
                                                    ) : (
                                                        selectable.map((d) => (
                                                            <button
                                                                type="button"
                                                                key={d.relative}
                                                                className="rounded-md px-2 py-1.5 text-left text-sm text-text-primary hover:bg-bg-muted"
                                                                onClick={() => setBrowsePath(d.relative)}
                                                            >
                                                                📁 {d.name}
                                                            </button>
                                                        ))
                                                    )}
                                                </div>
                                            )}
                                        </div>
                                        <div className="flex items-center justify-between">
                                            <button
                                                type="button"
                                                className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:text-text-primary disabled:opacity-40"
                                                disabled={!crumbs.length}
                                                onClick={() => setBrowsePath(crumbs.slice(0, -1).join('/'))}
                                            >
                                                {t('返回上级', 'Up one level')}
                                            </button>
                                            <button
                                                type="button"
                                                className="rounded-md bg-brand px-3 py-1.5 text-xs text-white disabled:opacity-50"
                                                disabled={!dirs.data?.available || currentUsed || dirs.loading}
                                                title={
                                                    currentUsed
                                                        ? t('已存在同名工作区', 'A workspace with this name exists')
                                                        : undefined
                                                }
                                                onClick={() => {
                                                    setPicked({
                                                        name: currentName,
                                                        relative: dirs.data?.relative ?? '',
                                                    });
                                                    setPickerOpen(false);
                                                }}
                                            >
                                                {currentUsed
                                                    ? t('当前目录已被使用', 'Current directory already in use')
                                                    : t('选择当前目录', 'Pick current directory')}
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            ) : null}
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
                                disabled={!picked || creating}
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
    const capsCatalog = useAutoRetry(() => api.capabilities(activeId), [activeId]);
    const approvals = useAutoRetry(() => api.pendingApprovals(), []);
    const [prompt, setPrompt] = useState('');
    const [preview, setPreview] = useState<PlanPreview | null>(null);
    const [busy, setBusy] = useState('');
    const [error, setError] = useState('');
    const [caps, setCaps] = useState<string[]>([]);
    // issue #226: 派生新任务 — run 视图预填 (任务 + 上次结果上下文), 一次性消费
    const [deriveFrom, setDeriveFrom] = useState('');
    // issue #231 (FCS-1): 五区 composer — 附件 chips + 模型/能力进 ⚙ 参数弹层
    const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
    const [model, setModel] = useState(preferredModel());
    const modelsRes = useAutoRetry(() => api.models(), []);
    const artifactsRes = useAutoRetry(
        () => (activeId ? api.spaceArtifacts(activeId) : Promise.resolve([])),
        [activeId]
    );
    // issue #237: 产物模块深链 ?artifact_ref=<id> — 解析成功注入 composer 与 run 上下文,
    // 失败显示可关闭提示 (不再静默注入假 ref)
    const [artifactRef, setArtifactRef] = useState<ArtifactRef | null>(null);
    const [artifactNotice, setArtifactNotice] = useState('');

    useEffect(() => {
        try {
            const raw = localStorage.getItem('cowork.derivePrefill');
            if (!raw) return;
            localStorage.removeItem('cowork.derivePrefill');
            const d = JSON.parse(raw) as { task?: string; outcome?: string; run_id?: string };
            const task = (d.task || '').trim();
            const outcome = (d.outcome || '').trim();
            setPrompt(
                task ? `在之前任务「${task}」的基础上继续${outcome ? `（上次结果：${outcome}）` : ''}，新的要求：` : ''
            );
            if (d.run_id) setDeriveFrom(d.run_id);
        } catch {
            // 坏数据忽略 — 不带预填进入
        }
    }, []);

    useEffect(() => {
        // issue #237: /shell/fusion-cowork/?artifact_ref=<id> (hash 前的 search 参数)。
        // 消费后立刻从 URL 抹除 — 刷新不重复注入; 失败显式提示且不注入上下文。
        const parsed = parseArtifactRefParam(window.location.search);
        if (!parsed.present) return;
        try {
            window.history.replaceState(null, '', window.location.pathname + window.location.hash);
        } catch {
            console.warn('[home] 无法从 URL 移除 artifact_ref 参数');
        }
        if (!parsed.valid) {
            setArtifactNotice(artifactRefFailureMessage('bad_id', t));
            return;
        }
        api.artifactCard(parsed.id).then(
            (r) => {
                const ref = artifactRefFromCard(r.artifact, parsed.id);
                setArtifactRef(ref);
                setPrompt(artifactRefPrompt(ref));
            },
            (e) => {
                console.warn('[home] artifact_ref 解析失败', parsed.id, e);
                setArtifactNotice(artifactRefFailureMessage(artifactRefFailureKind(e), t));
            }
        );
    }, [t]);

    const space = (ws.data?.workspaces ?? []).find((w) => w.id === activeId);
    const pendingCount = (approvals.data?.approvals ?? []).length;
    const runList = runs.data?.runs ?? [];

    function promptWithContext(): string {
        const extras: string[] = [];
        if (caps.length > 0) extras.push(`(可用能力: ${caps.join(', ')})`);
        if (attachments.length > 0) {
            const names = attachments.map((a) => a.label).join(', ');
            extras.push(`(附件: ${names})`);
            console.log('[home] composer attachments in task context:', names);
        }
        return extras.length > 0 ? `${prompt}\n\n${extras.join('\n')}` : prompt;
    }

    // issue #231 (FCS-1 §3): ＠ 引用面板 — 目录/任务/产物三类引用插入 prompt
    function insertRef(token: string) {
        setPrompt((p) => insertRefToken(p, token));
    }

    function toggleCap(name: string) {
        setCaps((prev) => (prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name]));
    }

    const referenceItems: ComposerActionItem[] = buildReferenceItems(
        [
            ...(ws.data?.workspaces ?? []).map((w) => ({ kind: 'directory' as const, id: w.id, name: w.name })),
            ...runList.slice(0, 8).map((r) => ({
                kind: 'task' as const,
                id: r.run_id,
                name: r.workflow_name || r.run_id.slice(0, 8),
            })),
            ...(artifactsRes.data ?? [])
                .slice(0, 8)
                .map((a) => ({ kind: 'artifact' as const, id: a.id, name: a.name })),
        ],
        insertRef
    );

    const modelOptions = (modelsRes.data?.models ?? []).map((m) => ({
        value: m.id,
        label: m.id,
        hint: m.id,
        curated: true,
    }));
    if (model && !modelOptions.some((o) => o.value === model)) {
        modelOptions.unshift({ value: model, label: model, hint: model, curated: true });
    }

    // FCS-1 ① 上下文条: 工作目录 + 派生/产物一次性 chip (点击清除)
    const contextChips: ComposerChip[] = buildContextChips({
        dirLabel: space?.name ?? activeId,
        onSwitchDir: onSwitch,
        deriveFrom,
        deriveLabel: t('派生自', 'Derived from'),
        onClearDerive: () => setDeriveFrom(''),
        artifactRefName: artifactRef?.name ?? '',
        artifactFromArtifacts: artifactRef !== null,
        onClearArtifact: () => {
            // 未编辑时一并清掉注入的提示词; 用户改过则保留其编辑内容
            if (artifactRef) setPrompt((p) => (p === artifactRefPrompt(artifactRef) ? '' : p));
            setArtifactRef(null);
        },
    });

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
                ...artifactRunContext(artifactRef),
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

    // issue #228: capability picker reads the workspace pool.
    const skillNames = poolSkillNames(capsCatalog.data);
    const poolExtras = poolExtraCapabilities(capsCatalog.data);

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
            <Composer
                value={prompt}
                onChange={setPrompt}
                onSubmit={() => void generatePreview()}
                busy={busy !== ''}
                placeholder={t(
                    '描述任务，如 "把 10 月对账跑一遍，异常项整理成报告"，/ 命令，@ 引用',
                    'Describe a task, e.g. "run the October reconciliation and report anomalies", / commands, @ references'
                )}
                ariaLabel={t('任务输入框', 'Task composer')}
                hint={t('Enter 生成计划 · Shift+Enter 换行', 'Enter to plan · Shift+Enter new line')}
                stopHint={t('· 生成中', '· generating')}
                sendVariant="pill"
                sendLabel={t('生成计划', 'Plan')}
                contextChips={contextChips}
                attachments={attachments}
                onRemoveAttachment={(key) => setAttachments((prev) => prev.filter((a) => a.key !== key))}
                attach={{
                    onFiles: (files) => {
                        setAttachments((prev) => [
                            ...prev,
                            ...files.map((f, i) => ({ key: `${f.name}-${Date.now()}-${i}`, label: f.name })),
                        ]);
                    },
                }}
                references={{ items: referenceItems, emptyLabel: t('暂无可引用项', 'No references yet') }}
                params={{
                    model: {
                        value: model,
                        onChange: (v) => {
                            setModel(v);
                            setPreferredModel(v);
                        },
                        options: modelOptions,
                        fallbackLabel: t('默认模型', 'Default model'),
                    },
                    actions: {
                        title: t('能力', 'Capabilities'),
                        items: [
                            ...skillNames.map((n) => ({
                                key: n,
                                label: `${caps.includes(n) ? '✓' : '○'} ⚙ ${n}`,
                                onClick: () => toggleCap(n),
                            })),
                            ...poolExtras.map((i) => ({
                                key: i.id,
                                label: `${caps.includes(i.name) ? '✓' : '○'} ${i.type === 'connector' ? '🔌' : '🤖'} ${
                                    i.title || i.name
                                }`,
                                onClick: () => toggleCap(i.name),
                            })),
                        ],
                        emptyLabel: t('暂无可用能力', 'No capabilities'),
                    },
                }}
            />

            {artifactNotice ? (
                <div
                    role="alert"
                    className="mt-2 flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-xs text-text-secondary"
                >
                    <span className="flex-1">{artifactNotice}</span>
                    <button
                        type="button"
                        aria-label={t('关闭提示', 'Dismiss')}
                        className="shrink-0 rounded px-1 text-text-muted hover:bg-bg-muted hover:text-text-primary"
                        onClick={() => setArtifactNotice('')}
                    >
                        ×
                    </button>
                </div>
            ) : null}

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
