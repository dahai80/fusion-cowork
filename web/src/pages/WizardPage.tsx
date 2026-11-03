// issue #263: 仓库 onboarding 向导 — GitHub org 一次性初始化
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';

type Plan = { repo: string; branch: string; actions: { op: string; target: string; detail: string }[] };
type ApplyResult = { repo: string; branch: string; applied: { op: string; target: string }[] };

export default function WizardPage() {
    const [org, setOrg] = useState('');
    const [repos, setRepos] = useState<{ name: string; full_name: string }[]>([]);
    const [selected, setSelected] = useState<string[]>([]);
    const [plans, setPlans] = useState<Plan[]>([]);
    const [results, setResults] = useState<ApplyResult[]>([]);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [checks, setChecks] = useState('lint,test');

    const loadRepos = useCallback(async () => {
        setError('');
        setBusy(true);
        try {
            const resp = await api.wizardRepos(org.trim());
            setRepos(resp.repos);
            setSelected([]);
            setPlans([]);
            setResults([]);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    }, [org]);

    const preview = useCallback(async () => {
        if (selected.length === 0) return;
        setError('');
        setBusy(true);
        try {
            const resp = await api.wizardPreview({ org: org.trim(), repos: selected });
            setPlans(resp.plans);
            setResults([]);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    }, [org, selected]);

    const apply = useCallback(async () => {
        if (selected.length === 0) return;
        setError('');
        setBusy(true);
        try {
            const resp = await api.wizardApply({
                org: org.trim(),
                repos: selected,
                protected_checks: checks
                    .split(',')
                    .map((c) => c.trim())
                    .filter(Boolean),
            });
            setResults(resp.results);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    }, [org, selected, checks]);

    const teardown = useCallback(async () => {
        if (selected.length === 0) return;
        setError('');
        setBusy(true);
        try {
            const resp = await api.wizardTeardown({ org: org.trim(), repos: selected });
            setResults([]);
            setPlans([]);
            setRepos((prev) => prev);
            setError(`已卸载 ${resp.results.filter((r) => r.removed.length > 0).length} 个仓库的向导产物`);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    }, [org, selected]);

    useEffect(() => {
        document.title = 'Onboarding 向导 — Cowork';
    }, []);

    const toggle = (full: string) =>
        setSelected((prev) => (prev.includes(full) ? prev.filter((r) => r !== full) : [...prev, full]));

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">仓库 Onboarding 向导</h1>
            <p className="text-sm text-zinc-500 dark:text-zinc-400">
                为 GitHub 仓库统一铺设 label 税 / issue 表单 / CODEOWNERS / 分支保护。Token 走环境变量 GITHUB_TOKEN。
            </p>

            <div className="flex gap-2">
                <input
                    value={org}
                    onChange={(e) => setOrg(e.target.value)}
                    placeholder="org 或用户名"
                    className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 text-sm shadow-sm dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
                />
                <button
                    type="button"
                    onClick={loadRepos}
                    disabled={busy || !org.trim()}
                    className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm disabled:opacity-50"
                >
                    列出仓库
                </button>
            </div>

            {repos.length > 0 && (
                <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                    <div className="mb-2 flex items-center justify-between">
                        <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
                            选择仓库 ({selected.length})
                        </span>
                        <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                            必需检查:
                            <input
                                value={checks}
                                onChange={(e) => setChecks(e.target.value)}
                                className="w-32 rounded-lg border border-zinc-200/80 px-2 py-1 dark:border-zinc-700 dark:bg-zinc-800"
                            />
                        </label>
                    </div>
                    <div className="grid grid-cols-1 gap-1 sm:grid-cols-2">
                        {repos.map((r) => (
                            <label key={r.full_name} className="flex items-center gap-2 text-sm dark:text-zinc-200">
                                <input
                                    type="checkbox"
                                    checked={selected.includes(r.full_name)}
                                    onChange={() => toggle(r.full_name)}
                                />
                                {r.full_name}
                            </label>
                        ))}
                    </div>
                    <div className="mt-3 flex gap-2">
                        <button
                            type="button"
                            onClick={preview}
                            disabled={busy}
                            className="rounded-xl border border-zinc-200/80 px-4 py-2 text-sm shadow-sm dark:border-zinc-700"
                        >
                            预览
                        </button>
                        <button
                            type="button"
                            onClick={apply}
                            disabled={busy}
                            className="rounded-xl bg-emerald-600 px-4 py-2 text-sm font-medium text-white shadow-sm disabled:opacity-50"
                        >
                            应用
                        </button>
                        <button
                            type="button"
                            onClick={teardown}
                            disabled={busy}
                            className="rounded-xl bg-zinc-600 px-4 py-2 text-sm font-medium text-white shadow-sm disabled:opacity-50"
                        >
                            卸载
                        </button>
                    </div>
                </div>
            )}

            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            {plans.map((p) => (
                <div
                    key={p.repo}
                    className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900"
                >
                    <div className="mb-2 text-sm font-medium text-zinc-800 dark:text-zinc-200">
                        {p.repo} ({p.branch})
                    </div>
                    <ul className="space-y-0.5 font-mono text-xs text-zinc-600 dark:text-zinc-400">
                        {p.actions.map((a, i) => (
                            <li key={i}>
                                [{a.op}] {a.target} {a.detail ? `— ${a.detail}` : ''}
                            </li>
                        ))}
                    </ul>
                </div>
            ))}

            {results.map((r) => (
                <div
                    key={r.repo}
                    className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4 text-sm shadow-sm dark:border-emerald-900 dark:bg-emerald-950"
                >
                    <div className="font-medium text-emerald-800 dark:text-emerald-300">
                        {r.repo}: {r.applied.length} 项变更
                    </div>
                    <ul className="mt-1 space-y-0.5 font-mono text-xs text-emerald-700 dark:text-emerald-400">
                        {r.applied.map((a, i) => (
                            <li key={i}>
                                [{a.op}] {a.target}
                            </li>
                        ))}
                    </ul>
                </div>
            ))}
        </div>
    );
}
