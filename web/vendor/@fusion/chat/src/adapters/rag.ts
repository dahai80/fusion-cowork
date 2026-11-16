import type {
    ChatAdapter,
    ChatCapabilities,
    ChatMember,
    ChatMessage,
    ListPage,
    SendPayload,
} from '../types';

/** Sample adapter binding a RAG session API (chat-standardization §3): plain
 *  Q&A — replies quoted to own previous turn only, no mentions/presence/
 *  attachments/voice. Exists to prove capability gating, not to be fancy. */
export class RagSessionAdapter implements ChatAdapter {
    readonly capabilities: ChatCapabilities = { replies: 'self' };
    private listeners = new Set<(msg: ChatMessage) => void>();
    private turn = 0;

    constructor(
        private baseUrl: string,
        /** Session/Q&A endpoint path, e.g. fusion-rag `/kb/bases/{kb_id}/ask`.
         *  History endpoints vary per deployment; absent history = fresh
         *  session (list degrades to an empty page, never throws). */
        private sessionPath = '/api/v1/session'
    ) {}

    async list(offset: number): Promise<ListPage> {
        try {
            const res = await fetch(`${this.baseUrl}${this.sessionPath}?offset=${offset}`);
            if (!res.ok) throw new Error(String(res.status));
            const data = (await res.json()) as { messages?: ChatMessage[]; hasMore?: boolean };
            return { messages: data.messages ?? [], hasMore: data.hasMore ?? false };
        } catch (err) {
            console.log('[chat:rag] no server-side history, fresh session', err);
            return { messages: [], hasMore: false };
        }
    }

    async send(payload: SendPayload): Promise<{ delivered: boolean; msgId?: string }> {
        this.turn += 1;
        const res = await fetch(`${this.baseUrl}${this.sessionPath}`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ question: payload.body, replyTo: payload.replyTo }),
        });
        if (!res.ok) {
            console.error('[chat:rag] send failed', res.status);
            return { delivered: false };
        }
        const data = (await res.json()) as { answer?: string; msg_id?: string };
        const answer = data.answer ?? '';
        const msg: ChatMessage = {
            id: data.msg_id ?? `msg_rag_${this.turn}`,
            sender: 'rag',
            senderName: 'RAG',
            body: answer,
            ts: Date.now(),
            replyTo: payload.replyTo,
        };
        for (const cb of this.listeners) cb(msg);
        return { delivered: true, msgId: msg.id };
    }

    subscribe(cb: (msg: ChatMessage) => void): () => void {
        this.listeners.add(cb);
        return () => this.listeners.delete(cb);
    }

    async members(): Promise<ChatMember[]> {
        return [
            { id: 'self', name: 'self' },
            { id: 'rag', name: 'RAG' },
        ];
    }
}
