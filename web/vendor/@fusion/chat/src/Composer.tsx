import { Mic, Paperclip, Reply, Send, X } from 'lucide-react';
import { useRef, useState } from 'react';
import { buildSendPayload, detectMention, filterMembers } from './mention';
import type { ChatCapabilities, ChatMember, ChatMessage, ChatRecorder, ChatUpload } from './types';

export function Composer({
    capabilities,
    members,
    replyTo,
    onCancelReply,
    onSend,
    upload,
    recorder,
    disabled,
}: {
    capabilities: ChatCapabilities;
    members: ChatMember[];
    replyTo: ChatMessage | null;
    onCancelReply(): void;
    onSend(payload: {
        body: string;
        replyTo?: string;
        mentions?: string[];
        attachments?: string[];
        audioRef?: string;
        transcript?: string;
    }): Promise<void>;
    upload?: ChatUpload;
    recorder?: ChatRecorder;
    disabled?: boolean;
}): React.JSX.Element {
    const [text, setText] = useState('');
    const [caret, setCaret] = useState(0);
    const [attachmentRefs, setAttachmentRefs] = useState<string[]>([]);
    const [recording, setRecording] = useState(false);
    const [sending, setSending] = useState(false);
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const fileRef = useRef<HTMLInputElement>(null);
    const mq = detectMention(text, caret);
    const mentionMatches = mq.active ? filterMembers(members, mq.query) : [];
    const repliesAllowed = capabilities.replies === true || capabilities.replies === 'self';

    function pickMention(name: string): void {
        const before = text.slice(0, mq.start);
        const after = text.slice(caret);
        setText(`${before}@${name} ${after}`);
        inputRef.current?.focus();
    }

    async function doSend(): Promise<void> {
        if (!text.trim() || sending) return;
        const payload = buildSendPayload(text, capabilities, members, replyTo);
        setSending(true);
        try {
            await onSend({
                ...payload,
                attachments: attachmentRefs.length ? attachmentRefs : undefined,
            });
            setText('');
            setAttachmentRefs([]);
            onCancelReply();
        } finally {
            setSending(false);
        }
    }

    async function startRecording(): Promise<void> {
        if (!recorder || recording) return;
        setRecording(true);
        try {
            await recorder.start();
        } catch (err) {
            console.error('[chat] recorder start failed', err);
            setRecording(false);
        }
    }

    async function stopRecording(): Promise<void> {
        if (!recorder || !recording) return;
        try {
            const { audioRef, transcript } = await recorder.stop();
            await onSend({ body: text.trim(), audioRef, transcript });
            setText('');
        } catch (err) {
            console.error('[chat] recorder stop failed', err);
        } finally {
            setRecording(false);
        }
    }

    async function onFiles(files: FileList | null): Promise<void> {
        if (!upload || !files?.length) return;
        try {
            const refs = await Promise.all([...files].map((f) => upload(f)));
            setAttachmentRefs((prev) => [...prev, ...refs]);
        } catch (err) {
            console.error('[chat] upload failed', err);
        }
    }

    return (
        <div
            className="border-t border-zinc-200/80 p-3 dark:border-zinc-800"
            data-testid="chat-composer"
        >
            {replyTo && repliesAllowed ? (
                <div className="mb-2 flex items-center gap-2 rounded-xl border border-zinc-200/80 bg-zinc-50 px-3 py-1.5 text-xs text-text-muted dark:border-zinc-700/80 dark:bg-zinc-800/60">
                    <Reply className="h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">
                        {replyTo.senderName ?? replyTo.sender}: {replyTo.body}
                    </span>
                    <button
                        type="button"
                        onClick={onCancelReply}
                        aria-label="cancel reply"
                        className="shrink-0"
                    >
                        <X className="h-3.5 w-3.5" />
                    </button>
                </div>
            ) : null}
            {attachmentRefs.length ? (
                <div className="mb-2 flex flex-wrap gap-1">
                    {attachmentRefs.map((r) => (
                        <span
                            key={r}
                            className="flex items-center gap-1 rounded-lg bg-zinc-100 px-2 py-0.5 text-[11px] text-text-muted dark:bg-zinc-800"
                        >
                            {r}
                            <button
                                type="button"
                                aria-label={`remove ${r}`}
                                onClick={() => setAttachmentRefs((p) => p.filter((x) => x !== r))}
                            >
                                <X className="h-3 w-3" />
                            </button>
                        </span>
                    ))}
                </div>
            ) : null}
            {mq.active && mentionMatches.length ? (
                <ul
                    className="mb-2 rounded-xl border border-zinc-200/80 bg-white shadow-sm dark:border-zinc-700/80 dark:bg-zinc-900"
                    data-testid="mention-menu"
                >
                    {mentionMatches.map((m) => (
                        <li key={m.id}>
                            <button
                                type="button"
                                className="w-full px-3 py-1.5 text-left text-sm hover:bg-zinc-50 dark:hover:bg-zinc-800"
                                onClick={() => pickMention(m.name)}
                            >
                                @{m.name}
                            </button>
                        </li>
                    ))}
                </ul>
            ) : null}
            <div className="flex items-end gap-2">
                {capabilities.attachments && upload ? (
                    <>
                        <button
                            type="button"
                            onClick={() => fileRef.current?.click()}
                            aria-label="attach file"
                            className="rounded-xl p-2 text-text-muted hover:bg-zinc-100 hover:text-text-primary dark:hover:bg-zinc-800"
                        >
                            <Paperclip className="h-4 w-4" />
                        </button>
                        <input
                            ref={fileRef}
                            type="file"
                            multiple
                            hidden
                            onChange={(e) => void onFiles(e.target.files)}
                        />
                    </>
                ) : null}
                <textarea
                    ref={inputRef}
                    value={text}
                    rows={1}
                    disabled={disabled}
                    placeholder={recording ? '录音中…' : '输入消息…'}
                    onChange={(e) => setText(e.target.value)}
                    onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
                    onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
                    onKeyDown={(e) => {
                        if (e.key === 'Enter' && !e.shiftKey) {
                            e.preventDefault();
                            void doSend();
                        }
                    }}
                    className="min-h-9 flex-1 resize-none rounded-xl border border-zinc-200/80 bg-white px-3 py-2 text-sm text-text-primary shadow-sm outline-none focus:border-indigo-400 dark:border-zinc-700/80 dark:bg-zinc-900"
                />
                {capabilities.voice && recorder ? (
                    <button
                        type="button"
                        aria-label={recording ? 'stop recording' : 'start recording'}
                        data-testid="voice-button"
                        onClick={() => (recording ? void stopRecording() : void startRecording())}
                        className={`rounded-xl p-2 ${
                            recording
                                ? 'bg-red-600/10 text-red-500'
                                : 'text-text-muted hover:bg-zinc-100 dark:hover:bg-zinc-800'
                        }`}
                    >
                        <Mic className="h-4 w-4" />
                    </button>
                ) : null}
                <button
                    type="button"
                    aria-label="send"
                    data-testid="send-button"
                    onClick={() => void doSend()}
                    disabled={disabled || sending || !text.trim()}
                    className="rounded-xl bg-indigo-600 p-2 text-white shadow-sm hover:bg-indigo-500 disabled:opacity-40"
                >
                    <Send className="h-4 w-4" />
                </button>
            </div>
        </div>
    );
}
