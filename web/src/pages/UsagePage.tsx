import { ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { api } from '../api';
import { useT } from '../i18n';

// W18 usage/quotas indicator — tenant usage counts (spaces/messages/
// artifacts/agents) vs QuotaEnforcer quotas (default unlimited). Read-only
// overview, data from GET /api/usage.

export function UsagePage() {
    const t = useT();
    const retry = useAutoRetry(() => api.usage(), []);
    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const u = retry.data;
    const rows: [string, string, string][] = [
        [
            // i18n: user-facing
            t('协作空间', 'Spaces'),
            String(u.usage.spaces),
            // i18n: user-facing
            u.quotas.max_spaces < 0 ? t('不限', 'Unlimited') : String(u.quotas.max_spaces),
        ],
        [
            // i18n: user-facing
            t('空间消息', 'Messages'),
            String(u.usage.messages),
            // i18n: user-facing
            u.quotas.max_messages_per_space < 0 ? t('不限', 'Unlimited') : String(u.quotas.max_messages_per_space),
        ],
        [
            // i18n: user-facing
            t('空间产物', 'Artifacts'),
            String(u.usage.artifacts),
            // i18n: user-facing
            u.quotas.max_artifacts_per_space < 0 ? t('不限', 'Unlimited') : String(u.quotas.max_artifacts_per_space),
        ],
        [
            'Agents',
            String(u.usage.agents),
            // i18n: user-facing
            u.quotas.max_agents_per_space < 0 ? t('不限', 'Unlimited') : String(u.quotas.max_agents_per_space),
        ],
    ];
    return (
        <div className="mx-auto max-w-2xl space-y-3">
            <div className="rounded-lg border border-border bg-bg-elevated p-3">
                <div className="flex items-center justify-between pb-2">
                    {/* i18n: user-facing */}
                    <div className="text-sm font-medium text-text-primary">{t('用量与配额', 'Usage & quotas')}</div>
                    <div className="text-xs text-text-muted">{u.tenant_id}</div>
                </div>
                <table className="w-full border-collapse text-left text-xs">
                    <thead>
                        <tr className="text-text-secondary">
                            {/* i18n: user-facing */}
                            <th className="pb-1 font-normal">{t('资源', 'Resource')}</th>
                            {/* i18n: user-facing */}
                            <th className="pb-1 font-normal">{t('用量', 'Usage')}</th>
                            {/* i18n: user-facing */}
                            <th className="pb-1 font-normal">{t('配额', 'Quota')}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows.map(([resource, usage, quota]) => (
                            <tr key={resource}>
                                <td className="py-0.5 text-text-primary">{resource}</td>
                                <td className="py-0.5 text-text-secondary">{usage}</td>
                                <td className="py-0.5 text-text-secondary">{quota}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
                <div className="pt-2 text-xs text-text-muted">
                    {/* i18n: user-facing */}
                    {u.unlimited ? t('当前租户无配额限制', 'No quota limits for this tenant') : null}
                    {u.quotas.max_storage_mb < 0 ? null : (
                        <div>
                            {/* i18n: user-facing */}
                            {t('存储配额', 'Storage quota')}: {u.quotas.max_storage_mb} MB
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
