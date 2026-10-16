import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type PluginInfo, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W20: plugins panel — consumes the /rpc endpoint (rpc_bridge plugins/*
// delegation to fusion-plugins-ecosystem). Requires the [plugins] extra on
// the server; absence surfaces as rpc errors per row/action toast.

export function PluginsPage() {
    const t = useT();
    const [busyId, setBusyId] = useState('');
    const retry = useAutoRetry(() => api.rpc<{ plugins: PluginInfo[] }>('plugins/list'), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error)
        return (
            <ErrorState
                message={t(
                    '插件服务不可用 (需 fusion-plugins-ecosystem)',
                    'Plugins unavailable (fusion-plugins-ecosystem required)'
                )}
                onRetry={retry.manualRetry}
            />
        );

    const plugins = retry.data?.plugins ?? [];

    async function uninstall(id: string) {
        if (busyId) return;
        setBusyId(id);
        try {
            await api.rpc<{ ok: boolean }>('plugins/uninstall', { plugin_id: id });
            sdk.toast.show({ level: 'success', message: t('已卸载', 'Uninstalled') });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] plugin uninstall failed', e);
            sdk.toast.show({ level: 'error', message: t('卸载失败', 'Uninstall failed') });
        } finally {
            setBusyId('');
        }
    }

    async function install(id: string) {
        if (busyId) return;
        setBusyId(id);
        try {
            await api.rpc<{ ok: boolean }>('plugins/install', { plugin_id: id });
            sdk.toast.show({ level: 'success', message: t('已安装并启用', 'Installed and enabled') });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] plugin install failed', e);
            sdk.toast.show({ level: 'error', message: t('安装失败', 'Install failed') });
        } finally {
            setBusyId('');
        }
    }

    return (
        <div className="mx-auto max-w-3xl">
            <div className="flex items-end justify-between pb-2">
                <div className="text-sm font-medium text-text-primary">{t('插件', 'Plugins')}</div>
                <div className="text-xs text-text-muted">{plugins.length}</div>
            </div>
            {plugins.length === 0 ? (
                <EmptyState
                    title={t('暂无插件', 'No plugins')}
                    description={t(
                        '安装 fusion-plugins-ecosystem 后此处展示插件目录',
                        'Install fusion-plugins-ecosystem to populate the catalog'
                    )}
                />
            ) : (
                <div className="space-y-1">
                    {plugins.map((p) => (
                        <div
                            key={p.id}
                            className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2 py-1.5"
                        >
                            <div className="min-w-0 flex-1">
                                <div className="flex items-center gap-2">
                                    <span className="truncate text-sm font-medium text-text-primary">{p.name}</span>
                                    <span className="rounded bg-bg-muted px-1.5 py-0.5 font-mono text-[10px] text-text-secondary">
                                        v{p.version}
                                    </span>
                                    <StatusBadge
                                        status={p.installed ? (p.enabled ? 'success' : 'warning') : 'offline'}
                                        label={
                                            p.installed
                                                ? p.enabled
                                                    ? t('已启用', 'enabled')
                                                    : t('已停用', 'disabled')
                                                : t('未安装', 'not installed')
                                        }
                                    />
                                </div>
                                {p.description ? (
                                    <div className="mt-0.5 truncate text-xs text-text-secondary">{p.description}</div>
                                ) : null}
                            </div>
                            <span className="shrink-0 rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                {p.category}
                            </span>
                            {p.installed ? (
                                <button
                                    type="button"
                                    className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                    disabled={busyId === p.id}
                                    onClick={() => void uninstall(p.id)}
                                >
                                    {t('卸载', 'Uninstall')}
                                </button>
                            ) : (
                                <button
                                    type="button"
                                    className="shrink-0 rounded-md bg-brand px-2 py-1 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                    disabled={busyId === p.id}
                                    onClick={() => void install(p.id)}
                                >
                                    {t('安装', 'Install')}
                                </button>
                            )}
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
