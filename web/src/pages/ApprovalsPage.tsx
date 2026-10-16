import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W19: guard L3 approval queue — pending high-risk actions delegated to
// fusion-guard (UDS /tmp/fusion-guard.sock) awaiting confirm. Guard must be
// enabled server-side (FUSION_GUARD_ENABLED=1) for confirms to land.
export function ApprovalsPage() {
    const t = useT();
    const [busyId, setBusyId] = useState('');
    const retry = useAutoRetry(() => api.pendingApprovals(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={4} />;
    if (retry.error) return <ErrorView message={retry.error.message} onRetry={retry.manualRetry} />;

    const approvals = retry.data?.approvals ?? [];

    async function confirm(actionId: string, approved: boolean) {
        if (busyId) return;
        setBusyId(actionId);
        try {
            const res = await api.confirmApproval(actionId, approved);
            if (res.confirmed) {
                sdk.toast.show({
                    level: 'success',
                    message: approved ? t('已批准', 'Approved') : t('已拒绝', 'Rejected'),
                });
            } else {
                sdk.toast.show({
                    level: 'warning',
                    message: t(
                        'confirm 未生效 (guard 未启用或队列已变)',
                        'Confirm not applied (guard disabled or queue changed)'
                    ),
                });
            }
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] approval confirm failed', e);
            sdk.toast.show({ level: 'error', message: t('操作失败', 'Operation failed') });
        } finally {
            setBusyId('');
        }
    }

    return (
        <div className="mx-auto max-w-3xl">
            <div className="flex items-end justify-between pb-2">
                <div className="text-sm font-medium text-text-primary">{t('审批队列', 'Approval queue')}</div>
                <div className="text-xs text-text-muted">{approvals.length}</div>
            </div>
            {approvals.length === 0 ? (
                <EmptyState
                    title={t('暂无待审批', 'Nothing pending')}
                    description={t('高风险操作 (guard L3) 出现时显示在此', 'High-risk actions (guard L3) appear here')}
                />
            ) : (
                <div className="space-y-1">
                    {approvals.map((a) => (
                        <div
                            key={a.action_id}
                            className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2 py-1.5"
                        >
                            <StatusBadge status="warning" label="L3" />
                            <span className="min-w-0 flex-1 truncate text-sm font-medium text-text-primary">
                                {a.tool_name}
                            </span>
                            <span className="shrink-0 font-mono text-[10px] text-text-muted">{a.action_id}</span>
                            <button
                                type="button"
                                className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                disabled={busyId === a.action_id}
                                onClick={() => void confirm(a.action_id, false)}
                            >
                                {t('拒绝', 'Reject')}
                            </button>
                            <button
                                type="button"
                                className="shrink-0 rounded-md bg-brand px-2 py-1 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                disabled={busyId === a.action_id}
                                onClick={() => void confirm(a.action_id, true)}
                            >
                                {t('批准', 'Approve')}
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}

function ErrorView({ message, onRetry }: { message: string; onRetry: () => void }) {
    const t = useT();
    return <ErrorState message={message || t('加载失败', 'Load failed')} onRetry={onRetry} />;
}
