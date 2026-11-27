import { describe, expect, test } from 'bun:test';
import manifest from '../manifest.json';

// issue #596: demand operator GUI — page registration in the shell manifest
// (route wired so the unified web shell can deep-link #/demand).

describe('demand page registration', () => {
    test('manifest declares /demand page', () => {
        const page = manifest.pages.find((p) => p.route === '/demand');
        expect(page).toBeDefined();
        expect(page?.title).toBe('需求引擎'); // i18n: user-facing
        expect(page?.title_en).toBe('Demand');
    });
});
