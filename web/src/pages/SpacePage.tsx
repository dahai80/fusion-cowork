import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type SpaceArtifact, type SpaceInfo, type SpaceMessage, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W12/W13/W10: collaboration space surface — list + create (W12), chat with
// 5s polling (W13; SSE stream exists server-side, polling survives gateway
// proxying), members (W12), artifacts list + content preview (W10).
// Data: mounted sub-app /api/space/*.

type Tab = 'messages' | 'members' | 'artifacts';

export function SpacePage() {
    const t = useT();
    const [selected, setSelected] = useState('');
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [creating, setCreating] = useState(false);
    const retry = useAutoRetry(() => api.spaces(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={5} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const spaces = retry.data ?? [];

    async function create() {
        if (creating || !name.trim()) return;
        setCreating(true);
        try {
            const sp = await api.createSpace({ name: name.trim(), description: description.trim() });
            sdk.toast.show({ level: 'success', message: t('空间已创建', 'Space created') });
            setName('');
            setDescription('');
            setSelected(sp.id);
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] space create failed', e);
            sdk.toast.show({ level: 'error', message: t('创建失败', 'Create failed') });
        } finally {
            setCreating(false);
        }
    }

    async function remove(id: string) {
        try {
            await api.deleteSpace(id);
            if (selected === id) setSelected('');
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] space delete failed', e);
            sdk.toast.show({ level: 'error', message: t('删除失败', 'Delete failed') });
        }
    }

    return (
        <div className="mx-auto flex max-w-5xl gap-4">
            <div className="w-56 shrink-0">
                <div className="flex items-end justify-between pb-2">
                    <div className="text-sm font-medium text-text-primary">{t('协作空间', 'Spaces')}</div>
                    <div className="text-xs text-text-muted">{spaces.length}</div>
                </div>
                <div className="flex flex-col gap-1 pb-2">
                    <input
                        className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                        placeholder={t('空间名称', 'Space name')}
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                    />
                    <input
                        className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                        placeholder={t('描述 (可选)', 'Description (optional)')}
                        value={description}
                        onChange={(e) => setDescription(e.target.value)}
                    />
                    <button
                        type="button"
                        className="rounded-md bg-brand px-2 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                        disabled={creating || !name.trim()}
                        onClick={() => void create()}
                    >
                        {creating ? t('创建中…', 'Creating…') : t('新建空间', 'New space')}
                    </button>
                </div>
                {spaces.length === 0 ? (
                    <EmptyState title={t('暂无空间', 'No spaces')} />
                ) : (
                    <div className="flex flex-col gap-1">
                        {spaces.map((sp: SpaceInfo) => (
                            <div
                                key={sp.id}
                                className={`flex items-center gap-1 rounded-md border px-2 py-1.5 text-sm ${
                                    selected === sp.id
                                        ? 'border-brand bg-bg-muted text-text-primary'
                                        : 'border-border text-text-secondary hover:bg-bg-muted'
                                }`}
                            >
                                <button
                                    type="button"
                                    className="min-w-0 flex-1 truncate text-left"
                                    onClick={() => setSelected(sp.id)}
                                >
                                    {sp.name}
                                </button>
                                <button
                                    type="button"
                                    className="shrink-0 rounded px-1 text-xs text-error hover:bg-error/10"
                                    aria-label={`delete ${sp.name}`}
                                    onClick={() => void remove(sp.id)}
                                >
                                    ×
                                </button>
                            </div>
                        ))}
                    </div>
                )}
            </div>
            <div className="min-w-0 flex-1">
                {selected ? (
                    <SpaceDetail spaceId={selected} />
                ) : (
                    <EmptyState
                        title={t('选择一个空间', 'Pick a space')}
                        description={t('左侧选择或新建协作空间', 'Select or create a space on the left')}
                    />
                )}
            </div>
        </div>
    );
}

