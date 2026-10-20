import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type ArtifactReview, type SpaceArtifact, type SpaceInfo, type SpaceMessage, api } from '../api';
import { useT } from '../i18n';
import { preferredModel } from '../modelPref';
import { sdk, streamSse } from '../sdk';

// W12/W13/W10: collaboration space surface — list + create (W12), chat with
// 5s polling (W13; SSE stream exists server-side, polling survives gateway
// proxying), members (W12), artifacts list + content preview (W10).
// Data: mounted sub-app /api/space/*.

type Tab = 'messages' | 'members' | 'artifacts' | 'knowledge';

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

    // W13/B2: SSE 实时聊天; 连不上回落 5s 轮询
    const [sseOk, setSseOk] = useState(false);
    useEffect(() => {
        const ac = new AbortController();
        setSseOk(false);
        streamSse(
            `/api/space/spaces/${encodeURIComponent(spaceId)}/stream`,
            (event) => {
                if (event === 'ready') {
                    setSseOk(true);
                    return;
                }
                if (event.endsWith(':message')) {
                    setReloadKey((k) => k + 1);
                }
            },
            ac.signal
        )
            .then(() => setSseOk(false))
            .catch((e) => {
                if (!ac.signal.aborted) {
                    console.warn('[cowork-web] space SSE unavailable, polling fallback', e);
                    setSseOk(false);
                }
            });
        return () => ac.abort();
    }, [spaceId]);

    useEffect(() => {
        if (sseOk) return;
        const timer = window.setInterval(() => setReloadKey((k) => k + 1), 5000);
        return () => window.clearInterval(timer);
    }, [sseOk]);

    async function send() {
        if (sending || !draft.trim()) return;
        setSending(true);
        try {
            await api.sendSpaceMessage(spaceId, draft.trim(), preferredModel());
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

    async function changeRole(userId: string, role: string) {
        try {
            await api.updateSpaceMemberRole(spaceId, userId, role);
            sdk.toast.show({ level: 'success', message: t('角色已更新', 'Role updated') });
            members.manualRetry();
        } catch (e) {
            console.error('[cowork-web] role update failed', e);
            sdk.toast.show({ level: 'error', message: t('角色更新失败', 'Role update failed') });
        }
    }

    async function removeMember(userId: string) {
        try {
            const res = await sdk.http.delete(
                `/api/space/spaces/${encodeURIComponent(spaceId)}/members/${encodeURIComponent(userId)}`
            );
            if (res.status >= 400) throw new Error(String(res.status));
            members.manualRetry();
        } catch (e) {
            console.error('[cowork-web] member remove failed', e);
            sdk.toast.show({ level: 'error', message: t('移除失败', 'Remove failed') });
        }
    }

    const tabs: { key: Tab; label: string }[] = [
        { key: 'messages', label: t('聊天', 'Chat') },
        { key: 'members', label: t('成员', 'Members') },
        { key: 'artifacts', label: t('产物', 'Artifacts') },
        { key: 'knowledge', label: t('知识库', 'Knowledge') },
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
                                <div className="flex items-center gap-1">
                                    {m.role === 'owner' ? (
                                        <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                            owner
                                        </span>
                                    ) : (
                                        <select
                                            className="rounded border border-border bg-bg px-1 py-0.5 text-xs text-text-primary"
                                            value={m.role || 'member'}
                                            onChange={(e) => void changeRole(m.user_id, e.target.value)}
                                        >
                                            <option value="admin">admin</option>
                                            <option value="member">member</option>
                                            <option value="viewer">viewer</option>
                                        </select>
                                    )}
                                    {m.role === 'owner' ? null : (
                                        <button
                                            type="button"
                                            className="rounded px-1 text-xs text-error hover:bg-error/10"
                                            aria-label={`remove ${m.user_id}`}
                                            onClick={() => void removeMember(m.user_id)}
                                        >
                                            ×
                                        </button>
                                    )}
                                </div>
                            </div>
                        ))
                    )}
                    <MemberMatrix spaceId={spaceId} />
                </div>
            ) : null}
            {tab === 'knowledge' ? <KbPanel spaceId={spaceId} /> : null}
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
                                <div
                                    key={a.id}
                                    className="flex w-full items-center gap-2 rounded-md border border-border bg-bg px-2 py-1.5 hover:bg-bg-muted"
                                >
                                    <button
                                        type="button"
                                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                                        onClick={() => void openArtifact(a.id)}
                                    >
                                        <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                            {a.artifact_type}
                                        </span>
                                        <span className="min-w-0 flex-1 truncate text-sm text-text-primary">
                                            {a.name}
                                        </span>
                                    </button>
                                    <ArtifactReviewBadge artifact={a} />
                                    <ArtifactReviewButtons
                                        spaceId={spaceId}
                                        artifactId={a.id}
                                        onDone={() => artifacts.manualRetry()}
                                    />
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            ) : null}
        </div>
    );
}

