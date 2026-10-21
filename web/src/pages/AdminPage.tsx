import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useCallback, useEffect, useState } from 'react';
import { type AdminMember, type AdminUsage, adminApi, adminKey, saveAdminKey } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// issue #156: 管理台 — 成员 / 用量 / 审计导出 (OTLP JSON) / 推送测试。
// 服务器未配 FUSION_COWORK_ADMIN_KEY 时所有请求 503 admin_disabled。

export function AdminPage() {
    const t = useT();
    const [keyInput, setKeyInput] = useState(adminKey());
    const [members, setMembers] = useState<AdminMember[]>([]);
    const [mode, setMode] = useState('local');
    const [usage, setUsage] = useState<AdminUsage | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState('');

    const load = useCallback(async () => {
        if (!adminKey()) {
            setError('');
            setMembers([]);
            setUsage(null);
            return;
        }
        setLoading(true);
        setError('');
        try {
            const [m, u] = await Promise.all([adminApi.members(), adminApi.usage()]);
            setMembers(m.members);
            setMode(m.mode);
            setUsage(u);
        } catch (e) {
            console.error('[cowork-web] admin load failed', e);
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => {
        void load();
    }, [load]);

    function applyKey() {
        saveAdminKey(keyInput.trim());
        void load();
    }

    async function exportAudit() {
        setBusy('audit');
        try {
            const data = await adminApi.auditExport();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `fusion-cowork-audit-${Date.now()}.json`;
            link.click();
            URL.revokeObjectURL(url);
            sdk.toast.show({ level: 'success', message: t('审计已导出', 'Audit exported') });
        } catch (e) {
            console.error('[cowork-web] audit export failed', e);
            sdk.toast.show({ level: 'error', message: t('导出失败', 'Export failed') });
        } finally {
            setBusy('');
        }
    }

    async function pushTest() {
        setBusy('push');
        try {
            const res = await adminApi.pushTest();
            sdk.toast.show({
                level: res.delivered ? 'success' : 'warning',
                message: res.enabled
                    ? t('推送已发送', 'Push sent')
                    : t('推送未配置 (FUSION_COWORK_PUSH_WEBHOOK_URL)', 'Push not configured'),
            });
        } catch (e) {
            console.error('[cowork-web] push test failed', e);
            sdk.toast.show({ level: 'error', message: t('推送测试失败', 'Push test failed') });
        } finally {
            setBusy('');
        }
    }

    return (
        <div className="mx-auto max-w-3xl space-y-4">
            <div className="flex items-end justify-between">
                <div className="text-sm font-medium text-text-primary">{t('管理台', 'Admin console')}</div>
                <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                    {mode === 'identity' ? t('身份服务', 'identity') : t('本地', 'local')}
                </span>
            </div>

            <div className="flex gap-2">
                <input
                    type="password"
                    className="min-w-0 flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-xs text-text-primary"
                    placeholder={t('管理密钥 (x-admin-key)', 'Admin key (x-admin-key)')}
                    value={keyInput}
                    onChange={(e) => setKeyInput(e.target.value)}
                />
                <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    onClick={applyKey}
                >
                    {t('保存并加载', 'Save & load')}
                </button>
            </div>

            {loading ? <SkeletonLines rows={5} /> : null}
            {error ? <ErrorState message={error} onRetry={() => void load()} /> : null}

            {usage ? (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    {[
                        { label: t('空间', 'Spaces'), value: usage.spaces.spaces },
                        { label: t('消息', 'Messages'), value: usage.spaces.messages },
                        { label: t('产物', 'Artifacts'), value: usage.spaces.artifacts },
                        { label: t('智能体', 'Agents'), value: usage.spaces.agents },
                    ].map((card) => (
                        <div key={card.label} className="rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="text-[10px] text-text-muted">{card.label}</div>
                            <div className="font-mono text-lg text-text-primary">{card.value}</div>
                        </div>
                    ))}
                    <div className="rounded-lg border border-border bg-bg-elevated p-3">
                        <div className="text-[10px] text-text-muted">{t('运行', 'Runs')}</div>
                        <div className="font-mono text-lg text-text-primary">{usage.runs.total}</div>
                    </div>
                    <div className="rounded-lg border border-border bg-bg-elevated p-3">
                        <div className="text-[10px] text-text-muted">{t('令牌', 'Tokens')}</div>
                        <div className="font-mono text-lg text-text-primary">{usage.tokens}</div>
                    </div>
                </div>
            ) : null}

            {members.length === 0 && !loading && !error ? (
                <EmptyState title={t('无成员', 'No members')} />
            ) : (
                <div className="space-y-1">
                    {members.map((m) => (
                        <div
                            key={m.user_id}
                            className="flex items-center gap-2 rounded-md border border-border bg-bg-elevated px-2 py-1.5"
                        >
                            <span className="min-w-0 flex-1 truncate font-mono text-sm text-text-primary">
                                {m.user_id}
                            </span>
                            {m.tenant_id ? (
                                <span className="shrink-0 font-mono text-[10px] text-text-muted">{m.tenant_id}</span>
                            ) : null}
                            <span className="shrink-0 rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                {m.role}
                            </span>
                        </div>
                    ))}
                </div>
            )}

            <div className="flex gap-2">
                <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    disabled={busy !== ''}
                    onClick={() => void exportAudit()}
                >
                    {t('导出审计 (OTLP)', 'Export audit (OTLP)')}
                </button>
                <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                    disabled={busy !== ''}
                    onClick={() => void pushTest()}
                >
                    {t('推送测试', 'Push test')}
                </button>
            </div>
        </div>
    );
}
