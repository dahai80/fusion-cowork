import { describe, expect, it } from 'bun:test';
import { breadcrumbSegments, dirBasename, selectableSystemDirs } from '../src/systemdirs';

// issue #251: new working-directory candidates = server system directories − names already taken; empty names dropped; hand-typed paths rejected;
// dialog drill-down level by level — breadcrumb splitting + current directory name from the last relative-path segment

describe('selectableSystemDirs (#251)', () => {
    it('filters used names and blank entries', () => {
        const dirs = [
            { name: 'alpha', path: '/w/alpha', relative: 'alpha' },
            { name: 'beta', path: '/w/beta', relative: 'beta' },
            { name: '', path: '/w/blank', relative: '' },
        ];
        const out = selectableSystemDirs(dirs, new Set(['beta']));
        expect(out.map((d) => d.name)).toEqual(['alpha']);
    });

    it('returns empty for undefined catalog', () => {
        expect(selectableSystemDirs(undefined, new Set())).toEqual([]);
    });

    it('keeps everything when nothing is used', () => {
        const dirs = [{ name: 'Q3 对账', path: '/w/q3', relative: 'Q3 对账' }]; // i18n: user-facing
        expect(selectableSystemDirs(dirs, new Set())).toEqual(dirs);
    });
});

describe('breadcrumbSegments + dirBasename (#251 modal)', () => {
    it('splits the relative path into clickable segments', () => {
        expect(breadcrumbSegments('')).toEqual([]);
        expect(breadcrumbSegments('proj')).toEqual(['proj']);
        expect(breadcrumbSegments('proj/src/deep')).toEqual(['proj', 'src', 'deep']);
    });

    it('derives the space name from the last segment with root fallback', () => {
        expect(dirBasename('proj/src/deep', 'fusion')).toBe('deep');
        expect(dirBasename('proj', 'fusion')).toBe('proj');
        expect(dirBasename('', 'fusion')).toBe('fusion');
    });
});
