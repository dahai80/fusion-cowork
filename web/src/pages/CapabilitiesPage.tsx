import { ConfirmDialog, EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useMemo, useState } from 'react';
import { type CapabilityItem, type CapabilityType, type CapabilityUsageRecord, api } from '../api';
import {
    CAPABILITY_TABS,
    capabilityItems,
    capabilityPitches,
    capabilityTypeFromRoute,
    dismissPitches,
    filterCapabilities,
    hasInstalledCapabilities,
    pitchesDismissed,
} from '../capabilities';
import { useT } from '../i18n';

const ACTIVE_KEY = 'cowork.activeWorkspace';

const SOURCE_LABEL: Record<string, string> = {
    builtin: 'builtin',
    user_pack: 'user pack',
    markdown_pack: 'markdown pack',
    knowledge: 'fusion-rag',
    connector_config: 'connector config',
    mcp_registry: 'MCP registry',
    dag_template: 'DAG template',
    agent_studio: 'agent-studio',
};

export function CapabilitiesPage({ arg = '' }: { arg?: string }) {
    const t = useT();
    const [tab, setTab] = useState<CapabilityType>(capabilityTypeFromRoute(arg));
    const [workspaceId, setWorkspaceId] = useState(() => localStorage.getItem(ACTIVE_KEY) || '');
    const [query, setQuery] = useState('');
    const [busy, setBusy] = useState('');
    const [message, setMessage] = useState('');
    const [usage, setUsage] = useState<CapabilityUsageRecord | null>(null);
    const [definition, setDefinition] = useState<{ id: string; json: string } | null>(null);
    const [spawnTarget, setSpawnTarget] = useState<string | null>(null);
    const [spawnInput, setSpawnInput] = useState('');
    const [confirm, setConfirm] = useState<{ title: string; description: string; action: () => void } | null>(null);

    const catalog = useAutoRetry(() => api.capabilities(workspaceId), [workspaceId]);
    const spaces = useAutoRetry(() => api.workspaces(), []);
    // issue #244 R1: 首访 pitch 卡 — 安装任意能力后永久消失 (localStorage 持久)
    const [pitchesGone, setPitchesGone] = useState(pitchesDismissed);
    const showPitches = !pitchesGone && !hasInstalledCapabilities(catalog.data);
    const pitches = capabilityPitches();

    const items = useMemo(
        () => filterCapabilities(capabilityItems(catalog.data, tab), query),
        [catalog.data, tab, query]
    );

    function reload() {
        catalog.manualRetry();
    }

    function changeWorkspace(next: string) {
        setWorkspaceId(next);
        if (next) localStorage.setItem(ACTIVE_KEY, next);
        else localStorage.removeItem(ACTIVE_KEY);
    }

    async function run(key: string, fn: () => Promise<unknown>, done: string) {
        setBusy(key);
        setMessage('');
        try {
            await fn();
            if (key.startsWith('install:')) {
                dismissPitches();
                setPitchesGone(true);
            }
            setMessage(done);
            reload();
        } catch (e) {
            setMessage(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    function install(item: CapabilityItem) {
        void run(
            `install:${item.id}`,
            () => api.installCapability(item.type, item.id, workspaceId),
            t('已安装', 'Installed')
        );
    }

    function uninstall(item: CapabilityItem) {
        setConfirm({
            title: t('卸载能力', 'Uninstall capability'),
            description: t(
                `从当前工作区移除 ${item.title}? 运行中的任务不受影响。`,
                `Remove ${item.title} from this workspace? Running tasks are unaffected.`
            ),
            action: () =>
                void run(
                    `uninstall:${item.id}`,
                    () => api.uninstallCapability(item.type, item.id, workspaceId),
                    t('已卸载', 'Uninstalled')
                ),
        });
    }

    function toggle(item: CapabilityItem) {
        void run(
            `toggle:${item.id}`,
            () => api.setCapabilityEnabled(item.type, item.id, !item.enabled, workspaceId),
            item.enabled ? t('已停用', 'Disabled') : t('已启用', 'Enabled')
        );
    }

    function grantScope(item: CapabilityItem, scope: string) {
        const apply = () =>
            void run(
                `scope:${item.id}:${scope}`,
                () => api.grantCapabilityScopes(item.id, [scope], true, workspaceId),
                t('已授权', 'Scope granted')
            );
        const sensitive = item.scopes.find((s) => s.id === scope)?.sensitive;
        if (!sensitive) {
            apply();
            return;
        }
        setConfirm({
            title: t('授权敏感 scope', 'Approve sensitive scope'),
            description: t(
                `授予 ${item.title} 敏感权限 ${scope}? 之后调用将需要审批。`,
                `Grant sensitive scope ${scope} to ${item.title}? Calls will still require approval.`
            ),
            action: apply,
        });
    }

    async function connect(item: CapabilityItem) {
        setBusy(`oauth:${item.id}`);
        setMessage('');
        try {
            const started = await api.startConnectorOAuth(item.id, workspaceId);
            window.location.href = started.authorize_url;
        } catch (e) {
            setMessage(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    async function showUsage(item: CapabilityItem) {
        setBusy(`usage:${item.id}`);
        setMessage('');
        try {
            setUsage(await api.capabilityUsage(item.type, item.id, workspaceId));
        } catch (e) {
            setMessage(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    async function showDefinition(item: CapabilityItem) {
        setBusy(`define:${item.id}`);
        setMessage('');
        try {
            const res = await api.subagentDefinition(item.id, workspaceId);
            setDefinition({ id: item.id, json: JSON.stringify(res.definition, null, 2) });
        } catch (e) {
            setMessage(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    async function spawn(item: CapabilityItem, approve = false) {
        setBusy(`spawn:${item.id}`);
        setMessage('');
        try {
            const res = await api.spawnSubagent(item.id, spawnInput, workspaceId, approve);
            if (res.run_id && res.route) {
                setMessage(
                    t(`已派生运行 ${res.run_id} — 打开 ${res.route}`, `Spawned run ${res.run_id} — open ${res.route}`)
                );
            } else {
                setMessage(t('已提交到 agent-studio', 'Submitted to agent-studio'));
            }
            setSpawnTarget(null);
            setSpawnInput('');
            reload();
        } catch (e) {
            setMessage(e instanceof Error ? e.message : String(e));
        } finally {
            setBusy('');
        }
    }

    const sources = catalog.data?.sources ?? [];

    return (
        <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                    <div className="text-lg font-medium text-text-primary">{t('能力市场', 'Capabilities')}</div>
                    <div className="text-xs text-text-muted">
                        {t(
                            '技能 / 连接器 / 子代理按工作区安装，运行前按已授权 scope 校验。',
                            'Skills, connectors and subagents install per workspace; runs are gated by granted scopes.'
                        )}
                    </div>
                </div>
                <select
                    aria-label={t('工作区', 'Workspace')}
                    className="rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-sm text-text-primary"
                    value={workspaceId}
                    onChange={(e) => changeWorkspace(e.target.value)}
                >
                    <option value="">{t('默认工作区', 'Default workspace')}</option>
                    {(spaces.data?.workspaces ?? []).map((w) => (
                        <option key={w.id} value={w.id}>
                            {w.name || w.id}
                        </option>
                    ))}
                </select>
            </div>

            {showPitches ? (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" data-testid="capability-pitches">
                    {pitches.map((p) => (
                        <button
                            key={p.key}
                            type="button"
                            className="rounded-xl border border-border bg-bg-elevated p-4 text-left shadow-sm transition-colors hover:border-accent/60 dark:shadow-zinc-900/5"
                            onClick={() => {
                                setTab(p.key);
                                setQuery('');
                                window.location.hash = `#/capabilities/${p.key}`;
                            }}
                        >
                            <div className="text-sm font-medium text-text-primary">
                                {p.icon} {t(p.title.zh, p.title.en)}
                            </div>
                            <div className="pt-1 text-xs text-text-secondary">{t(p.desc.zh, p.desc.en)}</div>
                        </button>
                    ))}
                </div>
            ) : null}

            <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
                {CAPABILITY_TABS.map((entry) => {
                    const count = catalog.data?.[`${entry.key}s` as 'skills' | 'connectors' | 'subagents']?.length ?? 0;
                    const on = tab === entry.key;
                    return (
                        <button
                            key={entry.key}
                            type="button"
                            className={`rounded-lg px-3 py-1.5 text-sm ${
                                on
                                    ? 'bg-bg-elevated font-medium text-text-primary'
                                    : 'text-text-muted hover:text-text-primary'
                            }`}
                            onClick={() => {
                                setTab(entry.key);
                                window.location.hash = `#/capabilities/${entry.key}`;
                            }}
                        >
                            {entry.icon} {t(entry.zh, entry.en)}
                            <span className="pl-1.5 text-xs text-text-muted">{count}</span>
                        </button>
                    );
                })}
                <input
                    className="ml-auto w-56 rounded-lg border border-border bg-bg-elevated px-3 py-1.5 text-sm text-text-primary"
                    placeholder={t('搜索能力 (⌘K 全局)', 'Search capabilities (⌘K global)')}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                />
            </div>

            {message ? <div className="text-xs text-text-muted">{message}</div> : null}

            {sources.length > 0 ? (
                <div className="flex flex-wrap gap-2 text-[11px] text-text-muted">
                    {sources.map((s) => (
                        <span
                            key={s.name}
                            className={`rounded-full border px-2 py-0.5 ${
                                s.available ? 'border-border' : 'border-error/40 text-error'
                            }`}
                            title={s.error}
                        >
                            {s.name}: {s.available ? t('可用', 'available') : t('不可用', 'unavailable')}
                        </span>
                    ))}
                </div>
            ) : null}

            {catalog.loading && !catalog.data ? (
                <SkeletonLines rows={6} />
            ) : catalog.error ? (
                <ErrorState message={catalog.error.message} onRetry={reload} />
            ) : items.length === 0 ? (
                <EmptyState
                    title={t('暂无能力', 'No capabilities')}
                    description={t('换个工作区或关键词。', 'Try another workspace or keyword.')}
                />
            ) : (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {items.map((item) => (
                        <CapabilityCard
                            key={item.id}
                            item={item}
                            busy={busy}
                            workspaceId={workspaceId}
                            onInstall={() => install(item)}
                            onUninstall={() => uninstall(item)}
                            onToggle={() => toggle(item)}
                            onGrantScope={(scope) => grantScope(item, scope)}
                            onConnect={() => void connect(item)}
                            onRevoke={() =>
                                void run(
                                    `revoke:${item.id}`,
                                    () => api.revokeConnector(item.id, workspaceId),
                                    t('已撤销授权', 'Revoked')
                                )
                            }
                            onUsage={() => void showUsage(item)}
                            onDefinition={() => void showDefinition(item)}
                            onSpawn={() => {
                                setSpawnTarget(spawnTarget === item.id ? null : item.id);
                                setSpawnInput('');
                            }}
                            spawnOpen={spawnTarget === item.id}
                            spawnInput={spawnInput}
                            onSpawnInput={setSpawnInput}
                            onSpawnSubmit={() => {
                                if (item.requires_approval) {
                                    setConfirm({
                                        title: t('批准派生子代理', 'Approve subagent spawn'),
                                        description: t(
                                            `派生 ${item.title} 将执行多步远程任务，确认继续?`,
                                            `Spawning ${item.title} runs a multi-step remote task. Continue?`
                                        ),
                                        action: () => void spawn(item, true),
                                    });
                                    return;
                                }
                                void spawn(item);
                            }}
                        />
                    ))}
                </div>
            )}

            {usage ? (
                <div className="rounded-xl border border-border bg-bg-elevated p-4">
                    <div className="flex items-center justify-between pb-2">
                        <div className="text-sm font-medium text-text-primary">
                            {t('使用记录', 'Usage')} · {usage.id}
                        </div>
                        <button
                            type="button"
                            className="text-xs text-text-muted hover:text-text-primary"
                            onClick={() => setUsage(null)}
                        >
                            {t('关闭', 'Close')}
                        </button>
                    </div>
                    <div className="pb-2 text-xs text-text-muted">
                        {t(`调用 ${usage.usage.count} 次`, `${usage.usage.count} calls`)}
                        {usage.usage.last_used_at
                            ? ` · ${new Date(usage.usage.last_used_at * 1000).toLocaleString()}`
                            : ''}
                    </div>
                    {usage.audit.length === 0 ? (
                        <div className="text-xs text-text-muted">{t('暂无记录', 'No records yet')}</div>
                    ) : (
                        <ul className="divide-y divide-border text-xs">
                            {usage.audit.map((row, i) => (
                                <li key={`${row.run_id}-${i}`} className="flex items-center gap-2 py-1.5">
                                    <span className="font-mono text-text-muted">{row.run_id.slice(0, 10)}</span>
                                    <span className="text-text-primary">{row.workflow || row.node}</span>
                                    <span className="ml-auto text-text-muted">{row.verdict}</span>
                                    {row.actor ? <span className="text-text-muted">@{row.actor}</span> : null}
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            ) : null}

            {definition ? (
                <div className="rounded-xl border border-border bg-bg-elevated p-4">
                    <div className="flex items-center justify-between pb-2">
                        <div className="text-sm font-medium text-text-primary">
                            {t('定义', 'Definition')} · {definition.id}
                        </div>
                        <button
                            type="button"
                            className="text-xs text-text-muted hover:text-text-primary"
                            onClick={() => setDefinition(null)}
                        >
                            {t('关闭', 'Close')}
                        </button>
                    </div>
                    <pre className="max-h-80 overflow-auto rounded-lg bg-bg-muted p-3 text-[11px] text-text-primary">
                        {definition.json}
                    </pre>
                </div>
            ) : null}

            <ConfirmDialog
                open={confirm !== null}
                title={confirm?.title ?? ''}
                description={confirm?.description ?? ''}
                danger
                onConfirm={() => {
                    confirm?.action();
                    setConfirm(null);
                }}
                onCancel={() => setConfirm(null)}
            />
        </div>
    );
}

function CapabilityCard(props: {
    item: CapabilityItem;
    busy: string;
    workspaceId: string;
    onInstall: () => void;
    onUninstall: () => void;
    onToggle: () => void;
    onGrantScope: (scope: string) => void;
    onConnect: () => void;
    onRevoke: () => void;
    onUsage: () => void;
    onDefinition: () => void;
    onSpawn: () => void;
    spawnOpen: boolean;
    spawnInput: string;
    onSpawnInput: (v: string) => void;
    onSpawnSubmit: () => void;
}) {
    const t = useT();
    const { item } = props;
    const usable = item.installed && item.enabled;
    const oauth = item.auth?.kind === 'oauth';
    const granted = new Set(item.granted_scopes);
    const btn =
        'rounded-lg border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50';

    return (
        <div className="flex flex-col gap-2 rounded-xl border border-border bg-bg-elevated p-4 shadow-sm">
            <div className="flex items-start justify-between gap-2">
                <div>
                    <div className="text-sm font-medium text-text-primary">{item.title || item.name}</div>
                    <div className="text-[11px] text-text-muted">
                        {SOURCE_LABEL[item.source] ?? item.source}
                        {item.usage.count > 0 ? ` · ${t(`${item.usage.count} 次`, `${item.usage.count} calls`)}` : ''}
                    </div>
                </div>
                <div className="flex flex-wrap justify-end gap-1">
                    {item.installed ? (
                        <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-text-muted">
                            {item.enabled ? t('已启用', 'enabled') : t('已停用', 'disabled')}
                        </span>
                    ) : (
                        <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-text-muted">
                            {t('未安装', 'not installed')}
                        </span>
                    )}
                    {oauth ? (
                        <span
                            className={`rounded-full border px-2 py-0.5 text-[10px] ${
                                item.connected ? 'border-border text-text-muted' : 'border-warning/40 text-warning'
                            }`}
                        >
                            {item.connected ? t('已授权', 'connected') : t('待授权', 'not connected')}
                        </span>
                    ) : null}
                </div>
            </div>

            {item.description ? <div className="text-xs text-text-muted">{item.description}</div> : null}

            {item.scopes.length > 0 ? (
                <div className="flex flex-wrap gap-1">
                    {item.scopes.map((scope) => (
                        <button
                            key={scope.id}
                            type="button"
                            className={`rounded-full border px-2 py-0.5 text-[10px] ${
                                granted.has(scope.id)
                                    ? 'border-border text-text-muted'
                                    : scope.sensitive
                                      ? 'border-warning/40 text-warning'
                                      : 'border-border text-text-primary'
                            }`}
                            title={granted.has(scope.id) ? t('已授权', 'granted') : t('点击授权', 'Click to grant')}
                            disabled={!usable || granted.has(scope.id) || props.busy !== ''}
                            onClick={() => props.onGrantScope(scope.id)}
                        >
                            {scope.id}
                            {scope.sensitive ? ' ⚠' : ''}
                        </button>
                    ))}
                </div>
            ) : null}

            {oauth && item.auth?.configured === false ? (
                <div className="text-[11px] text-warning">
                    {t('OAuth 未配置 (缺 client_id env)', 'OAuth not configured (missing client_id env)')}
                </div>
            ) : null}

            <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                {!item.installed ? (
                    <button type="button" className={btn} disabled={props.busy !== ''} onClick={props.onInstall}>
                        {t('安装', 'Install')}
                    </button>
                ) : (
                    <>
                        <button type="button" className={btn} onClick={props.onToggle} disabled={props.busy !== ''}>
                            {item.enabled ? t('停用', 'Disable') : t('启用', 'Enable')}
                        </button>
                        <button type="button" className={btn} onClick={props.onUninstall} disabled={props.busy !== ''}>
                            {t('卸载', 'Uninstall')}
                        </button>
                        <button type="button" className={btn} onClick={props.onUsage} disabled={props.busy !== ''}>
                            {t('使用记录', 'Usage')}
                        </button>
                    </>
                )}
                {item.type === 'connector' && item.installed ? (
                    <>
                        {oauth ? (
                            <>
                                <button
                                    type="button"
                                    className={btn}
                                    onClick={props.onConnect}
                                    disabled={props.busy !== '' || item.auth?.configured === false}
                                >
                                    {item.connected ? t('重新授权', 'Reconnect') : t('连接', 'Connect')}
                                </button>
                                <button
                                    type="button"
                                    className={btn}
                                    onClick={props.onRevoke}
                                    disabled={props.busy !== ''}
                                >
                                    {t('撤销', 'Revoke')}
                                </button>
                            </>
                        ) : null}
                    </>
                ) : null}
                {item.type === 'subagent' ? (
                    <>
                        <button type="button" className={btn} onClick={props.onDefinition} disabled={props.busy !== ''}>
                            {t('定义', 'Definition')}
                        </button>
                        <button
                            type="button"
                            className={btn}
                            onClick={props.onSpawn}
                            disabled={!usable || props.busy !== ''}
                        >
                            {t('派生', 'Spawn')}
                        </button>
                    </>
                ) : null}
                {item.detail_route ? (
                    <button
                        type="button"
                        className={btn}
                        onClick={() => {
                            window.location.hash = `#${item.detail_route}`;
                        }}
                    >
                        {t('详情', 'Details')}
                    </button>
                ) : null}
            </div>

            {props.spawnOpen ? (
                <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-bg-muted p-2">
                    <input
                        className="rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                        placeholder={t('给子代理的任务输入', 'Input for the subagent')}
                        value={props.spawnInput}
                        onChange={(e) => props.onSpawnInput(e.target.value)}
                    />
                    <button
                        type="button"
                        className={btn}
                        onClick={props.onSpawnSubmit}
                        disabled={props.busy !== '' || props.spawnInput.trim() === ''}
                    >
                        {t('提交派生', 'Spawn now')}
                    </button>
                </div>
            ) : null}
        </div>
    );
}
