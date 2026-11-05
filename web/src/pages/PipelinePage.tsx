import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useRef, useState } from 'react';
import { type RunRecord, api } from '../api';
import { useT } from '../i18n';
import { PIPELINE_LOOPS, PIPELINE_STAGES, isPipelineRun, stageLinks, stageStateOf } from '../pipeline';

// issue #310: delivery pipeline dashboard — S-shaped 3-column topology of the
// product_lifecycle template, node states bound to real run steps, node click
// opens the input/output artifact drawer, prev/next/autoplay step controls.

const COL_OF_STAGE: Record<number, number> = {
    1: 0,
    2: 0,
    3: 0,
    4: 0,
    5: 1,
    6: 1,
    7: 1,
    8: 1,
    9: 2,
    10: 2,
};

const STATE_CLASS: Record<string, string> = {
    done: 'border-emerald-400 bg-emerald-50 dark:bg-emerald-950/40',
    active: 'border-indigo-400 bg-indigo-50 dark:bg-indigo-950/40 animate-pulse',
    off: 'border-border bg-bg opacity-60',
};

// issue #318: no runId — list product_lifecycle runs to pick from (nav entry point)
function PipelineRunPicker() {
    const t = useT();
    const retry = useAutoRetry(() => api.runs(''), []);
    const runs = (retry.data?.runs ?? []).filter((r) => isPipelineRun(r.workflow_name)).slice(0, 20);
    return (
        <div className="mx-auto max-w-3xl space-y-3 p-4">
            <h1 className="text-lg font-semibold">{t('交付管线', 'Delivery pipeline')}</h1>
            <p className="text-xs text-text-secondary">
                {t('选择一次产品生命周期运行查看管线视图', 'Pick a product_lifecycle run to open its pipeline view')}
            </p>
            {retry.loading && !retry.data ? <SkeletonLines rows={6} /> : null}
            {retry.error ? <ErrorState message={retry.error.message} onRetry={retry.manualRetry} /> : null}
            {!retry.loading && !retry.error && runs.length === 0 ? (
                <EmptyState
                    title={t(
                        '暂无产品生命周期运行 — 从「模板」启动 product_lifecycle 模板',
                        'No product_lifecycle runs yet — launch the template from Templates'
                    )}
                />
            ) : null}
            <div className="space-y-2">
                {runs.map((r) => (
                    <button
                        type="button"
                        key={r.run_id}
                        className="w-full rounded-xl border border-border p-3 text-left shadow-sm transition hover:bg-bg-muted"
                        onClick={() => {
                            window.location.hash = `#/pipeline/${r.run_id}`;
                        }}
                    >
                        <div className="flex items-center justify-between text-sm font-medium">
                            <span className="font-mono text-xs">{r.run_id}</span>
                            <span>{r.status}</span>
                        </div>
                        <div className="mt-1 text-xs text-text-secondary">{r.workflow_name}</div>
                    </button>
                ))}
            </div>
        </div>
    );
}

export function PipelinePage({ runId }: { runId: string }) {
    if (!runId) return <PipelineRunPicker />;
    return <PipelineRunView runId={runId} />;
}