// W16: role-permission matrix (15 actions x 4 roles, read-only display).
function MemberMatrix({ spaceId }: { spaceId: string }) {
    const t = useT();
    const retry = useAutoRetry(() => api.spacePermissions(spaceId), [spaceId]);
    if (retry.loading && !retry.data) return null;
    if (retry.error || !retry.data) return null;
    const actions = Object.keys(retry.data.matrix.owner ?? {});
    return (
        <details className="rounded-md border border-border bg-bg px-2 py-1.5">
            <summary className="cursor-pointer text-xs text-text-secondary">
                {t('权限矩阵 (角色 x 动作)', 'Permission matrix (role x action)')}
            </summary>
            <div className="mt-2 overflow-x-auto">
                <table className="w-full border-collapse text-left text-[11px]">
                    <thead>
                        <tr className="text-text-secondary">
                            <th className="py-1 pr-3 font-medium">{t('动作', 'Action')}</th>
                            {retry.data.roles.map((r) => (
                                <th key={r} className="py-1 pr-3 font-medium">
                                    {r}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {actions.map((a) => (
                            <tr key={a} className="border-t border-border">
                                <td className="py-0.5 pr-3 font-mono text-text-secondary">{a}</td>
                                {retry.data!.roles.map((r) => (
                                    <td key={r} className="py-0.5 pr-3">
                                        {retry.data!.matrix[r]?.[a] ? (
                                            <span className="text-success">✓</span>
                                        ) : (
                                            <span className="text-text-muted">·</span>
                                        )}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </details>
    );
}

// W14: KB retrieval panel — status + bind/unbind + search + document list.
// Real search needs fusion-kb :11436 live; errors surface as 502-style toasts.
function KbPanel({ spaceId }: { spaceId: string }) {
    const t = useT();
    const [query, setQuery] = useState('');
    const [searching, setSearching] = useState(false);
    const [results, setResults] = useState<{ document_name?: string; content?: string; score?: number }[] | null>(null);
    const status = useAutoRetry(() => api.kbStatus(spaceId), [spaceId]);
    const docs = useAutoRetry(() => api.kbDocuments(spaceId), [spaceId]);
    const [instr, setInstr] = useState('');
    const [instrLoaded, setInstrLoaded] = useState(false);
    const [savingInstr, setSavingInstr] = useState(false);

    useEffect(() => {
        let alive = true;
        api
            .spaceInstructions(spaceId)
            .then((r) => {
                if (!alive) return;
                setInstr(r.instructions || '');
                setInstrLoaded(true);
            })
            .catch((e) => console.warn('[cowork-web] space instructions load failed', e));
        return () => {
            alive = false;
        };
    }, [spaceId]);

    async function saveInstructions() {
        if (savingInstr) return;
        setSavingInstr(true);
        try {
            const r = await api.saveSpaceInstructions(spaceId, instr);
            setInstr(r.instructions || '');
            sdk.toast.show({ level: 'success', message: t('空间指令已保存', 'Instructions saved') });
        } catch (e) {
            console.error('[cowork-web] space instructions save failed', e);
            sdk.toast.show({ level: 'error', message: t('保存失败', 'Save failed') });
        } finally {
            setSavingInstr(false);
    }
    }

    async function bind(unbind: boolean) {
        try {
            if (unbind) {
                await api.kbUnbind(spaceId);
            } else {
                await api.kbBind(spaceId);
            }
            status.manualRetry();
        } catch (e) {
            console.error('[cowork-web] kb toggle failed', e);
            sdk.toast.show({ level: 'error', message: t('知识库操作失败', 'KB operation failed') });
        }
    }

    async function search() {
        if (searching || !query.trim()) return;
        setSearching(true);
        try {
            setResults(await api.kbSearch(spaceId, query.trim()));
        } catch (e) {
            console.error('[cowork-web] kb search failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('检索失败 (需 fusion-kb 在线)', 'Search failed (fusion-kb required)'),
            });
        } finally {
            setSearching(false);
        }
    }

    return (
        <div className="space-y-2 py-2">
            <div className="rounded-lg border border-border bg-bg-elevated p-3">
                <div className="text-xs font-medium text-text-primary">{t('空间指令', 'Space instructions')}</div>
                <div className="mt-1 text-[10px] text-text-muted">
                    {t('注入该空间所有 agent 聊天的 system prompt', 'Injected into every agent chat system prompt in this space')}
                </div>
                <textarea
                    className="min-h-20 w-full rounded-md border border-border bg-bg px-2 py-1.5 text-xs text-text-primary"
                    placeholder={t('例: 用中文回答, 引用知识库内容时标注来源', 'e.g. answer in Chinese, cite KB sources')}
                    value={instr}
                    onChange={(e) => setInstr(e.target.value)}
                    disabled={!instrLoaded}
                />
                <div className="mt-1 flex justify-end">
                    <button
                        type="button"
                        className="rounded-md bg-brand px-2 py-1 text-xs text-white hover:opacity-90 disabled:opacity-50"
                        disabled={!instrLoaded || savingInstr}
                        onClick={() => void saveInstructions()}
                    >
                        {savingInstr ? t('保存中…', 'Saving…') : t('保存指令', 'Save instructions')}
                    </button>
                </div>
            </div>
            <div className="flex items-center gap-2">
                <span className="text-xs text-text-secondary">
                    {status.data?.bound ? t('已绑定知识库', 'KB bound') : t('未绑定知识库', 'No KB bound')}
                </span>
                {status.data?.bound ? (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-0.5 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => void bind(true)}
                    >
                        {t('解绑', 'Unbind')}
                    </button>
                ) : (
                    <button
                        type="button"
                        className="rounded-md border border-border px-2 py-0.5 text-xs text-text-primary hover:bg-bg-muted"
                        onClick={() => void bind(false)}
                    >
                        {t('绑定知识库', 'Bind KB')}
                    </button>
                )}
            </div>
            <div className="flex gap-2">
                <input
                    className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                    placeholder={t('检索知识库…', 'Search knowledge base…')}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter') void search();
                    }}
                />
                <button
                    type="button"
                    className="rounded-md bg-brand px-3 py-1 text-sm text-white hover:opacity-90 disabled:opacity-50"
                    disabled={searching || !query.trim()}
                    onClick={() => void search()}
                >
                    {searching ? t('检索中…', 'Searching…') : t('检索', 'Search')}
                </button>
            </div>
            {results ? (
                <div className="space-y-1">
                    {results.length === 0 ? (
                        <div className="text-xs text-text-muted">{t('无结果', 'No results')}</div>
                    ) : (
                        results.map((r, i) => (
                            <div key={i} className="rounded-md border border-border bg-bg p-2">
                                <div className="flex items-center justify-between">
                                    <span className="text-xs font-medium text-text-primary">
                                        {r.document_name || t('文档', 'Document')}
                                    </span>
                                    {typeof r.score === 'number' ? (
                                        <span className="font-mono text-[10px] text-text-muted">
                                            {r.score.toFixed(3)}
                                        </span>
                                    ) : null}
                                </div>
                                {r.content ? (
                                    <div className="mt-1 line-clamp-4 whitespace-pre-wrap text-xs text-text-secondary">
                                        {r.content}
                                    </div>
                                ) : null}
                            </div>
                        ))
                    )}
                </div>
            ) : null}
            <div className="pt-1">
                <div className="pb-1 text-xs text-text-secondary">{t('文档列表', 'Documents')}</div>
                {(docs.data ?? []).length === 0 ? (
                    <div className="text-xs text-text-muted">{t('暂无文档', 'No documents')}</div>
                ) : (
                    <div className="space-y-1">
                        {(docs.data ?? []).map((d, i) => (
                            <div
                                key={d.document_id || d.id || i}
                                className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                            >
                                {d.file_name || d.name || d.document_id || d.id}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    );
}

// W9 delivery review: badge shows metadata.review status (unreviewed by
// default); owner/admin approve/reject via review_artifact state machine.
function parseReview(artifact: SpaceArtifact): ArtifactReview | null {
    let meta: unknown = artifact.metadata;
    if (typeof meta === 'string') {
        try {
            meta = JSON.parse(meta);
        } catch {
            return null;
        }
    }
    const review = (meta as Record<string, unknown> | null)?.review;
    if (!review || typeof review !== 'object') return null;
    const r = review as Record<string, unknown>;
    if (r.status !== 'approved' && r.status !== 'rejected') return null;
    return r as unknown as ArtifactReview;
}

function ArtifactReviewBadge({ artifact }: { artifact: SpaceArtifact }) {
    const t = useT();
    const review = parseReview(artifact);
    if (!review) return null;
    return (
        <span
            className={
                review.status === 'approved'
                    ? 'rounded bg-status-success-bg px-1.5 py-0.5 text-[10px] text-status-success'
                    : 'rounded bg-status-error-bg px-1.5 py-0.5 text-[10px] text-status-error'
            }
        >
            {review.status === 'approved' ? t('已通过', 'Approved') : t('已退回', 'Rejected')}
        </span>
    );
}

function ArtifactReviewButtons({
    spaceId,
    artifactId,
    onDone,
}: { spaceId: string; artifactId: string; onDone: () => void }) {
    const t = useT();
    const [busy, setBusy] = useState(false);
    async function review(approved: boolean) {
        if (busy) return;
        setBusy(true);
        try {
            await api.reviewArtifact(spaceId, artifactId, approved);
            sdk.toast.show({ level: 'success', message: approved ? t('已通过', 'Approved') : t('已退回', 'Rejected') });
            onDone();
        } catch (e) {
            console.error('[cowork-web] artifact review failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('审查失败 (需空间 owner/admin)', 'Review failed (owner/admin only)'),
            });
        } finally {
            setBusy(false);
        }
    }
    return (
        <span className="flex shrink-0 gap-1">
            <button
                type="button"
                disabled={busy}
                className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                onClick={() => void review(true)}
            >
                {t('通过', 'Approve')}
            </button>
            <button
                type="button"
                disabled={busy}
                className="rounded border border-border px-1.5 py-0.5 text-[10px] text-text-secondary hover:bg-bg-muted disabled:opacity-50"
                onClick={() => void review(false)}
            >
                {t('退回', 'Reject')}
            </button>
        </span>
    );
}
