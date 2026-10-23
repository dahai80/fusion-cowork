import { afterEach, describe, expect, it } from 'bun:test';
import { applyTheme, initialTheme } from '../src/theme';

// issue #176: shell theme following — html.light/html.dark toggling and the
// initial-mode resolution chain (localStorage["fusion.shell.theme"] >
// prefers-color-scheme > dark). Bun test has no DOM, so stub the globals.
type ToggleCall = { name: string; force?: boolean };

interface ToggleRecorder {
    calls: ToggleCall[];
}

function stubDocument(): ToggleRecorder {
    const rec: ToggleRecorder = { calls: [] };
    (globalThis as { document?: unknown }).document = {
        documentElement: {
            classList: {
                toggle: (name: string, force?: boolean) => {
                    rec.calls.push({ name, force });
                },
            },
        },
    };
    return rec;
}

function stubStorage(get: (key: string) => string | null): { set: string[] } {
    const state = { set: [] as string[] };
    (globalThis as { localStorage?: unknown }).localStorage = {
        getItem: (key: string) => get(key),
        setItem: (key: string, value: string) => {
            if (key === 'fusion.shell.theme') state.set.push(value);
        },
    };
    return state;
}

const g = globalThis as Record<string, unknown>;
const originals = {
    document: (globalThis as { document?: unknown }).document,
    localStorage: Object.getOwnPropertyDescriptor(globalThis, 'localStorage'),
    window: (globalThis as { window?: unknown }).window,
};

afterEach(() => {
    if (originals.localStorage) {
        Object.defineProperty(globalThis, 'localStorage', originals.localStorage);
    } else {
        delete g.localStorage;
    }
    if (originals.document !== undefined) {
        g.document = originals.document;
    } else {
        delete g.document;
    }
    if (originals.window !== undefined) {
        g.window = originals.window;
    } else {
        delete g.window;
    }
});

describe('applyTheme (issue #176)', () => {
    it('toggles html.light on / html.dark off for light mode', () => {
        const rec = stubDocument();
        applyTheme('light');
        expect(rec.calls).toEqual([
            { name: 'light', force: true },
            { name: 'dark', force: false },
        ]);
    });

    it('toggles html.dark on / html.light off for dark mode', () => {
        const rec = stubDocument();
        applyTheme('dark');
        expect(rec.calls).toEqual([
            { name: 'light', force: false },
            { name: 'dark', force: true },
        ]);
    });
});

describe('initialTheme (issue #176)', () => {
    it('reads stored shell preference first', () => {
        stubStorage(() => 'light');
        expect(initialTheme()).toBe('light');
    });

    it('falls through on garbage stored value', () => {
        stubStorage(() => 'blue');
        expect(initialTheme()).toBe('dark');
    });

    it('honors prefers-color-scheme when nothing stored', () => {
        stubStorage(() => null);
        g.window = { matchMedia: () => ({ matches: true }) };
        expect(initialTheme()).toBe('light');
    });

    it('defaults to dark when storage throws and no matchMedia', () => {
        (globalThis as { localStorage?: unknown }).localStorage = {
            getItem: () => {
                throw new Error('private mode');
            },
        };
        expect(initialTheme()).toBe('dark');
    });
});
