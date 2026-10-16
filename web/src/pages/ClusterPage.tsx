import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { api } from '../api';
import { useT } from '../i18n';

// W21: cluster panel — read-only view over the distributed state layer
// (FUSION_CLUSTER_ENABLED=1): node registry heartbeat/vRAM ledger.
export function ClusterPage() {
    const t = useT();
    const retry = useAutoRetry(() => api.clusterState(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const state = retry.data!;

    if (!state.cluster_enabled) {
        return (
            <div className="mx-auto max-w-3xl">
                <EmptyState
                    title={t('集群未启用', 'Cluster disabled')}
                    description={t(
                        '设置 FUSION_CLUSTER_ENABLED=1 启用分布式状态层',
                        'Set FUSION_CLUSTER_ENABLED=1 to enable the distributed state layer'
                    )}
                />
            </div>
        );
    }

    return (
        <div className="mx-auto max-w-3xl">
            <div className="flex items-end justify-between pb-2">
                <div className="text-sm font-medium text-text-primary">{t('集群', 'Cluster')}</div>
                <div className="text-xs text-text-muted">
                    {t('节点', 'Nodes')}: {state.nodes.length} · vRAM {state.total_allocated_mb} MB
                </div>
            </div>
            {state.nodes.length === 0 ? (
                <EmptyState title={t('暂无节点', 'No nodes')} />
            ) : (
                <div className="space-y-1">
                    {state.nodes.map((n) => (
                        <div
                            key={n.node_id}
                            className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2 py-1.5"
                        >
                            <span className="min-w-0 flex-1 truncate font-mono text-sm text-text-primary">
                                {n.node_id}
                            </span>
                            {n.host ? (
                                <span className="shrink-0 font-mono text-xs text-text-secondary">
                                    {n.host}
                                    {n.port ? `:${n.port}` : ''}
                                </span>
                            ) : null}
                            <span className="shrink-0 rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                {n.role || 'worker'}
                            </span>
                            {n.vram_total_mb ? (
                                <span className="shrink-0 font-mono text-[10px] text-text-muted">
                                    {n.vram_used_mb || 0}/{n.vram_total_mb} MB
                                </span>
                            ) : null}
                            <StatusBadge
                                status={n.last_heartbeat_at ? 'success' : 'offline'}
                                label={n.last_heartbeat_at ? t('在线', 'alive') : t('无心跳', 'no heartbeat')}
                            />
                        </div>
                    ))}
                </div>
            )}
            {Object.keys(state.vram).length > 0 ? (
                <div className="pt-3">
                    <div className="pb-1 text-xs text-text-secondary">
                        {t('vRAM 分配 (按插件)', 'vRAM allocations (by plugin)')}
                    </div>
                    <div className="space-y-1">
                        {Object.entries(state.vram).map(([pluginId, mb]) => (
                            <div
                                key={pluginId}
                                className="flex items-center justify-between rounded-md border border-border bg-bg px-2 py-1 font-mono text-xs text-text-secondary"
                            >
                                <span>{pluginId}</span>
                                <span>{mb} MB</span>
                            </div>
                        ))}
                    </div>
                </div>
            ) : null}
        </div>
    );
}
