import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import {
    type DemandCapability,
    type DemandCatalog,
    type DemandLead,
    type DemandSettings,
    api,
    statusKind,
} from '../api';
import { useT } from '../i18n';

// issue #596: demand operator GUI — inbox (lead review + dual-draft approve),
// catalog editor, IMAP settings, pipeline (read-only wiring to scheduler/runs).
// Human sends on the original channel — the system drafts and approves, never sends.

const LEAD_STATUSES = ['drafted', 'matched', 'parked', 'spam', 'discarded', 'approved', 'normalized'] as const;

type Tab = 'inbox' | 'catalog' | 'settings' | 'pipeline';

const STATUS_CHIP: Record<string, string> = {
    drafted: 'bg-indigo-500/15 text-indigo-600 dark:text-indigo-400',
    approved: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
    parked: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
    spam: 'bg-zinc-500/15 text-zinc-500',
    discarded: 'bg-zinc-500/15 text-zinc-500',
};

function Chip({ label, className = 'bg-bg-muted text-text-secondary' }: { label: string; className?: string }) {
    return <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ${className}`}>{label}</span>;
}

function InboxPane({ t }: { t: (zh: string, en: string) => string }) {
    const [filter, setFilter] = useState('');
    const retry = useAutoRetry(() => api.demandLeads(filter), [filter]);
    const metrics = useAutoRetry(() => api.demandMetrics(), []);
    const [selected, setSelected] = useState<DemandLead | null>(null);
    const [editing, setEditing] = useState('');
    const [editingOn, setEditingOn] = useState(false);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [notice, setNotice] = useState<string | null>(null);

    async function act(fn: () => Promise<DemandLead>, done: string) {
        setBusy(true);
        setError(null);
        setNotice(null);
        try {
            const updated = await fn();
            setSelected(updated);
            setNotice(done);
            setEditingOn(false);
            retry.manualRetry();
            metrics.manualRetry();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const leads = retry.data.leads;

    return (
        <div className="space-y-3">
            {metrics.data ? (
                <div className="flex flex-wrap gap-2 text-xs text-text-secondary">
                    <Chip label={`${t('审批', 'Approvals')}: ${metrics.data.total_picks}`} />
                    <Chip
                        label={`${t('本地稿胜率', 'Local pick')}: ${(metrics.data.pick_rate_local * 100).toFixed(0)}%`}
                    />
                    <Chip
                        label={`${t('云稿胜率', 'Cloud pick')}: ${(metrics.data.pick_rate_cloud * 100).toFixed(0)}%`}
                    />
                    <Chip label={`${t('平均编辑距离', 'Avg edit distance')}: ${metrics.data.avg_edit_distance}`} />
                </div>
            ) : null}
            <div className="flex flex-wrap gap-1">
                <button
                    type="button"
                    className={`rounded-md px-2 py-1 text-xs ${filter === '' ? 'bg-bg-muted text-text-primary' : 'text-text-secondary hover:bg-bg-muted'}`}
                    onClick={() => setFilter('')}
                >
                    {t('全部', 'All')}
                </button>
                {LEAD_STATUSES.map((s) => (
                    <button
                        type="button"
                        key={s}
                        className={`rounded-md px-2 py-1 text-xs ${filter === s ? 'bg-bg-muted text-text-primary' : 'text-text-secondary hover:bg-bg-muted'}`}
                        onClick={() => setFilter(s)}
                    >
                        {s}
                    </button>
                ))}
            </div>
            <div className="grid gap-3 lg:grid-cols-[280px_1fr]">
                <div className="max-h-[28rem] space-y-1 overflow-y-auto rounded-xl border border-border p-2">
                    {leads.length === 0 ? (
                        <EmptyState title={t('暂无线索', 'No leads')} />
                    ) : (
                        leads.map((ld) => (
                            <button
                                type="button"
                                key={ld.id}
                                className={`w-full rounded-lg px-2 py-1.5 text-left text-xs hover:bg-bg-muted ${
                                    selected?.id === ld.id ? 'bg-bg-muted' : ''
                                }`}
                                onClick={() => {
                                    setSelected(ld);
                                    setEditingOn(false);
                                    setNotice(null);
                                    setError(null);
                                }}
                            >
                                <div className="flex items-center justify-between gap-2">
                                    <span className="truncate font-medium text-text-primary">
                                        #{ld.id} {ld.title || '—'}
                                    </span>
                                    <Chip label={ld.status} className={STATUS_CHIP[ld.status]} />
                                </div>
                                <div className="mt-0.5 flex gap-2 text-text-muted">
                                    <span>{ld.source}</span>
                                    <span>
                                        {t('分', 'score')} {ld.match_score ?? '—'}
                                    </span>
                                </div>
                            </button>
                        ))
                    )}
                </div>
                <div className="rounded-xl border border-border p-3">
                    {!selected ? (
                        <EmptyState title={t('选择左侧线索查看双稿', 'Select a lead to review drafts')} />
                    ) : (
                        <div className="space-y-3 text-xs">
                            <div className="flex items-center justify-between gap-2">
                                <span className="text-sm font-medium text-text-primary">
                                    #{selected.id} {selected.title}
                                </span>
                                <Chip label={selected.status} className={STATUS_CHIP[selected.status]} />
                            </div>
                            <div className="text-text-secondary">
                                {t('能力', 'Capabilities')}: {(selected.matched_capabilities ?? []).join(', ') || '—'} ·{' '}
                                {t('目录', 'Catalog')} v{selected.catalog_version || '—'} · {t('预算', 'Budget')}:{' '}
                                {selected.budget_hint || '—'}
                            </div>
                            {selected.match_reason ? (
                                <div className="text-text-muted">{selected.match_reason}</div>
                            ) : null}
                            {notice ? (
                                <div className="rounded-lg bg-emerald-500/10 px-2 py-1 text-emerald-600 dark:text-emerald-400">
                                    {notice}
                                </div>
                            ) : null}
                            {error ? (
                                <div className="rounded-lg bg-red-500/10 px-2 py-1 text-red-600 dark:text-red-400">
                                    {error}
                                </div>
                            ) : null}
                            {selected.status === 'drafted' && selected.draft_a && selected.draft_b ? (
                                <>
                                    <div className="grid gap-3 md:grid-cols-2">
                                        {(['a', 'b'] as const).map((slot) => (
                                            <div key={slot} className="space-y-1 rounded-lg border border-border p-2">
                                                <div className="flex items-center justify-between">
                                                    <span className="font-medium text-text-primary">
                                                        {slot === 'a'
                                                            ? t('草稿 A (本地)', 'Draft A (local)')
                                                            : t('草稿 B (云端)', 'Draft B (cloud)')}
                                                    </span>
                                                    <span className="text-text-muted">
                                                        {slot === 'a'
                                                            ? selected.draft_meta?.model_a
                                                            : selected.draft_meta?.model_b}
                                                        {slot === 'a'
                                                            ? selected.draft_meta?.latency_a_ms
                                                            : selected.draft_meta?.latency_b_ms}
                                                    </span>
                                                </div>
                                                <p className="whitespace-pre-wrap text-text-secondary">
                                                    {slot === 'a' ? selected.draft_a : selected.draft_b}
                                                </p>
                                                <button
                                                    type="button"
                                                    disabled={busy}
                                                    className="rounded-md bg-indigo-600 px-2 py-1 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                                                    onClick={() =>
                                                        act(
                                                            () => api.approveDemandLead(selected.id, slot),
                                                            t(
                                                                '已批准 — 请在原渠道人工发送',
                                                                'Approved — send manually on the original channel'
                                                            )
                                                        )
                                                    }
                                                >
                                                    {t('批准此稿', 'Approve')}
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                    {editingOn ? (
                                        <div className="space-y-1">
                                            <textarea
                                                aria-label={t('编辑后的最终稿', 'Edited final draft')}
                                                className="h-32 w-full rounded-lg border border-border bg-bg-elevated p-2 text-text-primary"
                                                value={editing}
                                                onChange={(e) => setEditing(e.target.value)}
                                            />
                                            <div className="flex gap-2">
                                                <button
                                                    type="button"
                                                    disabled={busy || !editing.trim()}
                                                    className="rounded-md bg-indigo-600 px-2 py-1 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                                                    onClick={() =>
                                                        act(
                                                            () => api.approveDemandLead(selected.id, 'a', editing),
                                                            t(
                                                                '已批准编辑稿 — 请在原渠道人工发送',
                                                                'Approved edited draft — send manually on the original channel'
                                                            )
                                                        )
                                                    }
                                                >
                                                    {t('保存并批准', 'Save and approve')}
                                                </button>
                                                <button
                                                    type="button"
                                                    className="rounded-md px-2 py-1 text-text-secondary hover:underline"
                                                    onClick={() => setEditingOn(false)}
                                                >
                                                    {t('取消', 'Cancel')}
                                                </button>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="flex gap-2">
                                            <button
                                                type="button"
                                                className="rounded-md border border-border px-2 py-1 text-text-secondary hover:bg-bg-muted"
                                                onClick={() => {
                                                    setEditing(selected.draft_a ?? '');
                                                    setEditingOn(true);
                                                }}
                                            >
                                                {t('编辑后批准', 'Edit then approve')}
                                            </button>
                                            <button
                                                type="button"
                                                disabled={busy}
                                                className="rounded-md border border-border px-2 py-1 text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                                                onClick={() =>
                                                    act(
                                                        () => api.discardDemandLead(selected.id, 'operator discarded'),
                                                        t('已丢弃', 'Discarded')
                                                    )
                                                }
                                            >
                                                {t('丢弃', 'Discard')}
                                            </button>
                                        </div>
                                    )}
                                </>
                            ) : selected.status === 'approved' ? (
                                <div className="rounded-lg bg-bg-muted px-2 py-1.5 text-text-secondary">
                                    {t(
                                        '已批准 — 系统不会自动发送, 请在原渠道人工提交',
                                        'Approved — the system never sends; submit manually on the original channel'
                                    )}
                                </div>
                            ) : (
                                <div className="text-text-muted">
                                    {t('该线索当前无可审批草稿', 'No draft to review at this status')}
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}

function CatalogPane({ t }: { t: (zh: string, en: string) => string }) {
    const retry = useAutoRetry(() => api.demandCatalog(), []);
    const [rows, setRows] = useState<DemandCapability[] | null>(null);
    const [result, setResult] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);

    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const catalog: DemandCatalog = retry.data;
    const capabilities = rows ?? catalog.capabilities;

    async function save() {
        setSaving(true);
        setError(null);
        try {
            const r = await api.saveDemandCatalog({ ...catalog, capabilities });
            setResult(
                t(
                    `已保存 v${r.version} (原 v${r.previous_version}), ${r.parked_re_scored} 条 parked 线索已重新评估`,
                    `Saved v${r.version} (was v${r.previous_version}), ${r.parked_re_scored} parked leads re-scored`
                )
            );
            rows && setRows(null);
            retry.manualRetry();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setSaving(false);
        }
    }

    return (
        <div className="space-y-3 text-xs">
            <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-text-primary">
                    {t('能力目录', 'Capability catalog')} v{catalog.version}
                </span>
                <button
                    type="button"
                    disabled={saving}
                    className="rounded-md bg-indigo-600 px-3 py-1 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                    onClick={save}
                >
                    {t('保存 (版本号自动递增)', 'Save (version auto-bump)')}
                </button>
            </div>
            {result ? (
                <div className="rounded-lg bg-emerald-500/10 px-2 py-1 text-emerald-600 dark:text-emerald-400">
                    {result}
                </div>
            ) : null}
            {error ? (
                <div className="rounded-lg bg-red-500/10 px-2 py-1 text-red-600 dark:text-red-400">{error}</div>
            ) : null}
            <div className="overflow-x-auto rounded-xl border border-border">
                <table className="w-full text-left">
                    <thead className="bg-bg-muted text-text-secondary">
                        <tr>
                            <th className="px-2 py-1.5">ID</th>
                            <th className="px-2 py-1.5">{t('名称', 'Name')}</th>
                            <th className="px-2 py-1.5">{t('报价 (CNY)', 'Price (CNY)')}</th>
                            <th className="px-2 py-1.5">{t('预算区间', 'Budget range')}</th>
                            <th className="px-2 py-1.5">{t('关键词', 'Keywords')}</th>
                            <th className="px-2 py-1.5">{t('启用', 'Enabled')}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {capabilities.map((c, i) => (
                            <tr key={c.id} className="border-t border-border">
                                <td className="px-2 py-1.5 font-mono">{c.id}</td>
                                <td className="px-2 py-1.5">{c.name_zh || c.name_en || '—'}</td>
                                <td className="px-2 py-1.5">
                                    <input
                                        aria-label={`${c.id} price`}
                                        type="number"
                                        className="w-20 rounded-md border border-border bg-bg px-1 py-0.5 text-text-primary"
                                        value={c.price_cny ?? 0}
                                        onChange={(e) => {
                                            const next = [...capabilities];
                                            next[i] = { ...c, price_cny: Number(e.target.value) };
                                            setRows(next);
                                        }}
                                    />
                                </td>
                                <td className="px-2 py-1.5 text-text-secondary">
                                    {c.budget_min_cny ?? '—'} – {c.budget_max_cny ?? '—'}
                                </td>
                                <td
                                    className="max-w-xs truncate px-2 py-1.5 text-text-muted"
                                    title={(c.keywords ?? []).join(', ')}
                                >
                                    {(c.keywords ?? []).join(', ')}
                                </td>
                                <td className="px-2 py-1.5">
                                    <input
                                        aria-label={`${c.id} enabled`}
                                        type="checkbox"
                                        checked={c.enabled !== false}
                                        onChange={(e) => {
                                            const next = [...capabilities];
                                            next[i] = { ...c, enabled: e.target.checked };
                                            setRows(next);
                                        }}
                                    />
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

function SettingsPane({ t }: { t: (zh: string, en: string) => string }) {
    const retry = useAutoRetry(() => api.demandSettings(), []);
    const [form, setForm] = useState<(Partial<DemandSettings> & { password?: string }) | null>(null);
    const [saved, setSaved] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [testResult, setTestResult] = useState<string | null>(null);
    const [busy, setBusy] = useState(false);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const s: DemandSettings = retry.data;
    const f = form ?? { host: s.host, port: s.port, user: s.user, folder: s.folder, password: '' };

    async function save() {
        setBusy(true);
        setError(null);
        setSaved(null);
        try {
            await api.saveDemandSettings({
                host: f.host,
                port: Number(f.port) || undefined,
                user: f.user,
                folder: f.folder,
                ...(f.password ? { password: f.password } : {}),
            });
            setSaved(t('已保存 (授权码写后不回显)', 'Saved (auth code is write-only)'));
            setForm(null);
            retry.manualRetry();
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    async function test() {
        setBusy(true);
        setTestResult(null);
        try {
            const r = await api.testDemandSettings();
            setTestResult(
                r.ok
                    ? t(
                          `连接成功 (${r.latency_ms}ms, cursor ${r.cursor})`,
                          `OK (${r.latency_ms}ms, cursor ${r.cursor})`
                      )
                    : t(`连接失败: ${r.error ?? 'unknown'}`, `Failed: ${r.error ?? 'unknown'}`)
            );
            retry.manualRetry();
        } catch (e) {
            setTestResult(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy(false);
        }
    }

    const field = 'w-full rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary';

    return (
        <div className="grid gap-3 lg:grid-cols-2">
            <div className="space-y-2 rounded-xl border border-border p-3 text-xs">
                <div className="text-sm font-medium text-text-primary">{t('QQ IMAP 收件', 'QQ IMAP inbox')}</div>
                {(['host', 'port', 'user', 'folder'] as const).map((k) => (
                    <label key={k} className="block space-y-0.5">
                        <span className="text-text-secondary">{k}</span>
                        <input
                            aria-label={k}
                            className={field}
                            value={String(f[k] ?? '')}
                            onChange={(e) => setForm({ ...f, [k]: e.target.value })}
                        />
                    </label>
                ))}
                <label className="block space-y-0.5">
                    <span className="text-text-secondary">
                        {t('授权码', 'Auth code')}
                        {s.password_present ? t(' (已设置)', ' (set)') : ''}
                    </span>
                    <input
                        aria-label="password"
                        type="password"
                        className={field}
                        placeholder={s.password_present ? '********' : ''}
                        value={f.password ?? ''}
                        onChange={(e) => setForm({ ...f, password: e.target.value })}
                    />
                </label>
                <div className="flex gap-2">
                    <button
                        type="button"
                        disabled={busy}
                        className="rounded-md bg-indigo-600 px-3 py-1 font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                        onClick={save}
                    >
                        {t('保存', 'Save')}
                    </button>
                    <button
                        type="button"
                        disabled={busy}
                        className="rounded-md border border-border px-3 py-1 text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                        onClick={test}
                    >
                        {t('测试连接', 'Test connection')}
                    </button>
                </div>
                {saved ? <div className="text-emerald-600 dark:text-emerald-400">{saved}</div> : null}
                {error ? <div className="text-red-600 dark:text-red-400">{error}</div> : null}
                {testResult ? <div className="text-text-secondary">{testResult}</div> : null}
                <div className="text-text-muted">
                    {t('环境文件', 'Env file')}: {s.env_path} · UID cursor {s.cursor ?? 0}
                </div>
            </div>
            <div className="space-y-2 rounded-xl border border-border p-3 text-xs">
                <div className="text-sm font-medium text-text-primary">
                    {t('模型路由 (只读)', 'Model routing (read-only)')}
                </div>
                <div className="text-text-secondary">
                    {t('草稿 A: 本地模型 @ fusion-gateway :11432', 'Draft A: local model @ fusion-gateway :11432')}
                </div>
                <div className="text-text-secondary">
                    {t('草稿 B: 云端 glm-5 @ fusion-gateway :11432', 'Draft B: cloud glm-5 @ fusion-gateway :11432')}
                </div>
                <div className="text-text-muted">
                    {t(
                        '密钥通过 FUSION_CLIENT_KEY 注入 (已脱敏), 网关配置见管理后台',
                        'Keys injected via FUSION_CLIENT_KEY (masked); configure at the gateway admin console'
                    )}
                </div>
                <div className="text-text-muted">
                    {t('通知', 'Notify')}: ntfy (FUSION_NTFY_URL, {t('可选', 'optional')})
                </div>
            </div>
        </div>
    );
}

function PipelinePane({ t }: { t: (zh: string, en: string) => string }) {
    // issue #608: the seeded demand-mail-rail task is operator-actionable here —
    // next-run time, pause/resume, run-now, plus last N run results.
    const tasks = useAutoRetry(() => api.schedulerTasks(), []);
    const runs = useAutoRetry(() => api.runs(), []);
    const [busy, setBusy] = useState(false);
    const [notice, setNotice] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const demandTasks = (tasks.data?.tasks ?? []).filter((task) => /demand/i.test(task.workflow_id));
    const demandRuns = (runs.data?.runs ?? []).filter((run) => /demand/i.test(run.workflow_name));

    const act = async (fn: () => Promise<unknown>, ok: string) => {
        setBusy(true);
        setNotice(null);
        setError(null);
        try {
            await fn();
            setNotice(ok);
            tasks.manualRetry();
            runs.manualRetry();
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        } finally {
            setBusy(false);
        }
    };

    const fmtTime = (ts: number | null) => (ts ? new Date(ts * 1000).toLocaleString() : t('—', '—'));

    return (
        <div className="space-y-3 text-xs">
            <div className="rounded-xl border border-border p-3">
                <div className="mb-2 text-sm font-medium text-text-primary">
                    {t('需求邮件管线 (调度)', 'Demand mail rail (scheduled)')}
                </div>
                {tasks.loading && !tasks.data ? (
                    <SkeletonLines rows={2} />
                ) : demandTasks.length === 0 ? (
                    <EmptyState
                        title={t(
                            '未注册 demand 调度任务 — 重启 serve 自动播种',
                            'No demand scheduler task — seeded automatically on serve restart'
                        )}
                    />
                ) : (
                    <div className="space-y-2">
                        {demandTasks.map((task) => (
                            <div key={task.id} className="rounded-lg border border-border/60 p-2">
                                <div className="flex flex-wrap items-center gap-2 text-text-secondary">
                                    <span className="font-mono text-text-primary">{task.workflow_id}</span>
                                    <span className="truncate">{task.name}</span>
                                    {task.trigger_config?.cron ? (
                                        <Chip label={String(task.trigger_config.cron)} />
                                    ) : null}
                                    <Chip
                                        label={task.status}
                                        className={
                                            task.status === 'running' || task.status === 'active'
                                                ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                                                : ''
                                        }
                                    />
                                </div>
                                <div className="mt-1 flex flex-wrap items-center gap-3 text-text-muted">
                                    <span>
                                        {t('下次运行', 'Next run')}: {fmtTime(task.next_run)}
                                    </span>
                                    <span>
                                        {t('上次运行', 'Last run')}: {fmtTime(task.last_run)}
                                    </span>
                                    <span>
                                        {t('次数', 'runs')} {task.run_count} · {t('失败', 'fail')} {task.fail_count}
                                    </span>
                                </div>
                                <div className="mt-2 flex gap-2">
                                    <button
                                        type="button"
                                        disabled={busy}
                                        className="rounded-lg border border-border px-2 py-1 text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                                        onClick={() =>
                                            void act(
                                                () => api.runNowSchedulerTask(task.id),
                                                t('已触发运行', 'Run triggered')
                                            )
                                        }
                                    >
                                        {t('立即运行', 'Run now')}
                                    </button>
                                    {task.status === 'paused' || task.status === 'failed' ? (
                                        <button
                                            type="button"
                                            disabled={busy}
                                            className="rounded-lg border border-border px-2 py-1 text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                                            onClick={() =>
                                                void act(() => api.resumeSchedulerTask(task.id), t('已恢复', 'Resumed'))
                                            }
                                        >
                                            {t('恢复', 'Resume')}
                                        </button>
                                    ) : (
                                        <button
                                            type="button"
                                            disabled={busy}
                                            className="rounded-lg border border-border px-2 py-1 text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                                            onClick={() =>
                                                void act(() => api.pauseSchedulerTask(task.id), t('已暂停', 'Paused'))
                                            }
                                        >
                                            {t('暂停', 'Pause')}
                                        </button>
                                    )}
                                </div>
                            </div>
                        ))}
                    </div>
                )}
                {notice ? <div className="mt-2 text-emerald-600 dark:text-emerald-400">{notice}</div> : null}
                {error ? <div className="mt-2 text-red-600 dark:text-red-400">{error}</div> : null}
            </div>
            <div className="rounded-xl border border-border p-3">
                <div className="mb-2 text-sm font-medium text-text-primary">{t('最近运行', 'Recent runs')}</div>
                {runs.loading && !runs.data ? (
                    <SkeletonLines rows={2} />
                ) : demandRuns.length === 0 ? (
                    <EmptyState title={t('暂无 demand 运行记录', 'No demand runs yet')} />
                ) : (
                    <div className="space-y-1">
                        {demandRuns.slice(0, 8).map((run) => (
                            <div key={run.run_id} className="flex items-center gap-2 text-text-secondary">
                                <a
                                    className="font-mono text-indigo-600 hover:underline dark:text-indigo-400"
                                    href={`#/runs/${run.run_id}`}
                                >
                                    {run.run_id.slice(0, 8)}
                                </a>
                                <span className="truncate">{run.workflow_name}</span>
                                <Chip
                                    label={run.status}
                                    className={
                                        statusKind(run.status) === 'success'
                                            ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                                            : ''
                                    }
                                />
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

export function DemandPage() {
    const t = useT();
    const [tab, setTab] = useState<Tab>('inbox');
    const tabs: { key: Tab; label: string }[] = [
        { key: 'inbox', label: t('线索收件箱', 'Inbox') },
        { key: 'catalog', label: t('能力目录', 'Catalog') },
        { key: 'settings', label: t('设置', 'Settings') },
        { key: 'pipeline', label: t('管线', 'Pipeline') },
    ];
    return (
        <div className="mx-auto max-w-5xl space-y-3">
            <div className="flex gap-1">
                {tabs.map((item) => (
                    <button
                        type="button"
                        key={item.key}
                        className={`rounded-md px-3 py-1 text-sm ${tab === item.key ? 'bg-bg-muted text-text-primary' : 'text-text-secondary hover:bg-bg-muted'}`}
                        onClick={() => setTab(item.key)}
                    >
                        {item.label}
                    </button>
                ))}
            </div>
            {tab === 'inbox' ? (
                <InboxPane t={t} />
            ) : tab === 'catalog' ? (
                <CatalogPane t={t} />
            ) : tab === 'settings' ? (
                <SettingsPane t={t} />
            ) : (
                <PipelinePane t={t} />
            )}
        </div>
    );
}
