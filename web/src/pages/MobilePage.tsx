import { EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W22 mobile surface: run status cards + pending approvals + deliver-to-
// desktop. issue #154 adds cross-device takeover controls (pause/resume/
// cancel + approval decisions) over the same REST seam the WS takeover uses.
// Delivery needs cross_sync on the server (503 → honest toast). Route #/m.

export function MobilePage() {
    const t = useT();
    const retry = useAutoRetry(() => api.mobileOverview(), []);
    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const ov = retry.data;

    async function deliver(runId: string) {
        try {
            const res = await api.deliver(runId);
            if (res.delivered) {
                sdk.toast.show({ level: 'success', message: t('已投递到桌面', 'Delivered to desktop') });
            } else {
                sdk.toast.show({
                    level: 'warning',
                    message: t('桌面设备不在线, 未送达', 'Desktop device offline, not delivered'),
                });
            }
        } catch (e) {
            console.error('[cowork-web] deliver failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('投递失败 (同步未启用)', 'Deliver failed (sync not enabled)'),
            });
        }
    }

    async function takeover(runId: string, action: 'pause' | 'resume' | 'cancel') {
        try {
            if (action === 'pause') await api.pauseRun(runId);
            else if (action === 'resume') await api.resumeRun(runId);
            else await api.cancelRun(runId);
            const label = t(
                action === 'pause' ? '已暂停' : action === 'resume' ? '已恢复' : '已取消',
                action === 'pause' ? 'Paused' : action === 'resume' ? 'Resumed' : 'Cancelled'
            );
            sdk.toast.show({ level: 'success', message: label });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] takeover failed', action, e);
            sdk.toast.show({ level: 'error', message: t('接管操作失败', 'Takeover action failed') });
        }
    }

    async function decide(actionId: string, approved: boolean) {
        try {
            await api.confirmApproval(actionId, approved);
            sdk.toast.show({
                level: 'success',
                message: approved ? t('已允许', 'Approved') : t('已拒绝', 'Denied'),
            });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] approval decision failed', e);
            sdk.toast.show({ level: 'error', message: t('审批提交失败', 'Approval failed') });
        }
    }

    return (
        <div className="mx-auto max-w-md space-y-3">
            <div className="text-sm font-medium text-text-primary">{t('运行概览', 'Runs overview')}</div>
            {ov.runs.length === 0 ? (
                <EmptyState title={t('暂无运行', 'No runs')} />
            ) : (
                ov.runs.map((r) => (
                    <div key={r.run_id} className="rounded-lg border border-border bg-bg-elevated p-3">
                        <div className="flex items-center justify-between pb-1">
                            <div className="min-w-0 flex-1 truncate text-sm text-text-primary">{r.workflow_name}</div>
                            <StatusBadge status={statusKind(r.status)} label={r.status} />
                        </div>
                        <div className="pb-2 font-mono text-[10px] text-text-muted">{r.run_id}</div>
                        <button
                            type="button"
                            className="w-full rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                            onClick={() => void deliver(r.run_id)}
                        >
                            {t('投递到桌面', 'Deliver to desktop')}
                        </button>
                        {(r.status === 'running' || r.status === 'paused') && (
                            <div className="flex gap-2 pt-2">
                                {r.status === 'running' && (
                                    <button
                                        type="button"
                                        className="flex-1 rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                                        onClick={() => void takeover(r.run_id, 'pause')}
                                    >
                                        {t('暂停', 'Pause')}
                                    </button>
                                )}
                                {r.status === 'paused' && (
                                    <button
                                        type="button"
                                        className="flex-1 rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                                        onClick={() => void takeover(r.run_id, 'resume')}
                                    >
                                        {t('恢复', 'Resume')}
                                    </button>
                                )}
                                <button
                                    type="button"
                                    className="flex-1 rounded-md border border-border px-2 py-1 text-xs text-error hover:bg-bg-muted"
                                    onClick={() => void takeover(r.run_id, 'cancel')}
                                >
                                    {t('取消', 'Cancel')}
                                </button>
                            </div>
                        )}
                    </div>
                ))
            )}
            <div className="pt-1 text-sm font-medium text-text-primary">
                {t('待审批', 'Pending approvals')} ({ov.pending_approvals.length})
            </div>
            {ov.pending_approvals.length === 0 ? (
                <div className="text-xs text-text-muted">{t('无待审批项', 'Nothing pending')}</div>
            ) : (
                ov.pending_approvals.map((a) => (
                    <div key={a.action_id} className="rounded-md border border-border bg-bg-elevated px-3 py-2">
                        <div className="flex items-center justify-between pb-2">
                            <span className="font-mono text-xs text-text-primary">{a.tool_name}</span>
                            <span className="font-mono text-[10px] text-text-muted">{a.action_id}</span>
                        </div>
                        <div className="flex gap-2">
                            <button
                                type="button"
                                className="flex-1 rounded-md border border-border px-2 py-1 text-xs text-error hover:bg-bg-muted"
                                onClick={() => void decide(a.action_id, false)}
                            >
                                {t('拒绝', 'Deny')}
                            </button>
                            <button
                                type="button"
                                className="flex-1 rounded-md border border-border px-2 py-1 text-xs text-success hover:bg-bg-muted"
                                onClick={() => void decide(a.action_id, true)}
                            >
                                {t('允许', 'Allow')}
                            </button>
                        </div>
                    </div>
                ))
            )}
        </div>
    );
}
