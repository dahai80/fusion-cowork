import { HttpError } from './retry';
import type { DeviceInfo, HostEvent, HostResponse, ModuleRequest, ThemeMode } from './types';

export interface HostBridgeOptions {
    /** Allowed module origin. Default: same origin as the host document. */
    origin?: string;
    /** Token attached to proxied http calls. */
    getToken(): string | null;
    onToast(input: { level: string; message: string; durationMs?: number }): void;
    onActiveTask(tabId: string, task: { name: string } | null): void;
    /** Gateway-proxied fetch used for http relay; host attaches Bearer here. */
    proxyHttp(req: {
        path: string;
        method: string;
        headers?: Record<string, string>;
        body?: unknown;
    }): Promise<{ status: number; data: unknown }>;
}

/** Issue #75: handleRequest returns bare strings ('theme'), plain objects
 * (http results, devices) and __httpError envelopes — only the last kind is
 * an error. `in` on a non-object throws, so guard the type first. */
export function isHttpErrorResult(
    result: unknown
): result is { __httpError: true; status: number; code: string; message: string } {
    return result !== null && typeof result === 'object' && '__httpError' in result;
}

export interface HostBridgeHandle {
    iframeWindow: Window | null;
    destroy(): void;
    notifyTheme(mode: ThemeMode): void;
    notifyVisibility(visible: boolean): void;
    notifyLang(lang: string): void;
    /** True when the module answered at least one heartbeat (SDK integrated). */
    readonly sdkAlive: boolean;
    startHeartbeat(
        onLivenessChange: (alive: boolean) => void,
        intervalMs?: number,
        timeoutMs?: number
    ): void;
    stopHeartbeat(): void;
}

/**
 * Host-side bridge for one module iframe. Validates source/origin on every
 * message; iframe never touches host state directly.
 */
