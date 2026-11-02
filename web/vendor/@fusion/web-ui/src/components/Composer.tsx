import {
    type ChangeEvent as ReactChangeEvent,
    type ClipboardEvent as ReactClipboardEvent,
    type KeyboardEvent as ReactKeyboardEvent,
    type ReactNode,
    type Ref,
    useEffect,
    useRef,
    useState,
} from 'react';
import { StreamingMarkdown } from './StreamingMarkdown';

export interface ComposerOption {
    value: string;
    label: string;
    /** Secondary text under the label (raw engine id) — shown in popovers. */
    hint?: string;
    /** Chat-capable default-list entry; non-curated items surface only via
     *  popover search (issue #87 curation contract). */
    curated?: boolean;
}

export interface ComposerSelectProps {
    value: string;
    options: ComposerOption[];
    onChange(value: string): void;
    /** Placeholder-ish label while no option matches (e.g. default model). */
    fallbackLabel?: string;
}

export interface ComposerActionItem {
    key: string;
    label: string;
    onClick?(): void;
}

export interface ComposerChip {
    key: string;
    label: string;
    onClick(): void;
    /** Visual-only dimming (e.g. module currently unreachable). */
    muted?: boolean;
}

/** One attachment chip (FCS-1 zone 2): label + removable. */
export interface ComposerAttachment {
    key: string;
    label: string;
}

/** ⚙ params popup contents (FCS-1 §1 zone 4 right): every tunable —
 *  model, effort/thinking, extra entries — lives here, never as loose
 *  toolbar buttons ("凡参数一律进 popup，禁散落按钮"). */
export interface ComposerParams {
    model?: ComposerSelectProps;
    effort?: ComposerSelectProps;
    /** Extra rows below the pickers (surface-specific settings). */
    actions?: { items: ComposerActionItem[]; title?: string; emptyLabel?: string };
}

export interface ComposerProps {
    value: string;
    onChange(value: string): void;
    onSubmit(): void;
    /** Esc / ⏹ while generating (FCS-1 §2): turns the send button into a
     *  stop control; without it the composer never enters the stop state. */
    onStop?(): void;
    placeholder?: string;
    ariaLabel?: string;
    /** Zone 1 context bar chips (optional, FCS-1 §1). */
    contextChips?: ComposerChip[];
    /** Zone 2 attachment chips; rendered only with a remove handler. */
    attachments?: ComposerAttachment[];
    onRemoveAttachment?(key: string): void;
    /** 📎 file attach entry (bottom-left, first in fixed order). */
    attach?: { onFiles(files: File[]): void };
    /** 🖼 image entry + ⌘V image-paste-to-chip (multimodal surfaces only). */
    images?: { onImage(file: File): void };
    /** ＠ reference palette (bottom-left, last in fixed order). */
    references?: { items: ComposerActionItem[]; emptyLabel?: string };
    /** `/` command palette — opens on a leading slash (FCS-1 §3). */
    slashCommands?: { items: ComposerActionItem[]; emptyLabel?: string };
    /** ⚙ params popup (bottom-right, before send). */
    params?: ComposerParams;
    /** Zone 5 hint row; false hides it. */
    hint?: string | false;
    /** Appended to the hint while generating (e.g. "· Esc 停止"). */
    stopHint?: string;
    busy?: boolean;
    disabled?: boolean;
    /** icon-only ⬆ (default) or home pill with a fixed label (FCS-1 §2). */
    sendVariant?: 'icon' | 'pill';
    /** Fixed pill text — never swaps with state (label switching forbidden). */
    sendLabel?: string;
    stopLabel?: string;
    textareaRef?: Ref<HTMLTextAreaElement>;
}

/** FCS-1 §4 dark overrides: the design tokens are CSS-var swapped, but the
 *  composer standard pins raw zinc/indigo scales — dark variants ride the
 *  html.dark class the shell theme store already toggles. */
