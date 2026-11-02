import { describe, expect, it } from 'bun:test';
import { selectableSystemDirs } from '../src/systemdirs';

// issue #251: 新建工作目录候选 = 服务端系统目录 − 已占用同名; 空名剔除; 不存在手输路径

describe('selectableSystemDirs (#251)', () => {
    it('filters used names and blank entries', () => {
        const dirs = [
            { name: 'alpha', path: '/w/alpha' },
            { name: 'beta', path: '/w/beta' },
            { name: '', path: '/w/blank' },
        ];
        const out = selectableSystemDirs(dirs, new Set(['beta']));
        expect(out.map((d) => d.name)).toEqual(['alpha']);
    });

    it('returns empty for undefined catalog', () => {
        expect(selectableSystemDirs(undefined, new Set())).toEqual([]);
    });

    it('keeps everything when nothing is used', () => {
        const dirs = [{ name: 'Q3 对账', path: '/w/q3' }];
        expect(selectableSystemDirs(dirs, new Set())).toEqual(dirs);
    });
});
