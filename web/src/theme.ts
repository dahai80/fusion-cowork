import { sdk } from './sdk';

// issue #176: follow the shell theme toggle inside the embedded iframe.
// Embedded: sdk.theme.subscribe fires on the shell's postMessage theme event —
// toggle html.light / html.dark (tokens.css: dark is default, html.light flips
// the --fs-* palette). The shell re-pushes theme on iframe load, so no flash.
// Standalone: initial mode from localStorage["fusion.shell.theme"] (written by
// the shell on same-origin topologies and by this module on every change), else
// prefers-color-scheme, else dark.
const STORAGE_KEY = 'fusion.shell.theme';

type ThemeMode = 'dark' | 'light';

export function applyTheme(mode: ThemeMode): void {
    if (typeof document === 'undefined') return;
    const root = document.documentElement;
    root.classList.toggle('light', mode === 'light');
    root.classList.toggle('dark', mode === 'dark');
}

export function initialTheme(): ThemeMode {
    try {
        const stored = localStorage.getItem(STORAGE_KEY);
        if (stored === 'light' || stored === 'dark') return stored;
    } catch {
        // storage unavailable (private mode) — fall through
    }
    if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches) {
        return 'light';
    }
    return 'dark';
}

export function initTheme(): () => void {
    applyTheme(initialTheme());
    const unsubscribe = sdk.theme.subscribe((mode) => {
        applyTheme(mode);
        try {
            localStorage.setItem(STORAGE_KEY, mode);
        } catch {
            // storage unavailable — theme still applies for this session
        }
    });
    return unsubscribe;
}