const DARK = '[.dark_&]:';
const BOX_DARK = `${DARK}border-zinc-700/60 ${DARK}bg-zinc-900`;
const TOOL_DARK = `${DARK}text-zinc-400 ${DARK}hover:bg-zinc-800 ${DARK}hover:text-zinc-200`;
const POPUP_DARK = `${DARK}border-zinc-700 ${DARK}bg-zinc-900`;

/** Auto-grow bounds (FCS-1 §4): one 44px row up to eight ~22px rows. */
const TA_MIN = 44;
const TA_MAX = 176;

/** Floating panel shared by the ⚙ params popup, slash palette and reference
 *  palette — anchored above the tool row, closed by outside mousedown or
 *  Escape. */
function ComposerPopup({
    buttonAria,
    buttonContent,
    open,
    onOpenChange,
    anchor = 'left',
    width = 'w-[300px]',
    children,
}: {
    buttonAria: string;
    buttonContent: ReactNode;
    open: boolean;
    onOpenChange(open: boolean): void;
    anchor?: 'left' | 'right';
    width?: string;
    children: (close: () => void) => ReactNode;
}) {
    const ref = useRef<HTMLDivElement | null>(null);
    const close = () => onOpenChange(false);

    useEffect(() => {
        if (!open) return;
        const onDown = (e: MouseEvent) => {
            if (ref.current && !ref.current.contains(e.target as Node)) onOpenChange(false);
        };
        document.addEventListener('mousedown', onDown);
        return () => document.removeEventListener('mousedown', onDown);
    }, [open, onOpenChange]);

    return (
        <div ref={ref}>
            <button
                type="button"
                onClick={() => onOpenChange(!open)}
                aria-expanded={open}
                aria-haspopup="dialog"
                aria-label={buttonAria}
                className={`flex h-8 w-8 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700 ${TOOL_DARK}`}
            >
                {buttonContent}
            </button>
            {open ? (
                <div
                    role="dialog"
                    aria-label={buttonAria}
                    className={`absolute bottom-full z-dropdown mb-2 max-h-[280px] overflow-y-auto rounded-xl border border-zinc-200 bg-white p-1.5 shadow-overlay ${POPUP_DARK} ${width} ${anchor === 'right' ? 'right-2.5' : 'left-2.5'}`}
                >
                    {children(close)}
                </div>
            ) : null}
        </div>
    );
}

function OptionRow({
    option,
    selected,
    onPick,
}: {
    option: ComposerOption;
    selected: boolean;
    onPick(value: string): void;
}) {
    return (
        <button
            key={option.value}
            type="button"
            onClick={() => onPick(option.value)}
            className={`flex w-full items-start justify-between gap-2 rounded-md px-2 py-1.5 text-left hover:bg-zinc-100 ${DARK}hover:bg-zinc-800 ${selected ? 'text-indigo-600' : 'text-zinc-600'} ${DARK}text-zinc-300`}
        >
            <span className="min-w-0">
                <span className="block truncate text-xs">{option.label}</span>
                {option.hint ? (
                    <span className="block truncate text-[10px] text-zinc-400">{option.hint}</span>
                ) : null}
            </span>
            {selected ? (
                <span aria-hidden className="shrink-0 text-xs text-indigo-600">
                    ✓
                </span>
            ) : null}
        </button>
    );
}

/** Model picker body (issue #87 curation): search input + curated-by-default
 *  list; a non-empty query reveals every option (raw ids included). */
function ModelPickerBody({ model, close }: { model: ComposerSelectProps; close(): void }) {
    const [query, setQuery] = useState('');
    const q = query.trim().toLowerCase();
    const visible = q
        ? model.options.filter(
              (o) =>
                  o.label.toLowerCase().includes(q) ||
                  o.value.toLowerCase().includes(q) ||
                  (o.hint ?? '').toLowerCase().includes(q)
          )
        : model.options.filter((o) => o.curated !== false);
    return (
        <>
            <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search models…"
                aria-label="search models"
                className={`mb-1 w-full rounded-md border border-zinc-200 bg-white px-2 py-1.5 text-xs text-zinc-900 outline-none focus:border-indigo-400 ${POPUP_DARK} ${DARK}text-zinc-100`}
            />
            {visible.length === 0 ? (
                <div className="px-2 py-1 text-xs text-zinc-400">No matches</div>
            ) : (
                visible.map((o) => (
                    <OptionRow
                        key={o.value}
                        option={o}
                        selected={o.value === model.value}
                        onPick={(v) => {
                            model.onChange(v);
                            close();
                        }}
                    />
                ))
            )}
        </>
    );
}

