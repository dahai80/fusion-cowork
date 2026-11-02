import type { Config } from 'tailwindcss';
import { cssVarName, palette, radius, shadow, typography, zIndex } from './tokens';

/**
 * Tailwind preset generated from the token source (G5): every fusion web UI
 * imports this preset so color/border/typography stay single-sourced.
 */
export const fusionPreset: Partial<Config> = {
    darkMode: ['class', '.light'],
    theme: {
        extend: {
            colors: Object.fromEntries(
                Object.keys(palette.dark).map((key) => [
                    // Semantic tailwind name: camelCase → kebab (bgElevated → bg-elevated).
                    key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`),
                    `var(${cssVarName(key as keyof typeof palette.dark)})`,
                ])
            ),
            borderRadius: {
                sm: radius.sm,
                DEFAULT: radius.md,
                md: radius.md,
                card: radius.card,
                lg: radius.lg,
                input: radius.input,
                full: radius.full,
            },
            fontFamily: {
                sans: typography.fontFamily.sans,
                serif: 'var(--fs-font-serif)',
                mono: typography.fontFamily.mono,
            },
            fontSize: {
                xs: typography.fontSize.xs,
                sm: typography.fontSize.sm,
                base: typography.fontSize.base,
                lg: typography.fontSize.lg,
                xl: typography.fontSize.xl,
            },
            boxShadow: {
                overlay: shadow.elev1,
                modal: shadow.elev2,
            },
            // z-index ladder (issue #56): consumers pick z-dropdown/z-overlay/
            // z-toast — never ad-hoc z-50/z-[60] values.
            zIndex: Object.fromEntries(
                Object.keys(zIndex).map((key) => [key, `var(--fs-z-${key})`])
            ),
        },
    },
};

export default fusionPreset;
