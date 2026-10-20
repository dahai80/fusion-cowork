import { afterAll, beforeEach, describe, expect, it } from 'bun:test';
import { runtimeApiKey, sdk } from '../src/sdk';

// issue #127: minimal regression coverage for the #96 runtime-key seam +
// vendored #20 content-type fix (mirror of web-shell standalone.test.ts).
// Bun test has no window, so the sdk import always takes the standalone path.
const calls: Array<{ input: string; init?: RequestInit }> = [];
const originalFetch = globalThis.fetch;

beforeEach(() => {
    calls.length = 0;
    globalThis.fetch = (async (input: string, init?: RequestInit) => {
        calls.push({ input, init });
        return new Response(JSON.stringify({ ok: 1 }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    }) as typeof fetch;
});

afterAll(() => {
    globalThis.fetch = originalFetch;
});

describe('standalone http contract (issue #96/#127)', () => {
    it('defaults content-type to application/json on JSON bodies (#20)', async () => {
        await sdk.http.post('/api/workflows', { name: 'w1' });
        const headers = calls[0]!.init?.headers as Record<string, string>;
        expect(headers['content-type']).toBe('application/json');
        expect(calls[0]!.init?.body).toBe(JSON.stringify({ name: 'w1' }));
    });

    it('sends runtime key as x-api-key (window global, #96)', async () => {
        (globalThis as { window?: unknown }).window = { __FUSION_COWORK_WEB_API_KEY__: 'k-test' };
        try {
            await sdk.http.post('/api/x', {});
            const headers = calls[0]!.init?.headers as Record<string, string>;
            expect(headers['x-api-key']).toBe('k-test');
            expect(headers['content-type']).toBe('application/json');
        } finally {
            delete (globalThis as { window?: unknown }).window;
        }
    });

    it('omits x-api-key and content-type when keyless GET', async () => {
        await sdk.http.get('/api/models');
        const headers = (calls[0]!.init?.headers ?? {}) as Record<string, string>;
        expect(headers['x-api-key']).toBeUndefined();
        expect(headers['content-type']).toBeUndefined();
    });

    it('runtimeApiKey() prefers window global over storage', () => {
        (globalThis as { window?: unknown }).window = { __FUSION_COWORK_WEB_API_KEY__: 'k-win' };
        try {
            expect(runtimeApiKey()).toBe('k-win');
        } finally {
            delete (globalThis as { window?: unknown }).window;
        }
    });
});
