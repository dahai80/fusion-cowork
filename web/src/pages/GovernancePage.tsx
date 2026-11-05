import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { type RunRecord, api } from '../api';
import { govAuditSummary, govGateProgress, isGovernanceRun } from '../governance';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// issue #302: governance hub — aggregated entry page (D1): launch cards,
// my governance runs with deterministic gate mini-progress, completion card,
// audit summary strip. Data sources all existing APIs; zero new backend.

const LAUNCH_IDS = ['governance_issue_lifecycle', 'governance_batch_audit'];

const STATE_ICON: Record<string, string> = {
    done: '✓',
    waiting: '●',
    failed: '✗',
    pending: '○',
};

export function GovernancePage() {
    const t = useT();
    const runsRetry = useAutoRetry(() => api.runs(), []);
    const [details, setDetails] = useState<Record<string, RunRecord>>({});

    const runs = (runsRetry.data?.runs ?? []).filter((r) => isGovernanceRun(r.workflow_name));

    useEffect(() => {
        let alive = true;
        (async () => {
            for (const r of runs.slice(0, 12)) {
                if (details[r.run_id]) continue;
                try {
                    const d = await api.run(r.run_id);
                    if (alive) setDetails((prev) => ({ ...prev, [r.run_id]: d }));
                } catch (e) {
                    console.error('[cowork-web] governance run detail failed', e);
                }
            }
        })();
        return () => {
            alive = false;
        };
    }, [runsRetry.data]);

    if (runsRetry.loading && !runsRetry.data) return <SkeletonLines rows={6} />;
    if (runsRetry.error) return <ErrorState message={runsRetry.error.message} onRetry={runsRetry.manualRetry} />;

    async function launch(tplId: string) {
        try {
            const detail = (await api.template(tplId)) as unknown as {
                params?: { properties?: Record<string, { type: string; default?: unknown }> };
                workflow?: { name?: string };
            };
            const props = detail.params?.properties ?? {};
            const params: Record<string, unknown> = {};
            for (const [k, spec] of Object.entries(props)) {
                if (spec.default !== undefined) params[k] = spec.default;
            }
            // launch with schema defaults; owner fills real values via the
            // TemplatesPage #294 form when defaults do not apply
            const res = await api.launchTemplate(tplId, params);
            sdk.toast.show({ level: 'success', message: t(`已启动: ${res.run_id}`, `Launched: ${res.run_id}`) });
            window.location.hash = `#/runs/${res.run_id}`;
        } catch (e) {
            console.error('[cowork-web] governance launch failed', e);
            sdk.toast.show({ level: 'error', message: t('启动失败', 'Launch failed') });
        }
    }

    function downloadTrajectory(id: string) {
        const d = details[id];
        if (!d) return;
        const blob = new Blob([JSON.stringify(d, null, 4)], { type: 'application/json' });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `trajectory-${id}.json`;
        a.click();
        URL.revokeObjectURL(a.href);
    }

    const allSteps = Object.values(details).flatMap((d) => d.steps ?? []);
    const audit = govAuditSummary(allSteps);
    const terminal = runs.filter((r) => ['success', 'failed', 'completed_with_denials'].includes(r.status));

    return (
        <div className="mx-auto max-w-5xl space-y-6 p-4">
            <h1 className="text-lg font-semibold">{t('治理', 'Governance')}</h1>

            <section className="space-y-2">
                <h2 className="text-sm font-medium text-text-secondary">{t('启动', 'Launch')}</h2>
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {LAUNCH_IDS.map((id) => (
                        <div key={id} className="rounded-xl border border-border bg-bg p-4 shadow-sm">
                            <div className="text-sm font-medium">{id}</div>
                            <button
                                type="button"
                                className="mt-3 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
                                onClick={() => launch(id)}
                            >
                                {t('启动模板', 'Launch template')}
                            </button>
                        </div>
                    ))}
                </div>
                <p className="text-xs text-text-secondary">
                    {t('带参数启动请到模板页', 'For parameterized launch use the Templates page')}{' '}
                    <a className="text-indigo-500 underline" href="#/templates">
                        #/templates
                    </a>
                </p>
            </section>

            <section className="space-y-2">
                <h2 className="text-sm font-medium text-text-secondary">{t('我的治理运行', 'My governance runs')}</h2>
                {runs.length === 0 ? (
                    <EmptyState title={t('暂无治理运行', 'No governance runs yet')} />
                ) : (
                    <div className="space-y-2">
                        {runs.slice(0, 12).map((r) => {
                            const d = details[r.run_id];
                            const gates = d ? govGateProgress(d.steps ?? []) : [];
                            return (
                                <div
                                    key={r.run_id}
                                    className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-bg px-4 py-3 text-sm shadow-sm"
                                >
                                    <span className="font-mono text-xs text-text-secondary">{r.run_id}</span>
                                    <span>{r.workflow_name}</span>
                                    <span className="flex items-center gap-1 font-mono text-xs">
                                        {gates.map((g) => (
                                            <span
                                                key={g.gate}
                                                className={
                                                    g.state === 'done'
                                                        ? 'text-emerald-600 dark:text-emerald-400'
                                                        : g.state === 'waiting'
                                                          ? 'text-amber-600 dark:text-amber-400'
                                                          : g.state === 'failed'
                                                            ? 'text-red-600 dark:text-red-400'
                                                            : 'text-text-secondary'
                                                }
                                                title={`${g.gate} (${g.node})`}
                                            >
                                                {g.gate}
                                                {STATE_ICON[g.state]}
                                            </span>
                                        ))}
                                    </span>
                                    {d?.hitl_pending ? (
                                        <a className="text-xs text-indigo-500 underline" href="#/approvals">
                                            {t('去批 ↗', 'Approve ↗')}
                                        </a>
                                    ) : null}
                                    <button
                                        type="button"
                                        className="text-xs text-indigo-500 underline"
                                        onClick={() => downloadTrajectory(r.run_id)}
                                    >
                                        {t('轨迹↓', 'Trajectory↓')}
                                    </button>
                                    <a className="text-xs text-indigo-500 underline" href={`#/runs/${r.run_id}`}>
                                        {t('详情↗', 'Detail↗')}
                                    </a>
                                </div>
                            );
                        })}
                    </div>
                )}
            </section>

            {terminal.length > 0 ? (
                <section className="space-y-2">
                    <h2 className="text-sm font-medium text-text-secondary">{t('已完成', 'Completed')}</h2>
                    {terminal.slice(0, 5).map((r) => {
                        const d = details[r.run_id];
                        const attr = (d?.steps ?? []).flatMap((s) =>
                            s.output_data?.hitl_attribution ? [s.output_data.hitl_attribution] : []
                        );
                        return (
                            <div
                                key={r.run_id}
                                className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-bg px-4 py-3 text-sm shadow-sm"
                            >
                                <span className="font-mono text-xs text-text-secondary">{r.run_id}</span>
                                <span className={r.status === 'success' ? 'text-emerald-600' : 'text-amber-600'}>
                                    {r.status}
                                </span>
                                <span className="text-xs text-text-secondary">
                                    {t('审批台账', 'Approval ledger')} × {attr.length}
                                </span>
                                <a className="text-xs text-indigo-500 underline" href={`#/runs/${r.run_id}`}>
                                    {t('详情↗', 'Detail↗')}
                                </a>
                                <button
                                    type="button"
                                    className="text-xs text-indigo-500 underline"
                                    onClick={() => downloadTrajectory(r.run_id)}
                                >
                                    {t('轨迹↓', 'Trajectory↓')}
                                </button>
                            </div>
                        );
                    })}
                </section>
            ) : null}

            <section className="space-y-2">
                <h2 className="text-sm font-medium text-text-secondary">{t('审计摘要', 'Audit summary')}</h2>
                <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-bg px-4 py-3 text-sm shadow-sm">
                    <span>
                        {t('决定数', 'Decisions')}: <span className="font-mono">{audit.decisions}</span>
                    </span>
                    {Object.entries(audit.byDecidedBy).map(([who, n]) => (
                        <span key={who} className="text-xs text-text-secondary">
                            {who}: {n}
                        </span>
                    ))}
                    <a className="text-xs text-indigo-500 underline" href="#/governance?tab=audit">
                        {t('审批台账 ↗', 'Approval ledger ↗')}
                    </a>
                </div>
            </section>
        </div>
    );
}