function ActionRows({
    items,
    emptyLabel,
    onPick,
}: {
    items: ComposerActionItem[];
    emptyLabel?: string;
    onPick(item: ComposerActionItem): void;
}) {
    if (items.length === 0) {
        return <div className="px-2 py-1 text-xs text-zinc-400">{emptyLabel ?? '—'}</div>;
    }
    return (
        <>
            {items.map((a) => (
                <button
                    key={a.key}
                    type="button"
                    onClick={() => onPick(a)}
                    className={`block w-full rounded-md px-2 py-1.5 text-left text-xs text-zinc-600 hover:bg-zinc-100 hover:text-indigo-600 ${DARK}text-zinc-300 ${DARK}hover:bg-zinc-800`}
                >
                    {a.label}
                </button>
            ))}
        </>
    );
}

/**
 * Shared conversation composer, FCS-1 (Fusion Composer Standard 1101)
 * five-zone anatomy: ① context chips ② attachment chips ③ auto-grow
 * textarea (1→8 rows) ④ tool row — bottom-left input enhancement
 * (📎→🖼→＠, fixed order), bottom-right execution (⚙ params popup → send
 * always rightmost) — ⑤ persistent hint row. Send is icon-only ⬆ 36×36
 * rounded-xl with three states (empty disabled / content enabled /
 * generating becomes ⏹ stop); label switching is forbidden — the home
 * pill keeps one fixed label. Shortcuts (§3): Enter send, Shift+Enter
 * newline, Esc stop-when-busy, leading `/` command palette, `@`
 * reference palette, ⌘V image → attachment chip (multimodal only).
 */
