import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
    cssVarName,
    fontStacks,
    palette,
    preset,
    radius,
    shadow,
    spacing,
    typography,
    zIndex,
} from '../src';

const SEMANTIC_KEYS = [
    'bg',
    'bgElevated',
    'bgMuted',
    'border',
    'textPrimary',
    'textSecondary',
    'textMuted',
    'success',
    'running',
    'warning',
    'error',
    'offline',
    'brand',
    'bgSidebar',
    'brandHover',
] as const;

describe('@fusion/design-tokens palette', () => {
    it('covers all semantic keys in dark and light', () => {
        for (const key of SEMANTIC_KEYS) {
            expect(palette.dark[key]).toMatch(/^#[0-9a-f]{6}$/i);
            expect(palette.light[key]).toMatch(/^#[0-9a-f]{6}$/i);
        }
    });
    it('dark and light palettes differ (at least bg)', () => {
        expect(palette.dark.bg).not.toBe(palette.light.bg);
    });
    it('cssVarName maps camelCase to --fs-kebab', () => {
        expect(cssVarName('bgElevated')).toBe('--fs-bg-elevated');
        expect(cssVarName('brand')).toBe('--fs-brand');
    });
});

describe('@fusion/design-tokens primitives', () => {
    it('spacing steps are 4px multiples', () => {
        for (const value of Object.values(spacing)) {
            const px = Number.parseFloat(value) * 16;
            expect(px % 4).toBe(0);
        }
    });
    it('radius and typography expose the documented scale', () => {
        expect(radius.card).toBe('0.5rem');
        expect(typography.fontSize.base).toBe('1rem');
        expect(typography.fontFamily.sans.length).toBeGreaterThan(0);
    });
});

describe('@fusion/design-tokens css + preset', () => {
    const css = readFileSync(join(import.meta.dir, '..', 'src', 'tokens.css'), 'utf8');
    it('tokens.css declares every palette var in both modes', () => {
        for (const key of SEMANTIC_KEYS) {
            const varName = cssVarName(key);
            expect(css).toContain(`${varName}: ${palette.dark[key]}`);
            expect(css).toContain(`${varName}: ${palette.light[key]}`);
        }
        expect(css).toContain(':root');
        expect(css).toContain('html.light');
    });
    it('preset maps semantic colors to --fs-* vars', () => {
        const colors = preset.theme?.extend?.colors as Record<string, string> | undefined;
        expect(colors?.bg).toBe('var(--fs-bg)');
        expect(colors?.brand).toBe('var(--fs-brand)');
    });
});

describe('@fusion/design-tokens interaction layer (issue #56)', () => {
    const css = readFileSync(join(import.meta.dir, '..', 'src', 'tokens.css'), 'utf8');
    it('z-index ladder is strictly increasing and declared in tokens.css', () => {
        const steps = ['base', 'dropdown', 'overlay', 'toast'] as const;
        for (let i = 1; i < steps.length; i++) {
            expect(Number(zIndex[steps[i]!])).toBeGreaterThan(Number(zIndex[steps[i - 1]!]));
        }
        for (const [key, value] of Object.entries(zIndex)) {
            expect(css).toContain(`--fs-z-${key}: ${value}`);
        }
    });
    it('elevation shadows: elev1 for dropdowns, elev2 for modals', () => {
        expect(shadow.elev1).toBe('0 8px 24px rgb(0 0 0 / 0.35)');
        expect(shadow.elev2).toBe('0 16px 48px rgb(0 0 0 / 0.45)');
    });
    it('preset maps z-index and shadows to tokens', () => {
        const z = preset.theme?.extend?.zIndex as Record<string, string> | undefined;
        expect(z?.base).toBe('var(--fs-z-base)');
        expect(z?.dropdown).toBe('var(--fs-z-dropdown)');
        expect(z?.overlay).toBe('var(--fs-z-overlay)');
        expect(z?.toast).toBe('var(--fs-z-toast)');
        const boxShadow = preset.theme?.extend?.boxShadow as Record<string, string> | undefined;
        expect(boxShadow?.overlay).toBe(shadow.elev1);
        expect(boxShadow?.modal).toBe(shadow.elev2);
    });
    it('radius vars declared in tokens.css for button-level pinning', () => {
        for (const [key, value] of Object.entries(radius)) {
            expect(css).toContain(`--fs-radius-${key}: ${value}`);
        }
    });
});

describe('@fusion/design-tokens layout tokens (issue #85)', () => {
    const css = readFileSync(join(import.meta.dir, '..', 'src', 'tokens.css'), 'utf8');
    it('font stacks pair with --fs-font-* in both :root and html.light', () => {
        for (const [key, stack] of Object.entries(fontStacks)) {
            const darkBlock = css.slice(css.indexOf(':root'), css.indexOf('html.light {'));
            const lightBlock = css.slice(css.indexOf('html.light {'));
            expect(darkBlock).toContain(`--fs-font-${key}: ${stack}`);
            expect(lightBlock).toContain(`--fs-font-${key}: ${stack}`);
        }
    });
    it('bg-sidebar and brand-hover differ per theme and pair with tokens.css', () => {
        expect(palette.dark.bgSidebar).not.toBe(palette.light.bgSidebar);
        expect(palette.dark.brandHover).not.toBe(palette.light.brandHover);
        expect(css).toContain(`--fs-bg-sidebar: ${palette.dark.bgSidebar}`);
        expect(css).toContain(`--fs-bg-sidebar: ${palette.light.bgSidebar}`);
        expect(css).toContain(`--fs-brand-hover: ${palette.dark.brandHover}`);
        expect(css).toContain(`--fs-brand-hover: ${palette.light.brandHover}`);
    });
    it('radius.input declared in both themes', () => {
        expect(radius.input).toBe('1rem');
        expect(css.match(/--fs-radius-input: 1rem/g)?.length).toBe(2);
    });
});