export function createHostBridge(
    tabId: string,
    iframeWindow: Window | null,
    options: HostBridgeOptions
): HostBridgeHandle {
    // Target origin for host->module postMessage. Default: the IFRAME's own
    // origin when it is cross-origin with the host (FUSION_DEV_MODULES dev
    // entries, e.g. :11590 inside :11430) — defaulting to the host document's
    // origin made the browser silently drop every push (theme/lang/http
    // requests) into a cross-origin frame. Same-origin deployments keep the
    // host-origin default; an explicit option still wins.
    let origin = options.origin ?? window.location.origin;
    if (!options.origin && iframeWindow) {
        const usable = (o: string) => /^https?:\/\//.test(o);
        try {
            const frameOrigin = new URL(iframeWindow.location.href).origin;
            // "null" is what about:blank (the pre-load placeholder) reports —
            // postMessage rejects it outright ("Invalid target origin 'null'"),
            // so only accept a real http(s) origin.
            if (usable(frameOrigin)) origin = frameOrigin;
        } catch {
            // cross-origin access to location throws in some states; the
            // src attribute is still readable — fall back to parsing it
            try {
                const src = (
                    iframeWindow as Window & { frameElement?: HTMLIFrameElement }
                ).frameElement?.getAttribute('src');
                const srcOrigin = src ? new URL(src, window.location.origin).origin : '';
                if (usable(srcOrigin)) origin = srcOrigin;
            } catch {
                /* keep host-origin default */
            }
        }
    }
    // The bridge is created while the iframe still shows the about:blank
    // placeholder, so a one-time origin snapshot usually resolves to the HOST
    // origin — and every push into a cross-origin module frame is then
    // silently dropped by the browser. Resolve the target origin PER SEND
    // from the iframe's current state instead; fall back to the snapshot
    // while the frame is between navigations.
    const targetOrigin = (): string => {
        if (options.origin) return options.origin;
        if (currentIframeWindow) {
            try {
                const o = new URL(currentIframeWindow.location.href).origin;
                if (/^https?:\/\//.test(o)) return o;
            } catch {
                /* cross-origin frame: location href is not readable */
            }
            try {
                const src = (
                    currentIframeWindow as Window & { frameElement?: HTMLIFrameElement }
                ).frameElement?.getAttribute('src');
                const o = src ? new URL(src, window.location.origin).origin : '';
                if (/^https?:\/\//.test(o)) return o;
            } catch {
                /* keep snapshot */
            }
        }
        return origin;
    };
    const disposers: Array<() => void> = [];
    let currentIframeWindow = iframeWindow;

    const pendingPings = new Set<(msgId: string) => void>();
    let sdkAlive = false;
    let pingSeq = 0;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;

    /**
     * Heartbeat (PRD 7.2): host pings; module SDK auto-answers. Enforcement
     * (crash) only after first pong, so non-SDK modules never false-crash.
     */
    function startHeartbeat(
        onLivenessChange: (alive: boolean) => void,
        intervalMs = 30000,
        timeoutMs = 10000
    ): void {
        stopHeartbeat();
        heartbeatTimer = setInterval(() => {
            if (!currentIframeWindow) return;
            const id = `ping-${tabId}-${++pingSeq}`;
            let answered = false;
            const resolver = (msgId: string) => {
                if (msgId === id) {
                    answered = true;
                }
            };
            pendingPings.add(resolver);
            currentIframeWindow.postMessage(
                { type: 'request', id, method: 'ping', payload: null },
                targetOrigin()
            );
            setTimeout(() => {
                // Drop the dead closure so long-lived tabs don't leak (review #8).
                pendingPings.delete(resolver);
                if (answered) {
                    onLivenessChange(true);
                } else if (sdkAlive) {
                    // Enforcement only once the module has answered before;
                    // non-SDK modules stay silent forever and never false-crash.
                    console.warn('[host-bridge] heartbeat missed for', tabId);
                    onLivenessChange(false);
                }
            }, timeoutMs);
        }, intervalMs);
    }

    function stopHeartbeat(): void {
        if (heartbeatTimer !== null) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
    }

    async function handleRequest(msg: ModuleRequest): Promise<unknown> {
        switch (msg.method) {
            case 'http': {
                const req = msg.payload as {
                    path: string;
                    method?: string;
                    headers?: Record<string, string>;
                    body?: unknown;
                };
                try {
                    const { status, data } = await options.proxyHttp({
                        path: req.path,
                        method: req.method ?? 'GET',
                        headers: req.headers,
                        body: req.body,
                    });
                    if (status >= 400) {
                        const envelope = data as { code?: string; message?: string } | null;
                        return {
                            __httpError: true,
                            status,
                            code: envelope?.code ?? 'http_error',
                            message: envelope?.message ?? `status ${status}`,
                        };
                    }
                    return { status, ok: status < 400, data };
                } catch (err) {
                    console.error('[host-bridge] http proxy failed:', err);
                    throw new HttpError(502, 'gateway_unreachable', 'gateway request failed');
                }
            }
            case 'toast':
                options.onToast(
                    msg.payload as { level: string; message: string; durationMs?: number }
                );
                return { ok: true };
            case 'tab':
                options.onActiveTask(tabId, (msg.payload as { name: string } | null) ?? null);
                return { ok: true };
            case 'theme': {
                const mode: ThemeMode = document.documentElement.classList.contains('light')
                    ? 'light'
                    : 'dark';
                return mode;
            }
            case 'devices': {
                const mobile = window.matchMedia('(max-width: 767px)').matches;
                const info: DeviceInfo = { viewport: mobile ? 'mobile' : 'desktop', mode: null };
                return info;
            }
            default:
                throw new Error(`unknown bridge method: ${(msg as ModuleRequest).method}`);
        }
    }

    function onMessage(ev: MessageEvent): void {
        if (ev.source !== currentIframeWindow || ev.origin !== origin) return;
        const raw = ev.data as { type?: string; id?: string };
        if (raw.type === 'response') {
            // Module answering a host-initiated request (heartbeat pong).
            sdkAlive = true;
            if (typeof raw.id === 'string') pendingPings.forEach((fn) => fn(raw.id as string));
            return;
        }
        const msg = ev.data as ModuleRequest;
        if (!raw || raw.type !== 'request') return;
        if (typeof raw.id !== 'string' || typeof msg.method !== 'string') return;
        void handleRequest(msg).then(
            (result) => {
                if (isHttpErrorResult(result)) {
                    const e = result as { status: number; code: string; message: string };
                    respond({
                        type: 'response',
                        id: msg.id,
                        ok: false,
                        error: { code: e.code, message: e.message },
                    });
                } else {
                    respond({ type: 'response', id: msg.id, ok: true, result });
                }
            },
            (err: Error) => {
                respond({
                    type: 'response',
                    id: msg.id,
                    ok: false,
                    error: { code: err.name, message: err.message },
                });
            }
        );
    }

    function respond(msg: HostResponse): void {
        if (!currentIframeWindow) return;
        currentIframeWindow.postMessage(msg, targetOrigin());
    }

    function notify(event: HostEvent): void {
        if (!currentIframeWindow) return;
        currentIframeWindow.postMessage(event, targetOrigin());
    }

    window.addEventListener('message', onMessage);
    disposers.push(() => window.removeEventListener('message', onMessage));

    return {
        get iframeWindow() {
            return currentIframeWindow;
        },
        set iframeWindow(w: Window | null) {
            currentIframeWindow = w;
        },
        destroy() {
            disposers.forEach((d) => d());
            stopHeartbeat();
            currentIframeWindow = null;
        },
        notifyTheme(mode: ThemeMode) {
            notify({ type: 'event', event: 'theme', payload: mode });
        },
        notifyVisibility(visible: boolean) {
            notify({ type: 'event', event: 'visibility', payload: visible });
        },
        notifyLang(lang: string) {
            notify({ type: 'event', event: 'lang', payload: lang });
        },
        get sdkAlive() {
            return sdkAlive;
        },
        startHeartbeat,
        stopHeartbeat,
    };
}
