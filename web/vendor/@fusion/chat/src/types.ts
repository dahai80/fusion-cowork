/** Plane-agnostic chat contract (issue #157, chat-standardization §3).
 *  The component never knows which backend it talks to — the host injects an
 *  adapter, and every UI affordance is gated on the adapter's declared
 *  capabilities. No abstraction beyond this interface until a third adapter
 *  exists (anti-over-abstraction gate, roadmap §7.4). */

export interface ChatMember {
    id: string;
    name: string;
    online?: boolean;
}

/** Ref-only discipline: files travel as artifact refs, never as content. */
export interface ChatAttachment {
    ref: string;
    name?: string;
}

export interface ChatMessage {
    id: string;
    sender: string;
    senderName?: string;
    body: string;
    ts: number;
    replyTo?: string;
    /** Summary line for the quote bar; derived from the replied message when
     *  the adapter does not provide one. */
    replyPreview?: string;
    mentions?: string[];
    attachments?: ChatAttachment[];
    audioRef?: string;
    transcript?: string;
    /** True while the adapter keeps appending tokens to body. */
    streaming?: boolean;
}

export interface SendPayload {
    body: string;
    replyTo?: string;
    mentions?: string[];
    attachments?: string[];
    audioRef?: string;
    transcript?: string;
}

export type RepliesCapability = boolean | 'self';

export interface ChatCapabilities {
    mentions?: boolean;
    replies?: RepliesCapability;
    attachments?: boolean;
    voice?: boolean;
    presence?: boolean;
}

export interface ListPage {
    messages: ChatMessage[];
    hasMore: boolean;
}

export type ChatUpload = (file: File) => Promise<string>;

export interface ChatRecorder {
    start(): Promise<void>;
    stop(): Promise<{ audioRef: string; transcript: string }>;
}

/** The entire backend surface. Capabilities NOT declared = the corresponding
 *  UI affordance is never rendered (issue #157 hard gate). */
export interface ChatAdapter {
    readonly capabilities: ChatCapabilities;
    list(offset: number): Promise<ListPage>;
    send(payload: SendPayload): Promise<{ delivered: boolean; msgId?: string }>;
    subscribe(cb: (msg: ChatMessage) => void): () => void;
    members(): Promise<ChatMember[]>;
}
