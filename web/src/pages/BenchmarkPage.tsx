// Model/node benchmark runner (web capability completion)
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';

type BenchResult = { node_name: string; status: string; latency_ms: number; error: string };

export default function BenchmarkPage() {
    const [node, setNode] = useState('');
    const [params, setParams] = useState('{}');
    const [repeats, setRepeats] = useState(3);
    const [results, setResults] = useState<BenchResult[]>([]);
    const [running, setRunning] = useState(false);
    const [error, setError] = useState('');
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const refresh = useCallback(async () => {
        try {
            const st = await api.benchmarkStatus();
            setRunning(st.running);
            setResults(st.results);
            if (!st.running && pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
            }
            if (st.error) setError(st.error);
        } catch {
            /* poll errors surface on next tick */
        }
    }, []);

    useEffect(() => {
        void refresh();
        document.title = 'Benchmark — Cowork';
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [refresh]);

    const run = async () => {
        setError('');
        let parsed: Record<string, unknown> = {};
        if (params.trim()) {
            try {
                parsed = JSON.parse(params);
            } catch {
                setError('params must be valid JSON');
                return;
            }
        }
        setRunning(true);
        try {
            await api.benchmarkRun({ specs: [{ node: node.trim(), params: parsed }], repeats, wait: true });
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        }
        await refresh();
        pollRef.current = setInterval(() => void refresh(), 2000);
    };

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">节点基准测试</h1>
            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="flex flex-wrap items-center gap-2 text-sm">
                    <input
                        value={node}
                        onChange={(e) => setNode(e.target.value)}
                        placeholder="node 名 (如 shell_exec)"
                        className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <input
                        value={params}
                        onChange={(e) => setParams(e.target.value)}
                        placeholder='params JSON, 如 {"command":"true"}'
                        className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 font-mono text-xs dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                        repeats
                        <input
                            type="number"
                            min={1}
                            max={10}
                            value={repeats}
                            onChange={(e) => setRepeats(Number(e.target.value) || 3)}
                            className="w-16 rounded-lg border border-zinc-200/80 px-2 py-1 dark:border-zinc-700 dark:bg-zinc-800"
                        />
                    </label>
                    <button
                        type="button"
                        disabled={running || !node.trim()}
                        onClick={() => void run()}
                        className="rounded-xl bg-indigo-600 px-4 py-2 font-medium text-white shadow-sm disabled:opacity-50"
                    >
                        {running ? '运行中…' : '运行'}
                    </button>
                </div>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <table className="w-full text-left text-sm dark:text-zinc-200">
                    <thead className="text-xs text-zinc-500 dark:text-zinc-400">
                        <tr>
                            <th className="py-1">node</th>
                            <th>status</th>
                            <th>latency (ms)</th>
                            <th>error</th>
                        </tr>
                    </thead>
                    <tbody>
                        {results.map((r, i) => (
                            <tr key={i} className="border-t border-zinc-100 dark:border-zinc-800">
                                <td className="py-1 font-mono text-xs">{r.node_name}</td>
                                <td
                                    className={
                                        r.status === 'success'
                                            ? 'text-emerald-600 dark:text-emerald-400'
                                            : 'text-red-600 dark:text-red-400'
                                    }
                                >
                                    {r.status}
                                </td>
                                <td>{r.latency_ms}</td>
                                <td className="text-xs text-zinc-500 dark:text-zinc-400">{r.error || ''}</td>
                            </tr>
                        ))}
                        {results.length === 0 && (
                            <tr>
                                <td colSpan={4} className="py-3 text-center text-xs text-zinc-400">
                                    {running ? '运行中…' : '暂无结果'}
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
