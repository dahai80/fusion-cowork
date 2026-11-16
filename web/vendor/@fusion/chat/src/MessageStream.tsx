import { useEffect, useRef, useState } from 'react';
import type { ChatMember, ChatMessage } from './types';

function SenderName(msg: ChatMessage, members: ChatMember[]): string {
    if (msg.senderName) return msg.senderName;
    const m = members.find((x) => x.id === msg.sender);
    return m?.name ?? msg.sender;
}

function ReplyPreviewLine(msg: ChatMessage, byId: Map<string, ChatMessage>): string | null {
    if (!msg.replyTo) return null;
    if (msg.replyPreview) return msg.replyPreview;
    const src = byId.get(msg.replyTo);
    if (!src) return null;
    const name = src.senderName ?? src.sender;
    const body = src.body.length > 60 ? `${src.body.slice(0, 60)}…` : src.body;
    return `${name}: ${body}`;
}

function MessageBubble({
    msg,
    members,
    byId,
    selfId,
    canReply,
    onReply,
}: {
    msg: ChatMessage;
    members: ChatMember[];
    byId: Map<string, ChatMessage>;
    selfId?: string;
    canReply: boolean;
    onReply(msg: ChatMessage): void;
}): React.JSX.Element {
    const mine = selfId != null && msg.sender === selfId;
    const preview = ReplyPreviewLine(msg, byId);
    return (
        <div
            className={`group flex flex-col gap-0.5 ${mine ? 'items-end' : 'items-start'}`}
            data-msg-id={msg.id}
        >
            <div className="flex items-baseline gap-2 px-1">
                <span className="text-xs font-medium text-text-primary">
                    {SenderName(msg, members)}
                </span>
                <span className="text-[10px] text-text-muted">
                    {new Date(msg.ts).toLocaleTimeString([], {
                        hour: '2-digit',
                        minute: '2-digit',
                    })}
                </span>
            </div>
            {preview ? (
                <button
                    type="button"
                    onClick={() => onReply(byId.get(msg.replyTo ?? '') ?? msg)}
                    className="max-w-md truncate rounded-lg border border-zinc-200/80 bg-zinc-50 px-2 py-0.5 text-left text-[11px] text-text-muted hover:bg-zinc-100 dark:border-zinc-700/80 dark:bg-zinc-800/60 dark:hover:bg-zinc-800"
                >
                    {preview}
                </button>
            ) : null}
            <div
                className={`max-w-md whitespace-pre-wrap break-words rounded-xl px-3 py-2 text-sm shadow-sm ${
                    mine
                        ? 'bg-indigo-600 text-white'
                        : 'border border-zinc-200/80 bg-white text-text-primary dark:border-zinc-700/80 dark:bg-zinc-900'
                }`}
            >
                {msg.transcript ? <p className="text-sm">{msg.transcript}</p> : null}
                {msg.body ? (
                    <p className={msg.transcript ? 'mt-1 text-xs opacity-80' : ''}>{msg.body}</p>
                ) : null}
                {msg.attachments?.length ? (
                    <ul className="mt-1 flex flex-col gap-0.5">
                        {msg.attachments.map((a) => (
                            <li key={a.ref} className="text-xs underline opacity-80">
                                {a.name ?? a.ref}
                            </li>
                        ))}
                    </ul>
                ) : null}
                {msg.streaming ? <span className="ml-1 inline-block animate-pulse">▍</span> : null}
            </div>
            {canReply && !msg.streaming ? (
                <button
                    type="button"
                    onClick={() => onReply(msg)}
                    className="hidden px-1 text-[11px] text-text-muted hover:text-indigo-500 group-hover:block"
                >
                    reply
                </button>
            ) : null}
        </div>
    );
}

/** Windowed message stream: renders a sliding slice so long histories stay
 *  cheap without pulling a virtualization dependency (issue #157). */
export function MessageStream({
    messages,
    members,
    selfId,
    canReply,
    onReply,
    onLoadOlder,
    hasMore,
}: {
    messages: ChatMessage[];
    members: ChatMember[];
    selfId?: string;
    canReply: boolean;
    onReply(msg: ChatMessage): void;
    onLoadOlder(): void;
    hasMore: boolean;
}): React.JSX.Element {
    const boxRef = useRef<HTMLDivElement>(null);
    const [windowSize, setWindowSize] = useState(50);
    const bottomRef = useRef<HTMLDivElement>(null);
    const byId = new Map(messages.map((m) => [m.id, m]));
    const slice = messages.slice(-windowSize);

    // biome-ignore lint/correctness/useExhaustiveDependencies: effect intentionally tracks message arrival, not ref identity
    useEffect(() => {
        bottomRef.current?.scrollIntoView({ block: 'end' });
    }, [messages]);

    return (
        <div
            ref={boxRef}
            className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4"
            onScroll={(e) => {
                const el = e.currentTarget;
                if (el.scrollTop === 0 && hasMore) {
                    setWindowSize((w) => w + 50);
                    onLoadOlder();
                }
            }}
            data-testid="chat-stream"
        >
            {hasMore ? (
                <div className="text-center text-[11px] text-text-muted">加载更早消息…</div>
            ) : null}
            {slice.map((m) => (
                <MessageBubble
                    key={m.id}
                    msg={m}
                    members={members}
                    byId={byId}
                    selfId={selfId}
                    canReply={canReply}
                    onReply={onReply}
                />
            ))}
            <div ref={bottomRef} />
        </div>
    );
}
