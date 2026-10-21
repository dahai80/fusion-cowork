import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type DiffBlock, type GuardApproval, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// #138 G6/C3/C6: approval cards — node context + params snapshot + diff-style
// blocks for command/file artifacts, three actions (allow once = precise
// scope / always allow tool = scope * / deny). Rules persist via backend
// save() on confirm (survive serve restart).
// #142: 编辑型 params 以 diff 分块呈现, 每块可独立接受/拒绝;
// 部分接受时 "仅本次允许" 携带 accepted_blocks, 后端重建窄化后的编辑参数。
export function ApprovalsPage() {
    const t = useT();
    const [busyId, setBusyId] = useState('');
    const retry = useAutoRetry(() => api.pendingApprovals(), []);

    if (retry.loading && !retry.data) return <SkeletonLines rows={4} />;
    if (retry.error) return <ErrorView message={retry.error.message} onRetry={retry.manualRetry} />;

    const approvals = retry.data?.approvals ?? [];

    async function confirm(actionId: string, approved: boolean, scope?: string, acceptedBlocks?: number[]) {
        if (busyId) return;
        setBusyId(actionId);
        try {
            const res = await api.confirmApproval(actionId, approved, scope, acceptedBlocks);
            if (res.confirmed) {
                sdk.toast.show({
                    level: 'success',
                    message: approved
                        ? acceptedBlocks
                            ? t('已批准 (按所选变更块)', 'Approved (selected blocks)')
                            : scope && scope !== '*'
                              ? t('已批准 (仅本次)', 'Approved (once)')
                              : t('已批准 (始终)', 'Approved (always)')
                        : t('已拒绝', 'Rejected'),
                });
            } else {
                sdk.toast.show({
                    level: 'warning',
                    message:
                        res.reason ||
                        t(
                            'confirm 未生效 (guard 未启用或队列已变)',
                            'Confirm not applied (guard disabled or queue changed)'
                        ),
                });
            }
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] approval confirm failed', e);
            sdk.toast.show({ level: 'error', message: t('操作失败', 'Operation failed') });
        } finally {
            setBusyId('');
        }
    }

    return (
        <div className="mx-auto max-w-3xl">
            <div className="flex items-end justify-between pb-2">
                <div className="text-sm font-medium text-text-primary">{t('审批队列', 'Approval queue')}</div>
                <div className="text-xs text-text-muted">{approvals.length}</div>
            </div>
            {approvals.length === 0 ? (
                <EmptyState
                    title={t('暂无待审批', 'Nothing pending')}
                    description={t('高风险操作 (guard L3) 出现时显示在此', 'High-risk actions (guard L3) appear here')}
                />
            ) : (
                <div className="space-y-2">
                    {approvals.map((a) => (
                        <ApprovalCard
                            key={a.action_id}
                            approval={a}
                            busy={busyId === a.action_id}
                            onConfirm={confirm}
                        />
                    ))}
                </div>
            )}
        </div>
    );
}

function paramScope(a: GuardApproval): string {
    const p = a.params ?? {};
    const cmd = typeof p.command === 'string' ? p.command : '';
    if (cmd) return `command:${cmd.split(/\s+/)[0]}`;
    for (const key of ['path', 'output_path', 'save_path', 'source_path']) {
        const v = p[key];
        if (typeof v === 'string' && v) return `file:${v.replace(/^~(?=\/)/, '')}/**`;
    }
    return '*';
}

function ParamBlocks({
    approval,
    accepted,
    onToggle,
}: { approval: GuardApproval; accepted?: Set<number>; onToggle?: (idx: number) => void }) {
    const t = useT();
    const p = approval.params ?? {};
    const entries = Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== '');
    if (entries.length === 0) return null;
    const cmd = typeof p.command === 'string' ? p.command : '';
    const blocks = approval.diff_blocks ?? [];
    const changeBlocks = blocks.filter((b) => b.tag !== 'equal');
    const oldStr = typeof p.old_string === 'string' ? p.old_string : '';
    const newStr = typeof p.new_string === 'string' ? p.new_string : '';
    return (
        <div className="mt-2 space-y-1.5">
            {cmd ? (
                <div className="rounded-md border border-border bg-bg px-2 py-1.5">
                    <div className="text-[10px] text-text-muted">{t('命令', 'Command')}</div>
                    <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-text-primary">
                        {cmd}
                    </pre>
                </div>
            ) : null}
            {changeBlocks.length > 0 ? (
                <div className="rounded-md border border-border bg-bg px-2 py-1.5">
                    <div className="text-[10px] text-text-muted">
                        {t(`变更 diff (${changeBlocks.length} 块)`, `Change diff (${changeBlocks.length} blocks)`)}
                    </div>
                    <div className="mt-1 space-y-1.5">
                        {blocks.map((b) => (
                            <DiffBlockView key={b.index} block={b} accepted={accepted} onToggle={onToggle} />
                        ))}
                    </div>
                </div>
            ) : oldStr || newStr ? (
                <div className="rounded-md border border-border bg-bg px-2 py-1.5">
                    <div className="text-[10px] text-text-muted">{t('变更内容', 'Change')}</div>
                    {oldStr ? (
                        <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-error">{`- ${oldStr}`}</pre>
                    ) : null}
                    {newStr ? (
                        <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-success">{`+ ${newStr}`}</pre>
                    ) : null}
                </div>
            ) : null}
            <div className="grid grid-cols-1 gap-x-3 gap-y-0.5 sm:grid-cols-2">
                {entries
                    .filter(([k]) => k !== 'command' && k !== 'old_string' && k !== 'new_string')
                    .map(([k, v]) => (
                        <div key={k} className="flex min-w-0 gap-1 text-[11px]">
                            <span className="shrink-0 text-text-muted">{k}</span>
                            <span className="min-w-0 flex-1 truncate font-mono text-text-primary">{String(v)}</span>
                        </div>
                    ))}
            </div>
        </div>
    );
}

