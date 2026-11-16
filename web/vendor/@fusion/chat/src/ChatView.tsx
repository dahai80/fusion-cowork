import { useCallback, useEffect, useRef, useState } from 'react';
import { Composer } from './Composer';
import { MessageStream } from './MessageStream';
import type {
    ChatAdapter,
    ChatMember,
    ChatMessage,
    ChatRecorder,
    ChatUpload,
    SendPayload,
} from './types';

/** Top-level chat surface: owns adapter state (list/subscribe/members) and
 *  gates every affordance on adapter capabilities (issue #157). */
export function ChatView({
    adapter,
    selfId,
    upload,
    recorder,
    className = '',
}: {
    adapter: ChatAdapter;
    selfId?: string;
    upload?: ChatUpload;
    recorder?: ChatRecorder;
    className?: string;
}): React.JSX.Element {
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [members, setMembers] = useState<ChatMember[]>([]);
    const [hasMore, setHasMore] = useState(false);
    const [replyTo, setReplyTo] = useState<ChatMessage | null>(null);
    const offsetRef = useRef(0);

    const loadOlder = useCallback(async () => {
        const page = await adapter.list(offsetRef.current);
        offsetRef.current += page.messages.length;
        setHasMore(page.hasMore);
        setMessages((prev) => [...page.messages, ...prev]);
    }, [adapter]);

    useEffect(() => {
        let alive = true;
        console.log('[chat] mounting ChatView, capabilities', adapter.capabilities);
        void loadOlder();
        void adapter.members().then((m) => {
            if (alive) setMembers(m);
        });
        const unsub = adapter.subscribe((msg) => {
            if (!alive) return;
            setMessages((prev) => {
                const idx = prev.findIndex((x) => x.id === msg.id);
                if (idx === -1) return [...prev, msg];
                const next = [...prev];
                next[idx] = msg;
                return next;
            });
        });
        return () => {
            alive = false;
            unsub();
        };
    }, [adapter, loadOlder]);

    const canReply =
        adapter.capabilities.replies === true || adapter.capabilities.replies === 'self';
    const selfRepliesOnly = adapter.capabilities.replies === 'self';

    function handleReply(msg: ChatMessage): void {
        if (selfRepliesOnly && selfId != null && msg.sender !== selfId) return;
        setReplyTo(msg);
    }

    async function handleSend(payload: SendPayload): Promise<void> {
        await adapter.send(payload);
    }

    return (
        <div
            className={`flex min-h-0 flex-1 flex-col bg-bg-base ${className}`}
            data-testid="chat-view"
        >
            {adapter.capabilities.presence ? (
                <div
                    className="flex items-center gap-2 border-b border-zinc-200/80 px-4 py-2 dark:border-zinc-800"
                    data-testid="presence-bar"
                >
                    {members.map((m) => (
                        <span
                            key={m.id}
                            className="flex items-center gap-1 text-xs text-text-muted"
                        >
                            <span
                                className={`inline-block h-2 w-2 rounded-full ${m.online ? 'bg-emerald-500' : 'bg-zinc-300 dark:bg-zinc-600'}`}
                                aria-label={m.online ? 'online' : 'offline'}
                            />
                            {m.name}
                        </span>
                    ))}
                </div>
            ) : null}
            <MessageStream
                messages={messages}
                members={members}
                selfId={selfId}
                canReply={canReply}
                onReply={handleReply}
                onLoadOlder={() => void loadOlder()}
                hasMore={hasMore}
            />
            <Composer
                capabilities={adapter.capabilities}
                members={members}
                replyTo={replyTo}
                onCancelReply={() => setReplyTo(null)}
                onSend={handleSend}
                upload={upload}
                recorder={recorder}
            />
        </div>
    );
}
