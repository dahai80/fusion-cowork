import { EmptyState, ErrorState, SkeletonLines, StatusBadge } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import type { StatusKind } from '@fusion/web-ui';
import { useState } from 'react';
import { type SessionInfo, type SessionSource, api, statusKind } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W11/A12: unified session list — engine sessions + space chat + scheduler
// history; source filter and per-source navigation.

function fmtTime(ts: number, lang: 'zh' | 'en'): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

export function SessionsPage() {
    const t = useT();
    const lang = sdk.i18n.lang();
    const [source, setSource] = useState<'' | SessionSource>('');
    const retry = useAutoRetry(() => api.sessions('', source), [source]);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const sessions = retry.data?.sessions ?? [];
    const sourceLabel: Record<SessionSource, string> = {
        engine: t('执行', 'Run'),
        space: t('协作空间', 'Space'),
        scheduler: t('调度', 'Schedule'),
    };

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-end justify-between pb-3">
                <div>
                    <div className="text-lg font-medium text-text-primary">{t('会话', 'Sessions')}</div>
                    <div className="text-xs text-text-muted">
                        {t('执行会话 / 协作空间 / 调度历史统一列表', 'Runs, spaces and scheduler history')}
                    </div>
                </div>
                <div className="flex items-center gap-2">
                    <select
                        className="rounded-md border border-border bg-bg px-2 py-1 text-xs text-text-primary"
                        value={source}
                        onChange={(e) => setSource(e.target.value as '' | SessionSource)}
                    >
                        <option value="">{t('全部来源', 'All sources')}</option>
                        <option value="engine">{t('执行', 'Run')}</option>
                        <option value="space">{t('协作空间', 'Space')}</option>
                        <option value="scheduler">{t('调度', 'Schedule')}</option>
                    </select>
                    <div className="text-xs text-text-muted">{sessions.length}</div>
                </div>
            </div>
            {sessions.length === 0 ? (
                <EmptyState title={t('暂无会话', 'No sessions')} />
            ) : (
                <div className="space-y-2">
                    {sessions.map((s: SessionInfo) => (
                        <button
                            type="button"
                            key={s.id}
                            className="flex w-full items-center gap-3 rounded-lg border border-border bg-bg-elevated p-3 text-left hover:bg-bg-muted"
                            onClick={() => {
                                window.location.hash = s.href || '#/runs';
                            }}
                        >
                            <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                {sourceLabel[s.source] ?? s.source}
                            </span>
                            <StatusBadge
                                status={
                                    (s.source === 'scheduler' && s.status === 'active'
                                        ? 'running'
                                        : statusKind(s.status)) as StatusKind
                                }
                                label={s.status}
                            />
                            <div className="min-w-0 flex-1">
                                <div className="truncate text-sm font-medium text-text-primary">
                                    {s.title || s.workflow_name || s.id}
                                </div>
                                <div className="truncate font-mono text-[10px] text-text-muted">
                                    {String(s.metadata?.last_message || '') ||
                                        (s.execution_id ? `run ${s.execution_id}` : s.id)}
                                </div>
                            </div>
                            <div className="shrink-0 text-right font-mono text-[10px] text-text-muted">
                                <div>{fmtTime(s.created_at, lang)}</div>
                                <div>{fmtTime(s.updated_at, lang)}</div>
                            </div>
                        </button>
                    ))}
                </div>
            )}
        </div>
    );
}
