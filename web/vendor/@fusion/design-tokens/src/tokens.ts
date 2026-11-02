/**
 * @fusion/design-tokens — single source for fusion web UI look (G5/C7).
 * Modules consume these via the vendor sync; module-private theme values
 * are deprecated. Dark is default, html.light switches the palette.
 */
export interface Palette {
    bg: string;
    bgElevated: string;
    bgMuted: string;
    border: string;
    textPrimary: string;
    textSecondary: string;
    textMuted: string;
    success: string;
    running: string;
    warning: string;
    error: string;
    offline: string;
    brand: string;
    bgSidebar: string;
    brandHover: string;
}

export const palette: { dark: Palette; light: Palette } = {
    dark: {
        bg: '#0b0e14',
        bgElevated: '#131722',
        bgMuted: '#1a2030',
        border: '#2a3244',
        textPrimary: '#e6e9f0',
        textSecondary: '#a8b0c2',
        textMuted: '#6b7488',
        success: '#34c778',
        running: '#3b82f6',
        warning: '#eab308',
        error: '#ef4444',
        offline: '#6b7488',
        brand: '#4f7cff',
        bgSidebar: '#131722',
        brandHover: '#6b93ff',
    },
    light: {
        bg: '#f5f6f8',
        bgElevated: '#ffffff',
        bgMuted: '#eceef2',
        border: '#d4d9e2',
        textPrimary: '#1a2030',
        textSecondary: '#4a5468',
        textMuted: '#8a92a4',
        success: '#16a34a',
        running: '#2563eb',
        warning: '#ca8a04',
        error: '#dc2626',
        offline: '#8a92a4',
        brand: '#2f5fe8',
        bgSidebar: '#eceef2',
        brandHover: '#264ec4',
    },
};

/** camelCase token key → CSS var (--fs-kebab-case). */
export function cssVarName(key: keyof Palette): string {
    return `--fs-${key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
}

/** 4px-base spacing scale (rem), steps 0..16. */
export const spacing: Record<string, string> = Object.fromEntries(
    Array.from({ length: 17 }, (_, i) => [String(i), `${(i * 4) / 16}rem`])
);

export const radius = {
    sm: '0.25rem',
    md: '0.5rem',
    card: '0.5rem',
    lg: '0.75rem',
    input: '1rem',
    full: '9999px',
} as const;

/** System font stacks (issue #85, D2 — no webfont bundling). Values mirror
 *  the --fs-font-serif / --fs-font-sans tokens in tokens.css exactly. */
export const fontStacks = {
    serif: 'ui-serif, Georgia, "Songti SC", "Noto Serif SC", serif',
    sans: 'Inter, ui-sans-serif, system-ui, "PingFang SC", "Microsoft YaHei", sans-serif',
} as const;

export const typography = {
    fontFamily: {
        sans: [
            'ui-sans-serif',
            'system-ui',
            '-apple-system',
            'PingFang SC',
            'Microsoft YaHei',
            'sans-serif',
        ] as string[],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'] as string[],
    },
    fontSize: {
        xs: '0.75rem',
        sm: '0.875rem',
        base: '1rem',
        lg: '1.125rem',
        xl: '1.25rem',
    },
} as const;

/** z-index ladder (issue #56): base < dropdown < overlay < toast — replaces
 *  ad-hoc z-50/z-[60]; toast must render above every dropdown and modal. */
export const zIndex = {
    base: '0',
    dropdown: '50',
    overlay: '60',
    toast: '70',
} as const;

/** Elevation tiers (issue #56): elev1 = dropdown/popover, elev2 = modal.
 *  Flat inline panels stay border-only (elev-0, no class needed). */
export const shadow = {
    elev1: '0 8px 24px rgb(0 0 0 / 0.35)',
    elev2: '0 16px 48px rgb(0 0 0 / 0.45)',
} as const;
