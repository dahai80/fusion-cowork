import { EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W11: unified session list — engine sessions (workflow runs with session
// tracking). Data: GET /api/sessions. Space chat sessions come with the P2
// space surface (W12+), not mixed here.

function fmtTime(ts: number, lang: 'zh' | 'en'): string {
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

export function SessionsPage() {
    const t = useT();
    const lang = sdk.i18n.lang();
    const retry = useAutoRetry(() => api.sessions(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const sessions = retry.data?.sessions ?? [];

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-end justify-between pb-3">
                <div>
                    <div className="text-lg font-medium text-text-primary">{t('会话', 'Sessions')}</div>
                    <div className="text-xs text-text-muted">
                        {t('工作流执行会话记录', 'Workflow execution session records')}
                    </div>
                </div>
                <div className="text-xs text-text-muted">{sessions.length}</div>
            </div>
            {sessions.length === 0 ? (
                <EmptyState title={t('暂无会话', 'No sessions')} />
            ) : (
                <div className="space-y-2">
                    {sessions.map((s) => (
                        <div
                            key={s.id}
                            className="flex items-center gap-3 rounded-lg border border-border bg-bg-elevated p-3"
                        >
                            <StatusBadge status={statusKind(s.status) as StatusKind} label={s.status} />
                            <div className="min-w-0 flex-1">
                                <div className="truncate text-sm font-medium text-text-primary">
                                    {s.workflow_name || s.id}
                                </div>
                                <div className="truncate font-mono text-[10px] text-text-muted">
                                    {s.execution_id ? `run ${s.execution_id}` : s.id}
                                </div>
                            </div>
                            <div className="shrink-0 text-right font-mono text-[10px] text-text-muted">
                                <div>{fmtTime(s.created_at, lang)}</div>
                                <div>{fmtTime(s.updated_at, lang)}</div>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
