import { ConfirmDialog, DataTable, EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type WorkflowListItem, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

function fmtTime(ts: number, lang: 'zh' | 'en'): string {
    if (!ts) return '—';
    return new Date(ts * 1000).toLocaleString(lang === 'en' ? 'en-US' : 'zh-CN');
}

export function WorkflowsPage({ onEdit }: { onEdit: (id: string) => void }) {
    const t = useT();
    const lang = sdk.i18n.lang();
    const [reloadKey, setReloadKey] = useState(0);
    const [pendingDelete, setPendingDelete] = useState<WorkflowListItem | null>(null);
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(() => api.workflows(), [reloadKey]);

    async function runFlow(row: WorkflowListItem) {
        try {
            const res = await api.startRun({ workflow_id: row.id, name: row.name });
            console.info(`[cowork-web] run started: ${res.run_id}`);
            window.location.hash = '#/runs';
        } catch (e) {
            console.error('[cowork-web] run failed', e);
            sdk.toast.show({ level: 'error', message: t('运行提交失败', 'Failed to submit run') }); // i18n: user-facing
        }
    }

    async function confirmDelete() {
        if (!pendingDelete) return;
        try {
            await api.deleteWorkflow(pendingDelete.id);
            console.info(`[cowork-web] workflow deleted: ${pendingDelete.id}`);
            setPendingDelete(null);
            setReloadKey((k) => k + 1);
        } catch (e) {
            console.error('[cowork-web] delete failed', e);
            sdk.toast.show({ level: 'error', message: t('删除失败', 'Delete failed') }); // i18n: user-facing
        }
    }

    if (loading && !data) return <SkeletonLines rows={5} />;
    if (error)
        return (
            <ErrorState
                message={error.message}
                retryLabel={t('重试', 'Retry')} // i18n: user-facing
                onRetry={manualRetry}
                retrying={attempt > 0 ? attempt : undefined}
            />
        );

    const rows = data?.workflows ?? [];
    return (
        <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between">
                <div>
                    <div className="text-lg font-medium text-text-primary">
                        {t('工作流', 'Workflows') /*// i18n: user-facing*/}
                    </div>
                    <div className="text-xs text-text-muted">
                        {t('已保存的 DAG 工作流定义', 'Saved DAG workflow definitions') /*// i18n: user-facing*/}
                    </div>
                </div>
                <button
                    type="button"
                    className="rounded-md bg-brand px-3 py-1.5 text-sm text-white hover:opacity-90"
                    onClick={() => {
                        window.location.hash = '#/canvas';
                    }}
                >
                    {t('新建工作流', 'New workflow') /*// i18n: user-facing*/}
                </button>
            </div>
            <DataTable<WorkflowListItem>
                columns={[
                    {
                        key: 'name',
                        header: t('名称', 'Name'), // i18n: user-facing
                        sortable: true,
                        render: (r) => <span className="text-text-primary">{r.name}</span>,
                    },
                    { key: 'node_count', header: t('节点数', 'Nodes'), sortable: true }, // i18n: user-facing
                    {
                        key: 'updated_at',
                        header: t('更新时间', 'Updated'), // i18n: user-facing
                        sortable: true,
                        render: (r) => <span className="font-mono text-xs">{fmtTime(r.updated_at, lang)}</span>,
                    },
                    {
                        key: 'actions',
                        header: '',
                        render: (r) => (
                            <div className="flex justify-end gap-2">
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => void runFlow(r)}
                                >
                                    {t('运行', 'Run') /*// i18n: user-facing*/}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => onEdit(r.id)}
                                >
                                    {t('编辑', 'Edit') /*// i18n: user-facing*/}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-error px-2 py-1 text-xs text-error hover:bg-error/10"
                                    onClick={() => setPendingDelete(r)}
                                >
                                    {t('删除', 'Delete') /*// i18n: user-facing*/}
                                </button>
                            </div>
                        ),
                    },
                ]}
                rows={rows}
                rowKey={(r) => r.id}
                emptyState={
                    <EmptyState
                        title={t('暂无工作流', 'No workflows yet')} // i18n: user-facing
                        description={t(
                            '点击「新建工作流」在画布上编排第一个 DAG', // i18n: user-facing
                            'Click "New workflow" to compose your first DAG on the canvas'
                        )}
                        action={
                            <button
                                type="button"
                                className="rounded-md bg-brand px-3 py-1.5 text-sm text-white"
                                onClick={() => {
                                    window.location.hash = '#/canvas';
                                }}
                            >
                                {t('新建工作流', 'New workflow') /*// i18n: user-facing*/}
                            </button>
                        }
                    />
                }
            />
            <ConfirmDialog
                open={pendingDelete !== null}
                title={t('删除工作流', 'Delete workflow')} // i18n: user-facing
                description={t(
                    `将永久删除「${pendingDelete?.name ?? ''}」, 该操作不可撤销。`, // i18n: user-facing
                    `This permanently deletes "${pendingDelete?.name ?? ''}". This cannot be undone.`
                )}
                danger
                confirmName={pendingDelete?.id}
                confirmLabel={t('删除', 'Delete')} // i18n: user-facing
                cancelLabel={t('取消', 'Cancel')} // i18n: user-facing
                onConfirm={() => void confirmDelete()}
                onCancel={() => setPendingDelete(null)}
            />
        </div>
    );
}
