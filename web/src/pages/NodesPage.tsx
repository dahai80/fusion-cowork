import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { api } from '../api';
import { useT } from '../i18n';

// W2: node catalog browser — all 67 registered nodes with params_schema
// hints. Data: GET /api/nodes (already returned by web serve).

const CATEGORY_COLOR: Record<string, string> = {
    tool: '#3b82f6',
    macos_system: '#8b5cf6',
    file_operation: '#10b981',
    ai_processing: '#f59e0b',
    io: '#06b6d4',
    logic: '#ef4444',
    fusion_ecosystem: '#ec4899',
    browser: '#6366f1',
};

export function NodesPage() {
    const t = useT();
    const [query, setQuery] = useState('');
    const retry = useAutoRetry(() => api.nodes(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const nodes = (retry.data?.nodes ?? []).filter((n) => {
        if (!query.trim()) return true;
        const q = query.toLowerCase();
        return (
            n.name.toLowerCase().includes(q) ||
            n.display_name.includes(query) ||
            n.category.toLowerCase().includes(q) ||
            n.description.includes(query)
        );
    });

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center justify-between gap-2 pb-3">
                <input
                    className="w-64 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                    placeholder={t('搜索节点 / 分类 / 描述', 'Search nodes / category / description')}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
                <div className="text-xs text-text-muted">
                    {nodes.length} / {retry.data?.nodes.length ?? 0}
                </div>
            </div>
            {nodes.length === 0 ? (
                <EmptyState title={t('没有匹配的节点', 'No matching nodes')} />
            ) : (
                <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                    {nodes.map((n) => (
                        <div key={n.name} className="rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="flex items-center gap-2">
                                <span
                                    className="h-2 w-2 shrink-0 rounded-full"
                                    style={{ background: CATEGORY_COLOR[n.category] ?? '#6b7280' }}
                                />
                                <span className="text-sm font-medium text-text-primary">
                                    {n.display_name || n.name}
                                </span>
                                <span className="font-mono text-[10px] text-text-muted">{n.name}</span>
                                <span className="ml-auto rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                    {n.category}
                                </span>
                            </div>
                            {n.description ? (
                                <div className="mt-1 text-xs text-text-secondary">{n.description}</div>
                            ) : null}
                            {n.params_schema && Object.keys(n.params_schema).length > 0 ? (
                                <details className="mt-1">
                                    <summary className="cursor-pointer text-[10px] text-text-muted">
                                        {t('参数 schema', 'Params schema')}
                                    </summary>
                                    <pre className="mt-1 overflow-x-auto rounded bg-bg p-1 font-mono text-[10px] text-text-secondary">
                                        {JSON.stringify(n.params_schema, null, 4)}
                                    </pre>
                                </details>
                            ) : null}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