function DiffBlockView({
    block,
    accepted,
    onToggle,
}: { block: DiffBlock; accepted?: Set<number>; onToggle?: (idx: number) => void }) {
    const t = useT();
    if (block.tag === 'equal') {
        return (
            <pre className="overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-text-muted">{block.old}</pre>
        );
    }
    const isAccepted = accepted ? accepted.has(block.index) : true;
    const tagLabel =
        block.tag === 'insert'
            ? t('新增', 'insert')
            : block.tag === 'delete'
              ? t('删除', 'delete')
              : t('替换', 'replace');
    return (
        <div
            className={`rounded border px-1.5 py-1 ${isAccepted ? 'border-border' : 'border-dashed border-warning opacity-60'}`}
        >
            <div className="flex items-center gap-2">
                <span className="font-mono text-[10px] text-text-muted">
                    #{block.index + 1} {tagLabel}
                </span>
                {!isAccepted ? (
                    <span className="rounded border border-warning px-1 text-[10px] text-warning">
                        {t('已拒绝', 'rejected')}
                    </span>
                ) : null}
                {onToggle ? (
                    <span className="ml-auto flex gap-1">
                        <button
                            type="button"
                            className={`rounded px-1.5 py-0.5 text-[10px] ${isAccepted ? 'bg-success/15 text-success' : 'border border-border text-text-muted hover:bg-bg-muted'}`}
                            disabled={isAccepted}
                            onClick={() => onToggle(block.index)}
                        >
                            {t('接受此块', 'Accept')}
                        </button>
                        <button
                            type="button"
                            className={`rounded px-1.5 py-0.5 text-[10px] ${!isAccepted ? 'bg-error/15 text-error' : 'border border-border text-text-muted hover:bg-bg-muted'}`}
                            disabled={!isAccepted}
                            onClick={() => onToggle(block.index)}
                        >
                            {t('拒绝此块', 'Reject')}
                        </button>
                    </span>
                ) : null}
            </div>
            {block.old ? (
                <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-error">{`- ${block.old}`}</pre>
            ) : null}
            {block.new ? (
                <pre className="mt-0.5 overflow-x-auto whitespace-pre-wrap font-mono text-[11px] text-success">{`+ ${block.new}`}</pre>
            ) : null}
        </div>
    );
}

function ApprovalCard({
    approval,
    busy,
    onConfirm,
}: {
    approval: GuardApproval;
    busy: boolean;
    onConfirm: (actionId: string, approved: boolean, scope?: string, acceptedBlocks?: number[]) => void;
}) {
    const t = useT();
    const scope = paramScope(approval);
    const ctx = [approval.workflow, approval.node_id].filter(Boolean).join(' / ');
    // #142: 分块选择状态 — 默认全接受, 拒绝块回退原文; 部分接受时 confirm 携带 accepted_blocks
    const nonEqual = (approval.diff_blocks ?? []).filter((b) => b.tag !== 'equal').map((b) => b.index);
    const [accepted, setAccepted] = useState<Set<number>>(() => new Set(nonEqual));
    const partial = nonEqual.length > 0 && accepted.size < nonEqual.length;
    function toggle(idx: number) {
        setAccepted((prev) => {
            const next = new Set(prev);
            if (next.has(idx)) next.delete(idx);
            else next.add(idx);
            return next;
        });
    }
    return (
        <div className="rounded-lg border border-border bg-bg-elevated p-3">
            <div className="flex flex-wrap items-center gap-2">
                <StatusBadge status="warning" label={approval.source === 'local' ? 'CONFIRM' : 'L3'} />
                <span className="text-sm font-medium text-text-primary">{approval.tool_name}</span>
                <span className="ml-auto shrink-0 font-mono text-[10px] text-text-muted">{approval.action_id}</span>
            </div>
            {ctx ? <div className="mt-1 text-[11px] text-text-muted">{ctx}</div> : null}
            <ParamBlocks approval={approval} accepted={accepted} onToggle={toggle} />
            {partial ? (
                <div className="mt-1 text-[11px] text-warning">
                    {t(
                        `已拒绝 ${nonEqual.length - accepted.size} 个变更块, 仅本次允许将按剩余块窄化编辑`,
                        `${nonEqual.length - accepted.size} block(s) rejected — allow once applies remaining blocks only`
                    )}
                </div>
            ) : null}
            <div className="mt-3 flex flex-wrap justify-end gap-2">
                <button
                    type="button"
                    className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-bg-muted disabled:opacity-50"
                    disabled={busy}
                    onClick={() => onConfirm(approval.action_id, false)}
                >
                    {t('拒绝', 'Deny')}
                </button>
                <button
                    type="button"
                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                    disabled={busy}
                    onClick={() => onConfirm(approval.action_id, true, scope, partial ? [...accepted] : undefined)}
                >
                    {t('仅本次允许', 'Allow once')}
                </button>
                <button
                    type="button"
                    className="rounded-md bg-brand px-2 py-1 text-xs text-white hover:opacity-90 disabled:opacity-50"
                    disabled={busy}
                    onClick={() => onConfirm(approval.action_id, true)}
                >
                    {t('始终允许', 'Always allow')}
                </button>
            </div>
        </div>
    );
}

function ErrorView({ message, onRetry }: { message: string; onRetry: () => void }) {
    const t = useT();
    return <ErrorState message={message || t('加载失败', 'Load failed')} onRetry={onRetry} />;
}
