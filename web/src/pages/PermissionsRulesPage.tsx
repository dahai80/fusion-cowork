// Permission rules management: level + rule CRUD (web capability completion)
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';

type Rule = { tool_name: string; allowed: boolean; scope: string };

export default function PermissionsRulesPage() {
    const [level, setLevel] = useState('');
    const [rules, setRules] = useState<Rule[]>([]);
    const [toolName, setToolName] = useState('');
    const [scope, setScope] = useState('*');
    const [allowed, setAllowed] = useState(true);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    const reload = useCallback(async () => {
        try {
            const resp = await api.permissionRules();
            setLevel(resp.level);
            setRules(resp.rules);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        }
    }, []);

    useEffect(() => {
        void reload();
        document.title = 'Permission Rules — Cowork';
    }, [reload]);

    const add = async () => {
        if (!toolName.trim()) return;
        setBusy(true);
        setError('');
        try {
            await api.addPermissionRule({ tool_name: toolName.trim(), allowed, scope: scope.trim() || '*' });
            setToolName('');
            await reload();
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const removeRules = async (name: string) => {
        setBusy(true);
        try {
            await api.deletePermissionRules(name);
            await reload();
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const switchLevel = async (lv: string) => {
        setBusy(true);
        try {
            const resp = await api.setPermissionLevel(lv);
            setLevel(resp.level);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const levels = ['confirm', 'manual', 'auto', 'plan', 'bypass'];
    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">权限规则</h1>
            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">级别: {level || '…'}</div>
                <div className="flex flex-wrap gap-2">
                    {levels.map((lv) => (
                        <button
                            key={lv}
                            type="button"
                            disabled={busy || lv === level}
                            onClick={() => void switchLevel(lv)}
                            className={`rounded-xl px-3 py-1.5 text-sm shadow-sm ${lv === level ? 'bg-indigo-600 text-white' : 'border border-zinc-200/80 dark:border-zinc-700'}`}
                        >
                            {lv}
                        </button>
                    ))}
                </div>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
                    <input
                        value={toolName}
                        onChange={(e) => setToolName(e.target.value)}
                        placeholder="tool_name (如 shell_exec)"
                        className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <input
                        value={scope}
                        onChange={(e) => setScope(e.target.value)}
                        placeholder="scope"
                        className="w-32 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <label className="flex items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400">
                        <input type="checkbox" checked={allowed} onChange={(e) => setAllowed(e.target.checked)} />
                        allowed
                    </label>
                    <button
                        type="button"
                        disabled={busy}
                        onClick={() => void add()}
                        className="rounded-xl bg-indigo-600 px-4 py-2 font-medium text-white shadow-sm disabled:opacity-50"
                    >
                        添加
                    </button>
                </div>
                <table className="w-full text-left text-sm dark:text-zinc-200">
                    <thead className="text-xs text-zinc-500 dark:text-zinc-400">
                        <tr>
                            <th className="py-1">tool</th>
                            <th>scope</th>
                            <th>verdict</th>
                            <th />
                        </tr>
                    </thead>
                    <tbody>
                        {rules.map((r, i) => (
                            <tr key={`${r.tool_name}-${i}`} className="border-t border-zinc-100 dark:border-zinc-800">
                                <td className="py-1 font-mono text-xs">{r.tool_name}</td>
                                <td className="font-mono text-xs">{r.scope}</td>
                                <td>
                                    <span
                                        className={
                                            r.allowed
                                                ? 'text-emerald-600 dark:text-emerald-400'
                                                : 'text-red-600 dark:text-red-400'
                                        }
                                    >
                                        {r.allowed ? 'allow' : 'deny'}
                                    </span>
                                </td>
                                <td className="text-right">
                                    <button
                                        type="button"
                                        disabled={busy}
                                        onClick={() => void removeRules(r.tool_name)}
                                        className="text-xs text-zinc-400 hover:text-red-500"
                                    >
                                        删除
                                    </button>
                                </td>
                            </tr>
                        ))}
                        {rules.length === 0 && (
                            <tr>
                                <td colSpan={4} className="py-3 text-center text-xs text-zinc-400">
                                    无持久规则
                                </td>
                            </tr>
                        )}
                    </tbody>
                </table>
            </div>
        </div>
    );
}