export function Composer({
    value,
    onChange,
    onSubmit,
    onStop,
    placeholder,
    ariaLabel,
    contextChips,
    attachments,
    onRemoveAttachment,
    attach,
    images,
    references,
    slashCommands,
    params,
    hint = 'Enter to send · Shift+Enter new line',
    stopHint,
    busy = false,
    disabled = false,
    sendVariant = 'icon',
    sendLabel = 'Send',
    stopLabel = 'Stop',
    textareaRef,
}: ComposerProps) {
    const [paramsOpen, setParamsOpen] = useState(false);
    const [slashOpen, setSlashOpen] = useState(false);
    const [refOpen, setRefOpen] = useState(false);
    const taInner = useRef<HTMLTextAreaElement | null>(null);
    const attachInput = useRef<HTMLInputElement | null>(null);
    const imageInput = useRef<HTMLInputElement | null>(null);

    const anyPalette = paramsOpen || slashOpen || refOpen;

    // Auto-grow (FCS-1 §1 zone 3): 1→8 rows, scroll inside beyond that.
    // `value` is the re-render trigger; the resize reads the DOM after the
    // controlled textarea has caught up.
    useEffect(() => {
        void value;
        const el = taInner.current;
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = `${Math.min(Math.max(el.scrollHeight, TA_MIN), TA_MAX)}px`;
    }, [value]);

    // Slash palette tracks a leading `/` (FCS-1 §3); reference palette opens
    // on any `@`. Palettes only exist when their items were provided.
    const handleChange = (e: ReactChangeEvent<HTMLTextAreaElement>) => {
        const v = e.target.value;
        onChange(v);
        if (slashCommands) setSlashOpen(v.startsWith('/'));
        if (references) setRefOpen(v.includes('@'));
    };

    const handleKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!busy) onSubmit();
            return;
        }
        if (e.key === 'Escape') {
            if (busy && onStop) {
                e.preventDefault();
                onStop();
            } else if (anyPalette) {
                e.preventDefault();
                setParamsOpen(false);
                setSlashOpen(false);
                setRefOpen(false);
            } else {
                taInner.current?.blur();
            }
        }
        // ⌘K and every other global shortcut passes through untouched.
    };

    // ⌘V image paste → attachment chip (multimodal surfaces only, §3).
    const handlePaste = (e: ReactClipboardEvent<HTMLTextAreaElement>) => {
        if (!images) return;
        const file = Array.from(e.clipboardData.files).find((f) => f.type.startsWith('image/'));
        if (!file) return;
        e.preventDefault();
        images.onImage(file);
        console.log('[composer] image pasted as attachment:', file.name || 'clipboard image');
    };

    const setTaRef = (el: HTMLTextAreaElement | null) => {
        taInner.current = el;
        if (typeof textareaRef === 'function') textareaRef(el);
        else if (textareaRef && typeof textareaRef === 'object')
            (textareaRef as { current: HTMLTextAreaElement | null }).current = el;
    };

    const canSend = value.trim().length > 0;
    const sendColor = busy
        ? `bg-zinc-200 text-zinc-700 hover:bg-zinc-300 ${DARK}bg-zinc-700 ${DARK}text-zinc-200 ${DARK}hover:bg-zinc-600`
        : canSend
          ? 'bg-indigo-600 text-white hover:bg-indigo-500'
          : `bg-zinc-200 text-zinc-400 ${DARK}bg-zinc-700 ${DARK}text-zinc-500`;
    const sendShape =
        sendVariant === 'pill'
            ? 'h-9 w-auto gap-1.5 rounded-xl px-3.5 text-sm font-medium'
            : 'h-9 w-9 rounded-xl text-base';
    const sendButton = busy ? (
        <button
            type="button"
            onClick={onStop}
            disabled={!onStop}
            aria-label={stopLabel}
            title={stopLabel}
            className={`flex shrink-0 items-center justify-center ${sendShape} ${sendColor}`}
        >
            <span aria-hidden>⏹</span>
            {sendVariant === 'pill' ? <span>{sendLabel}</span> : null}
        </button>
    ) : (
        <button
            type="button"
            onClick={onSubmit}
            disabled={disabled || !canSend}
            aria-label={sendLabel}
            title={sendLabel}
            className={`flex shrink-0 items-center justify-center ${sendShape} ${sendColor} disabled:cursor-not-allowed`}
        >
            <span aria-hidden>⬆</span>
            {sendVariant === 'pill' ? <span>{sendLabel}</span> : null}
        </button>
    );

    const chipBase = `rounded-full border border-zinc-200 bg-zinc-50 px-2.5 py-0.5 text-xs text-zinc-600 ${DARK}border-zinc-700 ${DARK}bg-zinc-800 ${DARK}text-zinc-300`;

    return (
        <div className="w-full text-left">
            <div
                className={`relative rounded-2xl border border-zinc-200/80 bg-white shadow-sm focus-within:border-indigo-400/60 focus-within:ring-2 focus-within:ring-indigo-500/30 ${BOX_DARK}`}
            >
                {/* ① context bar */}
                {contextChips && contextChips.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5 px-4 pt-3">
                        {contextChips.map((c) => (
                            <button
                                key={c.key}
                                type="button"
                                onClick={c.onClick}
                                className={`${chipBase} hover:border-indigo-400 hover:text-indigo-600 ${c.muted ? 'opacity-40' : ''}`}
                            >
                                {c.label}
                            </button>
                        ))}
                    </div>
                ) : null}
                {/* ② attachment chips */}
                {attachments && attachments.length > 0 && onRemoveAttachment ? (
                    <div className="flex flex-wrap gap-1.5 px-4 pt-3">
                        {attachments.map((a) => (
                            <span key={a.key} className={`${chipBase} flex items-center gap-1`}>
                                <span aria-hidden>📄</span>
                                {a.label}
                                <button
                                    type="button"
                                    onClick={() => onRemoveAttachment(a.key)}
                                    aria-label={`remove ${a.label}`}
                                    className="text-zinc-400 hover:text-zinc-600"
                                >
                                    ×
                                </button>
                            </span>
                        ))}
                    </div>
                ) : null}
                {/* ③ textarea */}
                <textarea
                    ref={setTaRef}
                    value={value}
                    onChange={handleChange}
                    onKeyDown={handleKeyDown}
                    onPaste={handlePaste}
                    placeholder={placeholder}
                    aria-label={ariaLabel}
                    disabled={disabled}
                    className={`block w-full resize-none border-none bg-transparent px-4 pb-2 pt-3 text-sm text-zinc-900 outline-none placeholder:text-zinc-400 ${DARK}text-zinc-100 ${DARK}placeholder:text-zinc-500`}
                    style={{ height: TA_MIN, maxHeight: TA_MAX }}
                />
                {/* ④ tool row */}
                <div className="flex items-center gap-1 px-2.5 pb-2.5">
                    {attach ? (
                        <>
                            <input
                                ref={attachInput}
                                type="file"
                                multiple
                                className="hidden"
                                onChange={(e) => {
                                    attach.onFiles(Array.from(e.target.files ?? []));
                                    e.target.value = '';
                                }}
                            />
                            <button
                                type="button"
                                onClick={() => attachInput.current?.click()}
                                aria-label="attach files"
                                title="Attach"
                                className={`flex h-8 w-8 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700 ${TOOL_DARK}`}
                            >
                                <span aria-hidden>📎</span>
                            </button>
                        </>
                    ) : null}
                    {images ? (
                        <>
                            <input
                                ref={imageInput}
                                type="file"
                                accept="image/*"
                                className="hidden"
                                onChange={(e) => {
                                    const f = (e.target.files ?? [])[0];
                                    if (f) images.onImage(f);
                                    e.target.value = '';
                                }}
                            />
                            <button
                                type="button"
                                onClick={() => imageInput.current?.click()}
                                aria-label="attach image"
                                title="Image"
                                className={`flex h-8 w-8 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700 ${TOOL_DARK}`}
                            >
                                <span aria-hidden>🖼</span>
                            </button>
                        </>
                    ) : null}
                    {references ? (
                        <ComposerPopup
                            buttonAria="reference"
                            buttonContent={<span aria-hidden>＠</span>}
                            open={refOpen}
                            onOpenChange={setRefOpen}
                        >
                            {(close) => (
                                <ActionRows
                                    items={references.items}
                                    emptyLabel={references.emptyLabel}
                                    onPick={(item) => {
                                        item.onClick?.();
                                        close();
                                    }}
                                />
                            )}
                        </ComposerPopup>
                    ) : null}
                    {slashCommands ? (
                        <ComposerPopup
                            buttonAria="commands"
                            buttonContent={<span aria-hidden>/</span>}
                            open={slashOpen}
                            onOpenChange={setSlashOpen}
                        >
                            {(close) => (
                                <ActionRows
                                    items={slashCommands.items}
                                    emptyLabel={slashCommands.emptyLabel}
                                    onPick={(item) => {
                                        item.onClick?.();
                                        close();
                                    }}
                                />
                            )}
                        </ComposerPopup>
                    ) : null}
                    <span className="flex-1" />
                    {/* ⚙ params popup: every tunable lives here (§1). */}
                    {params ? (
                        <ComposerPopup
                            buttonAria="parameters"
                            buttonContent={<span aria-hidden>⚙</span>}
                            open={paramsOpen}
                            onOpenChange={setParamsOpen}
                            anchor="right"
                            width="w-[320px]"
                        >
                            {() => (
                                <>
                                    {params.model ? (
                                        <div className="mb-1 px-1.5 text-[10px] uppercase tracking-wide text-zinc-400">
                                            Model
                                        </div>
                                    ) : null}
                                    {params.model ? (
                                        <ModelPickerBody
                                            model={params.model}
                                            close={() => setParamsOpen(false)}
                                        />
                                    ) : null}
                                    {params.effort ? (
                                        <div className="mb-1 mt-2 px-1.5 text-[10px] uppercase tracking-wide text-zinc-400">
                                            Thinking
                                        </div>
                                    ) : null}
                                    {params.effort
                                        ? params.effort.options.map((o) => (
                                              <OptionRow
                                                  key={o.value}
                                                  option={o}
                                                  selected={o.value === params.effort?.value}
                                                  onPick={(v) => params.effort?.onChange(v)}
                                              />
                                          ))
                                        : null}
                                    {params.actions && params.actions.items.length > 0 ? (
                                        <div className="mb-1 mt-2 px-1.5 text-[10px] uppercase tracking-wide text-zinc-400">
                                            {params.actions.title ?? 'Actions'}
                                        </div>
                                    ) : null}
                                    {params.actions ? (
                                        <ActionRows
                                            items={params.actions.items}
                                            emptyLabel={params.actions.emptyLabel}
                                            onPick={(item) => {
                                                item.onClick?.();
                                                setParamsOpen(false);
                                            }}
                                        />
                                    ) : null}
                                </>
                            )}
                        </ComposerPopup>
                    ) : null}
                    {sendButton}
                </div>
            </div>
            {/* ⑤ persistent hint row */}
            {hint !== false ? (
                <div
                    className={`mt-1.5 text-center text-[11px] text-zinc-500 ${DARK}text-zinc-400`}
                >
                    {busy && stopHint ? `${hint} ${stopHint}` : hint}
                </div>
            ) : null}
        </div>
    );
}

