// Team console: roster / pool / mailbox (web capability completion)
import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';

type Agent = { agent_id: string; name?: string; workflow_id?: string; team?: string; enabled?: boolean };
type Message = { id?: string; sender?: string; recipient?: string; body?: string; kind?: string; created_at?: string };

export default function TeamPage() {
    const [running, setRunning] = useState(false);
    const [agents, setAgents] = useState<Agent[]>([]);
    const [inboxAgent, setInboxAgent] = useState('');
    const [messages, setMessages] = useState<Message[]>([]);
    const [sender, setSender] = useState('');
    const [recipient, setRecipient] = useState('');
    const [body, setBody] = useState('');
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);

    const reload = useCallback(async () => {
        try {
            const [st, roster] = await Promise.all([api.teamStatus(), api.teamRoster()]);
            setRunning(Boolean(st.running));
            setAgents((roster.agents as Agent[]) ?? []);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        }
    }, []);

    const loadInbox = useCallback(async (agentId: string) => {
        if (!agentId) return;
        try {
            const resp = await api.teamInbox(agentId, 0);
            setMessages((resp.messages as Message[]) ?? []);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        }
    }, []);

    useEffect(() => {
        void reload();
        document.title = 'Team — Cowork';
    }, [reload]);

    const togglePool = async () => {
        setBusy(true);
        try {
            const resp = await api.teamPool(!running);
            setRunning(Boolean(resp.running));
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const register = async () => {
        const id = prompt('agent_id?');
        if (!id) return;
        setBusy(true);
        try {
            await api.teamRegister({ agent_id: id.trim() });
            await reload();
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    const send = async () => {
        if (!sender.trim() || !recipient.trim() || !body.trim()) return;
        setBusy(true);
        try {
            await api.teamSend({ sender: sender.trim(), recipient: recipient.trim(), body: body.trim() });
            setBody('');
            if (recipient.trim() === inboxAgent) await loadInbox(inboxAgent);
        } catch (e) {
            setError(String(e instanceof Error ? e.message : e));
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="mx-auto max-w-4xl space-y-4 p-4">
            <h1 className="text-lg font-semibold text-zinc-900 dark:text-zinc-100">团队控制台</h1>
            {error && (
                <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300">
                    {error}
                </div>
            )}

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="flex items-center justify-between">
                    <span className="text-sm font-medium text-zinc-700 dark:text-zinc-300">
                        消费池:{' '}
                        <span className={running ? 'text-emerald-600 dark:text-emerald-400' : 'text-zinc-400'}>
                            {running ? '运行中' : '停止'}
                        </span>
                    </span>
                    <div className="flex gap-2">
                        <button
                            type="button"
                            disabled={busy}
                            onClick={() => void togglePool()}
                            className="rounded-xl bg-indigo-600 px-3 py-1.5 text-sm text-white shadow-sm disabled:opacity-50"
                        >
                            {running ? '停止' : '启动'}
                        </button>
                        <button
                            type="button"
                            disabled={busy}
                            onClick={() => void register()}
                            className="rounded-xl border border-zinc-200/80 px-3 py-1.5 text-sm shadow-sm dark:border-zinc-700"
                        >
                            登记 Agent
                        </button>
                    </div>
                </div>
                <ul className="mt-3 space-y-1 text-sm dark:text-zinc-200">
                    {agents.map((a, i) => (
                        <li
                            key={`${a.agent_id}-${i}`}
                            className="flex items-center justify-between border-b border-zinc-100 py-1 dark:border-zinc-800"
                        >
                            <span className="font-mono text-xs">
                                {a.agent_id} {a.name ? `(${a.name})` : ''} {a.enabled === false ? '· disabled' : ''}
                            </span>
                            <span className="flex gap-2">
                                <button
                                    type="button"
                                    onClick={() => {
                                        setInboxAgent(a.agent_id);
                                        void loadInbox(a.agent_id);
                                    }}
                                    className="text-xs text-indigo-500"
                                >
                                    收件箱
                                </button>
                                <button
                                    type="button"
                                    disabled={busy}
                                    onClick={async () => {
                                        await api.teamRemove(a.agent_id);
                                        await reload();
                                    }}
                                    className="text-xs text-zinc-400 hover:text-red-500"
                                >
                                    移除
                                </button>
                            </span>
                        </li>
                    ))}
                    {agents.length === 0 && <li className="py-2 text-xs text-zinc-400">花名册为空 — 先登记 agent</li>}
                </ul>
            </div>

            <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                <div className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">发消息</div>
                <div className="flex flex-wrap gap-2 text-sm">
                    <input
                        value={sender}
                        onChange={(e) => setSender(e.target.value)}
                        placeholder="sender"
                        className="w-32 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <input
                        value={recipient}
                        onChange={(e) => setRecipient(e.target.value)}
                        placeholder="recipient"
                        className="w-32 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <input
                        value={body}
                        onChange={(e) => setBody(e.target.value)}
                        placeholder="消息内容"
                        className="flex-1 rounded-xl border border-zinc-200/80 bg-white px-3 py-2 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100"
                    />
                    <button
                        type="button"
                        disabled={busy}
                        onClick={() => void send()}
                        className="rounded-xl bg-indigo-600 px-4 py-2 font-medium text-white shadow-sm disabled:opacity-50"
                    >
                        发送
                    </button>
                </div>
            </div>

            {inboxAgent && (
                <div className="rounded-2xl border border-zinc-200/80 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
                    <div className="mb-2 text-sm font-medium text-zinc-700 dark:text-zinc-300">
                        收件箱: {inboxAgent}
                    </div>
                    <ul className="space-y-1 text-xs dark:text-zinc-300">
                        {messages.map((m, i) => (
                            <li key={i} className="border-b border-zinc-100 py-1 dark:border-zinc-800">
                                <span className="text-zinc-400">
                                    {m.sender} → {m.recipient}
                                </span>{' '}
                                {m.body}
                            </li>
                        ))}
                        {messages.length === 0 && <li className="text-zinc-400">空</li>}
                    </ul>
                </div>
            )}
        </div>
    );
}
