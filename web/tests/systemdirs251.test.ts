import { describe, expect, it } from 'bun:test';
import { breadcrumbSegments, dirBasename, selectableSystemDirs } from '../src/systemdirs';

// issue #251: 新建工作目录候选 = 服务端系统目录 − 已占用同名; 空名剔除; 不存在手输路径;
// 弹窗逐级下钻 — 面包屑切分 + 当前目录名取相对路径末段

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
        const dirs = [{ name: 'Q3 对账', path: '/w/q3', relative: 'Q3 对账' }];
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