export type ChatStreamState = 'pending' | 'streaming' | 'done' | 'failed';

export interface ChatStep {
    id: string;
    label: string;
    /** ⚡ mutation (writes) or ◇ read-only — default read-only. */
    kind?: 'mutation' | 'read';
    /** Human duration, e.g. "1.2s". */
    duration?: string;
}

export interface ChatDatum {
    id: string;
    label: string;
    onClick?(): void;
}

export interface ChatCitation {
    id: string;
    label: string;
    href?: string;
}

export interface ChatArtifact {
    id: string;
    label: string;
    href?: string;
}

export interface ChatMessage {
    id: string;
    role: 'user' | 'agent' | 'system' | 'error';
    body: string;
    /** Streaming agent message: rendered via the shared StreamingMarkdown. */
    streaming?: boolean;
    /** Streaming lifecycle (#57): pending = typing indicator, streaming =
     *  markdown + cursor, failed = error block (error token, monospace). */
    state?: ChatStreamState;
    steps?: ChatStep[];
    data?: ChatDatum[];
    citations?: ChatCitation[];
    artifacts?: ChatArtifact[];
}

export interface ChatThreadProps {
    messages: ChatMessage[];
    onCitationClick?(citation: ChatCitation): void;
    onArtifactClick?(artifact: ChatArtifact): void;
    emptyState?: ReactNode;
    /** Scrollable container label (a11y). */
    ariaLabel?: string;
}

