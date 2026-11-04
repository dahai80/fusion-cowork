// System info + desktop cleanup preview (web capability completion)
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';

type Info = {
    version: string;
    python: string;
    platform: string;
    node_count: number;
    disk_free_gb: number | null;
    disk_total_gb: number | null;
    pid: number;
};

export default function SystemPage() {
    const [info, setInfo] = useState<Info | null>(null);
    const [outputs, setOutputs] = useState<Record<string, string>>({});
    const [dryRun, setDryRun] = useState(true);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    const loadInfo = useCallback(async () => {
        try {
            setInfo(await api.systemInfo());
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        }
    }, []);

    useEffect(() => {
        void loadInfo();
        document.title = 'System — Cowork';
    }, [loadInfo]);

    const clean = async () => {
        setBusy(true);
        setError('');
        try {
            const resp = await api.systemClean(dryRun);
            setOutputs(resp.outputs);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const rows: [string, string][] = info
        ? [
              ['版本', info.version],
              ['Python', info.python],
              ['平台', info.platform],
              ['节点数', String(info.node_count)],
              ['磁盘剩余', info.disk_free_gb !== null ? `${info.disk_free_gb} / ${info.disk_total_gb} GB` : '…'],
              ['PID', String(info.pid)],
          ]
        : [];

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">系统</h1>
            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2 dark:text-zinc-200">
                    {rows.map(([k, v]) => (
                        <div
                            key={k}
                            className="flex justify-between gap-4 border-b border-zinc-100 py-1 dark:border-zinc-800"
                        >
                            <dt className="text-zinc-500 dark:text-zinc-400">{k}</dt>
                            <dd className="font-mono text-xs">{v}</dd>
                        </div>
                    ))}
                </dl>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 flex items-center justify-between">
                    <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">桌面 / 下载清理</span>
                    <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                        <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
                        预览模式
                    </label>
                </div>
                <button
                    type="button"
                    disabled={busy}
                    onClick={() => void clean()}
                    className="rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm disabled:opacity-50"
                >
                    {busy ? '执行中…' : dryRun ? '预览清理' : '执行清理'}
                </button>
                {!dryRun && (
                    <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
                        执行模式会移动真实文件 — 先跑预览确认。
                    </p>
                )}
                {Object.keys(outputs).length > 0 && (
                    <ul className="mt-3 space-y-1 text-xs text-zinc-600 dark:text-zinc-400">
                        {Object.entries(outputs).map(([k, v]) => (
                            <li key={k}>
                                {k}: {v}
                            </li>
                        ))}
                    </ul>
                )}
            </div>
        </div>
    );
}