function PipelineRunView({ runId }: { runId: string }) {
    const t = useT();
    const retry = useAutoRetry(() => api.run(runId), [runId]);
    const [selected, setSelected] = useState<number | null>(null);
    const [cursor, setCursor] = useState<number | null>(null);
    const [playing, setPlaying] = useState(false);
    const timer = useRef<ReturnType<typeof setInterval> | null>(null);

    const record: RunRecord | null = retry.data ?? null;
    const steps = record?.steps ?? [];
    const total = steps.length;

    useEffect(() => {
        if (!playing) {
            if (timer.current) clearInterval(timer.current);
            timer.current = null;
            return;
        }
        timer.current = setInterval(() => {
            setCursor((c) => {
                const next = (c ?? -1) + 1;
                if (next >= total) {
                    setPlaying(false);
                    return total - 1;
                }
                return next;
            });
        }, 800);
        return () => {
            if (timer.current) clearInterval(timer.current);
            timer.current = null;
        };
    }, [playing, total]);

    if (retry.loading && !retry.data) return <SkeletonLines rows={8} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    // replay cursor: null = live (all steps); number = walk recorded steps
    const visibleSteps = cursor === null ? steps : steps.slice(0, cursor + 1);
    const links = stageLinks;

    function drawerFor(stageId: number) {
        const stage = PIPELINE_STAGES[stageId - 1];
        const stageSteps = visibleSteps.filter((s) => stage.nodeIds.includes(s.node_id));
        const outputs = stageSteps.flatMap((s) => links(s.output_data));
        const inputs = stageSteps.flatMap((s) =>
            Object.entries(s.input_data ?? {}).map(([k, v]) => `${k}: ${JSON.stringify(v).slice(0, 80)}`)
        );
        return { stage, inputs, outputs };
    }

    return (
        <div className="mx-auto max-w-6xl space-y-4 p-4">
            <div className="flex items-center justify-between">
                <h1 className="text-lg font-semibold">
                    {t('交付管线', 'Delivery pipeline')}
                    <span className="ml-2 font-mono text-xs text-text-secondary">{runId}</span>
                </h1>
                <div className="flex items-center gap-2 text-xs">
                    <button
                        type="button"
                        className="rounded-lg border border-border px-2 py-1"
                        onClick={() => {
                            setPlaying(false);
                            setCursor((c) => Math.max(0, (c ?? total - 1) - 1));
                        }}
                    >
                        {t('上一步', 'Prev')}
                    </button>
                    <button
                        type="button"
                        className="rounded-lg border border-border px-2 py-1"
                        onClick={() => {
                            setPlaying(false);
                            setCursor((c) => Math.min(total - 1, (c ?? -1) + 1));
                        }}
                    >
                        {t('下一步', 'Next')}
                    </button>
                    <button
                        type="button"
                        className="rounded-lg border border-border px-2 py-1"
                        onClick={() => {
                            setCursor(0);
                            setPlaying((p) => !p);
                        }}
                    >
                        {playing ? t('暂停', 'Pause') : t('自动播放', 'Autoplay')}
                    </button>
                    <button
                        type="button"
                        className="rounded-lg border border-border px-2 py-1"
                        onClick={() => {
                            setPlaying(false);
                            setCursor(null);
                        }}
                    >
                        {t('实时', 'Live')}
                    </button>
                </div>
            </div>
            {cursor !== null ? (
                <p className="text-xs text-text-secondary">
                    {t('回放', 'Replay')}: {cursor + 1}/{total}
                </p>
            ) : null}

            {total === 0 ? (
                <EmptyState title={t('运行尚无步骤记录', 'Run has no recorded steps yet')} />
            ) : (
                <div className="grid grid-cols-3 gap-4">
                    {[0, 1, 2].map((col) => (
                        <div key={col} className="space-y-3">
                            {PIPELINE_STAGES.filter((s) => COL_OF_STAGE[s.id] === col).map((stage) => {
                                const state = stageStateOf(stage, visibleSteps);
                                return (
                                    <button
                                        type="button"
                                        key={stage.id}
                                        className={`w-full rounded-xl border p-3 text-left shadow-sm transition ${STATE_CLASS[state]}`}
                                        onClick={() => setSelected(stage.id)}
                                    >
                                        <div className="flex items-center justify-between text-sm font-medium">
                                            <span>
                                                {stage.id}. {t(stage.labelZh, stage.labelEn)}
                                            </span>
                                            <span>{state === 'done' ? '✓' : state === 'active' ? '●' : '○'}</span>
                                        </div>
                                        <div className="mt-1 font-mono text-[10px] text-text-secondary">
                                            {stage.nodeIds.join(',') || 'run-input'}
                                        </div>
                                    </button>
                                );
                            })}
                        </div>
                    ))}
                </div>
            )}

            <p className="text-xs text-text-secondary">
                {t('回流环', 'Rejection loops')}:{PIPELINE_LOOPS.map(([from, to]) => ` ${from}→${to}`).join(',')}
            </p>

            {selected !== null ? (
                <div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-bg p-4 shadow-md">
                    <div className="mx-auto max-w-4xl space-y-2">
                        {(() => {
                            const { stage, inputs, outputs } = drawerFor(selected);
                            return (
                                <>
                                    <div className="flex items-center justify-between">
                                        <h3 className="text-sm font-semibold">
                                            {stage.id}. {t(stage.labelZh, stage.labelEn)}
                                        </h3>
                                        <button
                                            type="button"
                                            className="text-xs text-text-secondary underline"
                                            onClick={() => setSelected(null)}
                                        >
                                            {t('关闭', 'Close')}
                                        </button>
                                    </div>
                                    <div className="grid grid-cols-2 gap-4 text-xs">
                                        <div>
                                            <p className="mb-1 font-medium">{t('输入工件', 'Input artifacts')}</p>
                                            <ul className="list-inside list-disc space-y-0.5">
                                                {stage.inputsZh.map((x, i) => (
                                                    <li key={`i${i}`}>{t(x, stage.inputsEn[i] ?? x)}</li>
                                                ))}
                                                {inputs.slice(0, 6).map((x, i) => (
                                                    <li key={`s${i}`} className="font-mono text-[10px]">
                                                        {x}
                                                    </li>
                                                ))}
                                            </ul>
                                        </div>
                                        <div>
                                            <p className="mb-1 font-medium">{t('输出工件', 'Output artifacts')}</p>
                                            <ul className="list-inside list-disc space-y-0.5">
                                                {stage.outputsZh.map((x, i) => (
                                                    <li key={`o${i}`}>{t(x, stage.outputsEn[i] ?? x)}</li>
                                                ))}
                                                {outputs.map((u, i) => (
                                                    <li key={`l${i}`}>
                                                        <a className="text-indigo-500 underline" href={u}>
                                                            {u}
                                                        </a>
                                                    </li>
                                                ))}
                                            </ul>
                                        </div>
                                    </div>
                                </>
                            );
                        })()}
                    </div>
                </div>
            ) : null}
        </div>
    );
}