/**
 * Shared chat thread (issue #55) — f-ecommerce bubble geometry: user right
 * (bg-muted, radius 16/16/4/16, max-w 82%), agent left (panel bg + border,
 * radius 4/16/16/16, max-w 88%). Agent bodies stream via StreamingMarkdown.
 * Issue #57 adds the output contract: tool-call step rows (⚡/◇ markers +
 * duration), scrollable step-data result lists, citation chips, artifact
 * cards, and the pending/streaming/done/failed lifecycle.
 */
export function ChatThread({
    messages,
    onCitationClick,
    onArtifactClick,
    emptyState,
    ariaLabel,
}: ChatThreadProps) {
    if (messages.length === 0 && emptyState) {
        return <div className="text-sm text-text-secondary">{emptyState}</div>;
    }
    return (
        <div className="flex flex-col gap-1" aria-label={ariaLabel} role="log" aria-live="polite">
            {messages.map((m) => {
                if (m.role === 'error' || (m.role === 'agent' && m.state === 'failed')) {
                    return (
                        <div
                            key={m.id}
                            role="alert"
                            className="max-w-[88%] self-start rounded-[4px_16px_16px_16px] border border-error/50 bg-bg px-3.5 py-2 font-mono text-xs text-error"
                        >
                            {m.body}
                        </div>
                    );
                }
                if (m.role === 'system') {
                    return (
                        <div key={m.id} className="self-center text-xs text-text-muted">
                            {m.body}
                        </div>
                    );
                }
                if (m.role === 'user') {
                    return (
                        <div
                            key={m.id}
                            className="max-w-[82%] self-end rounded-[16px_16px_4px_16px] bg-bg-muted px-3.5 py-2 text-sm whitespace-pre-wrap break-words text-text-primary"
                        >
                            {m.body}
                        </div>
                    );
                }
                return (
                    <div
                        key={m.id}
                        className="max-w-[88%] self-start rounded-[4px_16px_16px_16px] border border-border bg-bg px-3.5 py-2 text-sm text-text-secondary"
                    >
                        {m.state === 'pending' ? (
                            <span
                                className="inline-flex items-center gap-1 py-1"
                                aria-label="typing"
                            >
                                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-muted" />
                                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-muted [animation-delay:150ms]" />
                                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-text-muted [animation-delay:300ms]" />
                            </span>
                        ) : null}
                        {m.steps && m.steps.length > 0 ? (
                            <div className="flex flex-col gap-0.5">
                                {m.steps.map((s) => (
                                    <div
                                        key={s.id}
                                        className="step flex items-center gap-1.5 text-xs text-text-secondary"
                                    >
                                        <span aria-hidden>
                                            {s.kind === 'mutation' ? '⚡' : '◇'}
                                        </span>
                                        <span>{s.label}</span>
                                        {s.duration ? (
                                            <span className="text-text-muted">{s.duration}</span>
                                        ) : null}
                                    </div>
                                ))}
                            </div>
                        ) : null}
                        {m.body ? (
                            m.streaming || m.state === 'streaming' ? (
                                <>
                                    <StreamBody text={m.body} />
                                    <span className="animate-pulse text-text-secondary" aria-hidden>
                                        ▍
                                    </span>
                                </>
                            ) : (
                                m.body
                            )
                        ) : null}
                        {m.data && m.data.length > 0 ? (
                            <div className="step-data mt-1 max-h-40 overflow-y-auto rounded-md border border-border bg-bg-muted">
                                {m.data.map((d) => (
                                    <button
                                        key={d.id}
                                        type="button"
                                        onClick={d.onClick}
                                        className="block w-full px-2.5 py-1.5 text-left text-xs text-text-secondary hover:text-brand"
                                    >
                                        {d.label}
                                    </button>
                                ))}
                            </div>
                        ) : null}
                        {m.citations && m.citations.length > 0 ? (
                            <div className="citation mt-1.5 flex flex-wrap gap-1.5">
                                {m.citations.map((c) => (
                                    <button
                                        key={c.id}
                                        type="button"
                                        onClick={() => onCitationClick?.(c)}
                                        className="rounded-full border border-border bg-bg-muted px-2 py-0.5 text-xs text-text-secondary hover:border-brand hover:text-brand"
                                    >
                                        {c.label}
                                    </button>
                                ))}
                            </div>
                        ) : null}
                        {m.artifacts && m.artifacts.length > 0 ? (
                            <div className="artifact mt-1.5 flex flex-wrap gap-1.5">
                                {m.artifacts.map((a) => (
                                    <button
                                        key={a.id}
                                        type="button"
                                        onClick={() => onArtifactClick?.(a)}
                                        className="flex items-center gap-1.5 rounded-md border border-border bg-bg-muted px-2.5 py-1.5 text-xs text-text-primary hover:border-brand"
                                    >
                                        <span aria-hidden>📄</span>
                                        {a.label}
                                    </button>
                                ))}
                            </div>
                        ) : null}
                    </div>
                );
            })}
        </div>
    );
}

function StreamBody({ text }: { text: string }) {
    return <StreamingMarkdown text={text} />;
}