function SpaceDetail({ spaceId }: { spaceId: string }) {
    const t = useT();
    const [tab, setTab] = useState<Tab>('messages');
    const [draft, setDraft] = useState('');
    const [sending, setSending] = useState(false);
    const [reloadKey, setReloadKey] = useState(0);
    const [artifactPreview, setArtifactPreview] = useState<SpaceArtifact | null>(null);

    const msgs = useAutoRetry(() => api.spaceMessages(spaceId), [spaceId, reloadKey]);
    const members = useAutoRetry(() => api.spaceMembers(spaceId), [spaceId]);
    const artifacts = useAutoRetry(() => api.spaceArtifacts(spaceId), [spaceId, reloadKey]);

    // W13: 5s chat polling while visible
    useEffect(() => {
        const timer = window.setInterval(() => setReloadKey((k) => k + 1), 5000);
        return () => window.clearInterval(timer);
    }, []);

    async function send() {
        if (sending || !draft.trim()) return;
        setSending(true);
        try {
            await api.sendSpaceMessage(spaceId, draft.trim());
            setDraft('');
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] space send failed', e);
            sdk.toast.show({ level: 'error', message: t('发送失败', 'Send failed') });
        } finally {
            setSending(false);
        }
    }

    async function openArtifact(aid: string) {
        try {
            setArtifactPreview(await api.spaceArtifact(spaceId, aid));
        } catch (e) {
            console.error('[cowork-web] artifact open failed', e);
            sdk.toast.show({ level: 'error', message: t('打开失败', 'Open failed') });
        }
    }

    const tabs: { key: Tab; label: string }[] = [
        { key: 'messages', label: t('聊天', 'Chat') },
        { key: 'members', label: t('成员', 'Members') },
        { key: 'artifacts', label: t('产物', 'Artifacts') },
    ];

    return (
        <div className="rounded-lg border border-border bg-bg-elevated p-3">
            <div className="flex gap-1 border-b border-border pb-2">
                {tabs.map(({ key, label }) => (
                    <button
                        type="button"
                        key={key}
                        className={`rounded-md px-3 py-1 text-sm ${
                            tab === key ? 'bg-bg-muted text-text-primary' : 'text-text-secondary hover:bg-bg-muted'
                        }`}
                        onClick={() => setTab(key)}
                    >
                        {label}
                    </button>
                ))}
            </div>
            {tab === 'messages' ? (
                <div>
                    <div className="max-h-96 space-y-2 overflow-y-auto py-2">
                        {(msgs.data ?? []).length === 0 ? (
                            <EmptyState title={t('暂无消息', 'No messages')} />
                        ) : (
                            (msgs.data ?? []).map((m: SpaceMessage) => (
                                <div key={m.id} className="rounded-md border border-border bg-bg p-2">
                                    <div className="flex items-center justify-between">
                                        <span className="text-xs font-medium text-text-primary">{m.user_id}</span>
                                        {m.created_at ? (
                                            <span className="font-mono text-[10px] text-text-muted">
                                                {new Date(m.created_at).toLocaleString()}
                                            </span>
                                        ) : null}
                                    </div>
                                    <div className="mt-1 whitespace-pre-wrap text-sm text-text-secondary">
                                        {m.content}
                                    </div>
                                </div>
                            ))
                        )}
                    </div>
                    <div className="flex gap-2 pt-2">
                        <input
                            className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                            placeholder={t('输入消息…', 'Type a message…')}
                            value={draft}
                            onChange={(e) => setDraft(e.target.value)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter') void send();
                            }}
                        />
                        <button
                            type="button"
                            className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                            disabled={sending || !draft.trim()}
                            onClick={() => void send()}
                        >
                            {t('发送', 'Send')}
                        </button>
                    </div>
                </div>
            ) : null}
            {tab === 'members' ? (
                <div className="space-y-1 py-2">
                    {(members.data ?? []).length === 0 ? (
                        <EmptyState title={t('暂无成员', 'No members')} />
                    ) : (
                        (members.data ?? []).map((m) => (
                            <div
                                key={m.user_id}
                                className="flex items-center justify-between rounded-md border border-border bg-bg px-2 py-1.5"
                            >
                                <div className="text-sm text-text-primary">{m.display_name || m.user_id}</div>
                                <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                    {m.role || 'member'}
                                </span>
                            </div>
                        ))
                    )}
                </div>
            ) : null}
            {tab === 'artifacts' ? (
                <div className="py-2">
                    {artifactPreview ? (
                        <div className="rounded-md border border-border bg-bg p-3">
                            <div className="flex items-center justify-between pb-2">
                                <div className="text-sm font-medium text-text-primary">{artifactPreview.name}</div>
                                <button
                                    type="button"
                                    className="rounded px-2 py-0.5 text-xs text-text-secondary hover:bg-bg-muted"
                                    onClick={() => setArtifactPreview(null)}
                                >
                                    {t('关闭', 'Close')}
                                </button>
                            </div>
                            <pre className="max-h-72 overflow-auto whitespace-pre-wrap font-mono text-xs text-text-secondary">
                                {artifactPreview.content}
                            </pre>
                        </div>
                    ) : (artifacts.data ?? []).length === 0 ? (
                        <EmptyState title={t('暂无产物', 'No artifacts')} />
                    ) : (
                        <div className="space-y-1">
                            {(artifacts.data ?? []).map((a) => (
                                <button
                                    type="button"
                                    key={a.id}
                                    className="flex w-full items-center gap-2 rounded-md border border-border bg-bg px-2 py-1.5 text-left hover:bg-bg-muted"
                                    onClick={() => void openArtifact(a.id)}
                                >
                                    <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                        {a.artifact_type}
                                    </span>
                                    <span className="min-w-0 flex-1 truncate text-sm text-text-primary">{a.name}</span>
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            ) : null}
        </div>
    );
}
