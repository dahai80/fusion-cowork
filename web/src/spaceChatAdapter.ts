// issue #496: space chat adapter for the @fusion/chat component.
// The component is plane-agnostic (fusion-web-shell#157) — this adapter maps
// the cowork space chat REST surface onto the ChatAdapter contract. The
// backend plane is the kernel TeamMessage runtime (space/kernel_plane.py);
// @ mentions are validated server-side by kernel member checks.
import type {
    ChatAdapter,
    ChatCapabilities,
    ChatMember,
    ChatMessage,
    ListPage,
    SendPayload,
} from '@fusion/chat';
import { api, type SpaceMessage } from './api';

export class SpaceChatAdapter implements ChatAdapter {
    readonly capabilities: ChatCapabilities = {
        mentions: true,
        replies: true,
        attachments: false,
        voice: false,
        presence: false,
    };

    private spaceId: string;
    private model: string;
    private pollTimer: ReturnType<typeof setInterval> | null = null;

    constructor(spaceId: string, model = '') {
        this.spaceId = spaceId;
        this.model = model;
    }

    async list(offset: number): Promise<ListPage> {
        void offset;
        // Snapshot adapter: history loads through subscribe() polling; list()
        // only seeds the latest window (pagination not wired — hasMore false).
        const msgs = await api.spaceMessages(this.spaceId);
        const messages = (msgs ?? []).map((m) => this.toChatMessage(m));
        return { messages: messages.slice(-50), hasMore: false };
    }

    async send(payload: SendPayload): Promise<{ delivered: boolean; msgId?: string }> {
        const msg = await api.sendSpaceMessage(
            this.spaceId,
            payload.body,
            this.model,
            payload.replyTo,
        );
        return { delivered: true, msgId: msg?.id };
    }

    subscribe(cb: (msg: ChatMessage) => void): () => void {
        // No SSE fan-out per-message yet — poll the snapshot and emit new ids
        const seen = new Set<string>();
        const tick = async () => {
            try {
                const msgs = (await api.spaceMessages(this.spaceId)) ?? [];
                for (const m of msgs) {
                    if (!seen.has(m.id)) {
                        seen.add(m.id);
                        cb(this.toChatMessage(m));
                    }
                }
            } catch (e) {
                console.error('[cowork-web] space chat poll failed', e);
            }
        };
        void tick();
        this.pollTimer = setInterval(() => void tick(), 3000);
        return () => {
            if (this.pollTimer) clearInterval(this.pollTimer);
            this.pollTimer = null;
        };
    }

    async members(): Promise<ChatMember[]> {
        const [members, agents] = await Promise.all([
            api.spaceMembers(this.spaceId).catch(() => []),
            api.spaceAgents(this.spaceId).catch(() => []),
        ]);
        const out: ChatMember[] = (members ?? []).map((m) => ({
            id: m.user_id,
            name: m.display_name || m.user_id,
        }));
        for (const a of agents ?? []) {
            out.push({ id: a.id, name: a.name || a.id });
        }
        return out;
    }

    toChatMessage(m: SpaceMessage): ChatMessage {
        return spaceMessageToChat(m);
    }
}

/** Pure mapping SpaceMessage (kernel-plane read model) -> ChatMessage. */
export function spaceMessageToChat(m: SpaceMessage): ChatMessage {
    return {
        id: m.id,
        sender: m.agent_id || m.user_id,
        body: m.content,
        ts: m.created_at ? Date.parse(m.created_at) || 0 : 0,
        replyTo: m.parent_msg_id ?? undefined,
        mentions: (m.metadata?.mentions as string[] | undefined) ?? undefined,
        attachments: (m.attachments as string[] | undefined)?.map((ref) => ({ ref })),
    };
}
