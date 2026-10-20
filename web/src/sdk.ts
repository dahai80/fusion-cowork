import { type FusionShellSdk, HttpError, type HttpResult, createShellSdk } from '@fusion/web-sdk';

// Single SDK instance for the module. Embedded (inside union-web shell
// iframe): http goes through the host postMessage bridge — the host injects
// the module api_key, the UI holds no keys (spec S8: no auth logic in UI).
// Standalone (own Vite server / web-api static host): same-origin direct
// fetch with a runtime key seam (issue #96) — build-time VITE_API_KEY is
// unreachable in a keyless release build, so the key resolves at runtime:
// window.__FUSION_COWORK_WEB_API_KEY__ > localStorage
// ('fusion-cowork-web-api-key') > VITE_API_KEY (legacy fallback). No key →
// headers omitted (keyless serve posture, default 127.0.0.1 bind).
declare global {
    interface Window {
        __FUSION_COWORK_WEB_API_KEY__?: string;
    }
}

export const isEmbedded = (): boolean => typeof window !== 'undefined' && window.parent !== window;

// ?embedded=1 is added by the shell when loading the module; standalone
// keeps the module's own bottom nav. When embedded the shell owns chrome,
// so the module hides its own navigation.
export const embeddedParam = (): boolean => {
    try {
        return new URLSearchParams(window.location.search).get('embedded') === '1';
    } catch {
        return false;
    }
};

const RUNTIME_KEY_STORAGE = 'fusion-cowork-web-api-key';

export function runtimeApiKey(): string {
    if (typeof window !== 'undefined' && window.__FUSION_COWORK_WEB_API_KEY__) {
        return window.__FUSION_COWORK_WEB_API_KEY__;
    }
    try {
        return localStorage.getItem(RUNTIME_KEY_STORAGE) ?? '';
    } catch {
        return '';
    }
}

async function directRequest<T>(
    path: string,
    method: 'GET' | 'POST' | 'PUT' | 'DELETE',
    body?: unknown
): Promise<HttpResult<T>> {
    const headers: Record<string, string> = {};
    // Vendored web-sdk standalone request() omits content-type (upstream
    // fusion-web-shell#22) — FastAPI then 422s every POST/PUT. Set it here
    // whenever a body is present so keyless builds work too.
    if (body !== undefined) {
        headers['content-type'] = 'application/json';
    }
    const key = runtimeApiKey();
    if (key) {
        headers['x-api-key'] = key;
    }
    const res = await fetch(apiBase() + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as T;
    if (!res.ok) {
        throw new HttpError(res.status, 'http_error', `request failed: ${res.status}`);
    }
    return { status: res.status, ok: true, data };
}

function directHttp() {
    return {
        request: <T>(path: string) => directRequest<T>(path, 'GET'),
        get: <T>(path: string) => directRequest<T>(path, 'GET'),
        post: <T>(path: string, body?: unknown) => directRequest<T>(path, 'POST', body),
        put: <T>(path: string, body?: unknown) => directRequest<T>(path, 'PUT', body),
        delete: <T>(path: string) => directRequest<T>(path, 'DELETE'),
    };
}

// W13/B2: SSE over fetch (EventSource cannot send the runtime api key header).
// Resolves when the stream closes; throws on non-2xx / network error so the
// caller can fall back to polling.
export async function streamSse(
    path: string,
    onEvent: (event: string, data: unknown) => void,
    signal: AbortSignal
): Promise<void> {
    if (isEmbedded()) throw new Error('sse-unavailable-embedded');
    const headers: Record<string, string> = { accept: 'text/event-stream' };
    const key = runtimeApiKey();
    if (key) headers['x-api-key'] = key;
    const res = await fetch(apiBase() + path, { headers, signal });
    if (!res.ok || !res.body) throw new Error(`sse failed: ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx = buf.indexOf('\n\n');
        while (idx >= 0) {
            const chunk = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            let event = 'message';
            const dataLines: string[] = [];
            for (const line of chunk.split('\n')) {
                if (line.startsWith('event:')) event = line.slice(6).trim();
                else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
            }
            if (dataLines.length) {
                const raw = dataLines.join('\n');
                let parsed: unknown = raw;
                try {
                    parsed = JSON.parse(raw);
                } catch {
                    // keep raw text
                }
                onEvent(event, parsed);
            }
            idx = buf.indexOf('\n\n');
        }
    }
}

export const sdk: FusionShellSdk = createShellSdk();

// issue #129: under the unified shell the module is served at
// /shell/<id>/ — origin-root API paths 404 (gateway has no generic /api/*
// routes). Derive the prefix from the document pathname like agent-studio's
// apiBase(): "/shell/fusion-cowork/" → "/shell/fusion-cowork", standalone
// root → "". One build serves both topologies.
export function apiBase(): string {
    if (typeof window === 'undefined') return '';
    const path = window.location?.pathname ?? '';
    const m = path.match(/^(\/shell\/[^/]+)/);
    return m ? m[1] : '';
}

if (!isEmbedded()) {
    // Standalone against the web-api: layer runtime key + content-type onto
    // the plain same-origin http. Embedded mode never enters this branch.
    // apiBase() is applied per-request (shell-proxied standalone deployments).
    (sdk as unknown as { http: ReturnType<typeof directHttp> }).http = directHttp();
} else {
    // Embedded through the host postMessage bridge: the host fetches the
    // path as-is, so the /shell/<id> module prefix must ride on the path.
    const bridgeHttp = sdk.http;
    (sdk as unknown as { http: unknown }).http = {
        request: <T>(path: string) => bridgeHttp.request<T>(apiBase() + path),
        get: <T>(path: string) => bridgeHttp.get<T>(apiBase() + path),
        post: <T>(path: string, body?: unknown) => bridgeHttp.post<T>(apiBase() + path, body),
        put: <T>(path: string, body?: unknown) => bridgeHttp.put<T>(apiBase() + path, body),
        delete: <T>(path: string) => bridgeHttp.delete<T>(apiBase() + path),
    };
}
