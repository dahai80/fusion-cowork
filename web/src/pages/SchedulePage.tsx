import { ConfirmDialog, EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type SchedulerTask, api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W6: scheduled tasks (cron/interval) — list, create, pause/resume, delete.
// Calendar layout is a later batch; list covers the P0 data surface.
// Data: /api/scheduler/tasks.

function fmtTime(ts: number | null, lang: string): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US');
}

export function SchedulePage() {
    const t = useT();
    const retry = useAutoRetry(() => api.schedulerTasks(), []);
    const [confirmId, setConfirmId] = useState('');
    const [busy, setBusy] = useState('');
    const [form, setForm] = useState({ name: '', workflow_id: '', cron: '', interval_minutes: '' });
    const [workflowOptions, setWorkflowOptions] = useState<{ id: string; name: string }[]>([]);

    useEffect(() => {
        api.workflows()
            .then((d) => setWorkflowOptions(d.workflows.map((w) => ({ id: w.id, name: w.name }))))
            .catch(() => undefined);
    }, []);

    const tasks = retry.data?.tasks ?? [];

    async function create() {
        if (busy) return;
        if (!form.name.trim() || !form.workflow_id) {
            sdk.toast.show({ level: 'warning', message: t('名称与工作流必填', 'Name and workflow required') });
            return;
        }
        if (!form.cron.trim() && !form.interval_minutes.trim()) {
            sdk.toast.show({ level: 'warning', message: t('cron 或间隔分钟必填其一', 'cron or interval required') });
            return;
        }
        setBusy('create');
        try {
            const body: { name: string; workflow_id: string; cron?: string; interval_minutes?: number } = {
                name: form.name.trim(),
                workflow_id: form.workflow_id,
            };
            if (form.cron.trim()) body.cron = form.cron.trim();
            if (form.interval_minutes.trim()) body.interval_minutes = Number(form.interval_minutes);
            await api.createSchedulerTask(body);
            sdk.toast.show({ level: 'success', message: t('已创建定时任务', 'Task created') });
            setForm({ name: '', workflow_id: '', cron: '', interval_minutes: '' });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] schedule create failed', e);
            sdk.toast.show({ level: 'error', message: t('创建失败', 'Create failed') });
        } finally {
            setBusy('');
        }
    }

    async function toggle(task: SchedulerTask) {
        if (busy) return;
        setBusy(task.id);
        try {
            if (task.status === 'active') {
                await api.pauseSchedulerTask(task.id);
            } else {
                await api.resumeSchedulerTask(task.id);
            }
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] schedule toggle failed', e);
            sdk.toast.show({ level: 'error', message: t('操作失败', 'Operation failed') });
        } finally {
            setBusy('');
        }
    }

    async function remove() {
        if (!confirmId || busy) return;
        setBusy(confirmId);
        try {
            await api.deleteSchedulerTask(confirmId);
            sdk.toast.show({ level: 'success', message: t('已删除', 'Deleted') });
            setConfirmId('');
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] schedule delete failed', e);
            sdk.toast.show({ level: 'error', message: t('删除失败', 'Delete failed') });
        } finally {
            setBusy('');
        }
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="mb-3 rounded-lg border border-border bg-bg-elevated p-3">
                <div className="pb-2 text-sm font-medium text-text-primary">
                    {t('新建定时任务', 'New scheduled task')}
                </div>
                <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                    <input
                        className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                        placeholder={t('任务名称', 'Task name')}
                        value={form.name}
                        onChange={(e) => setForm({ ...form, name: e.target.value })}
                    />
                    <select
                        className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                        value={form.workflow_id}
                        onChange={(e) => setForm({ ...form, workflow_id: e.target.value })}
                    >
                        <option value="">{t('选择工作流', 'Pick workflow')}</option>
                        {workflowOptions.map((w) => (
                            <option key={w.id} value={w.id}>
                                {w.name}
                            </option>
                        ))}
                    </select>
                    <input
                        className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                        placeholder="cron (0 9 * * *)"
                        value={form.cron}
                        onChange={(e) => setForm({ ...form, cron: e.target.value })}
                    />
                    <div className="flex gap-2">
                        <input
                            className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                            placeholder={t('间隔分钟', 'Interval min')}
                            value={form.interval_minutes}
                            onChange={(e) => setForm({ ...form, interval_minutes: e.target.value })}
                        />
                        <button
                            type="button"
                            className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                            disabled={!!busy}
                            onClick={() => void create()}
                        >
                            {busy === 'create' ? t('创建中…', 'Creating…') : t('创建', 'Create')}
                        </button>
                    </div>
                </div>
            </div>

            {retry.loading && !retry.data ? (
                <SkeletonLines rows={5} />
            ) : retry.error ? (
                <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />
            ) : tasks.length === 0 ? (
                <EmptyState
                    title={t('暂无定时任务', 'No scheduled tasks')}
                    description={t('创建后按 cron/间隔自动运行', 'Runs automatically by cron/interval')}
                />
            ) : (
                <div className="space-y-2">
                    {tasks.map((task) => (
                        <div
                            key={task.id}
                            className="flex items-center gap-3 rounded-lg border border-border bg-bg-elevated p-3"
                        >
                            <StatusBadge status={statusKind(task.status === 'active' ? 'running' : 'offline')} />
                            <div className="min-w-0 flex-1">
                                <div className="truncate text-sm font-medium text-text-primary">{task.name}</div>
                                <div className="truncate text-xs text-text-muted">
                                    {task.trigger_type === 'cron'
                                        ? `cron ${String(task.trigger_config?.cron ?? '')}`
                                        : `${t('每', 'every')} ${String(task.trigger_config?.minutes ?? '')} min`}{' '}
                                    · {t('下次', 'next')} {fmtTime(task.next_run, sdk.i18n.lang())}
                                </div>
                            </div>
                            <div className="shrink-0 font-mono text-[10px] text-text-muted">
                                ✔ {task.run_count} / ✘ {task.fail_count}
                            </div>
                            <div className="flex shrink-0 gap-1">
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                    disabled={!!busy}
                                    onClick={() => void toggle(task)}
                                >
                                    {task.status === 'active' ? t('暂停', 'Pause') : t('恢复', 'Resume')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-error hover:bg-error/10"
                                    disabled={!!busy}
                                    onClick={() => setConfirmId(task.id)}
                                >
                                    {t('删除', 'Delete')}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}
            <ConfirmDialog
                open={!!confirmId}
                title={t('删除定时任务', 'Delete scheduled task')}
                description={confirmId ? (tasks.find((x) => x.id === confirmId)?.name ?? confirmId) : ''}
                danger
                confirmName={confirmId}
                onConfirm={() => void remove()}
                onCancel={() => setConfirmId('')}
            />
        </div>
    );
}
