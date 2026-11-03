// issue #261: 画布助手纯函数 — schema 字段推导 / 输入形态 / run 状态着色
import { describe, expect, it } from 'bun:test';
import { fieldKind, isTrueish, runStatusByNode, schemaFields, toneColor, toneFor } from '../src/canvas';

describe('schemaFields', () => {
    it('maps properties with required first-marker', () => {
        const fields = schemaFields({
            properties: { command: { type: 'string', description: 'cmd' }, timeout: { type: 'number' } },
            required: ['command'],
        });
        expect(fields.length).toBe(2);
        expect(fields[0].key).toBe('command');
        expect(fields[0].required).toBe(true);
        expect(fields[1].required).toBe(false);
    });

    it('returns empty for null/missing properties', () => {
        expect(schemaFields(null)).toEqual([]);
        expect(schemaFields({})).toEqual([]);
        expect(schemaFields({ type: 'object' })).toEqual([]);
    });
});

describe('fieldKind', () => {
    it('select wins over type', () => {
        expect(fieldKind({ enum: ['a', 'b'] })).toBe('select');
    });
    it('boolean/number/array map to their inputs', () => {
        expect(fieldKind({ type: 'boolean' })).toBe('checkbox');
        expect(fieldKind({ type: 'integer' })).toBe('number');
        expect(fieldKind({ type: 'number' })).toBe('number');
        expect(fieldKind({ type: 'array' })).toBe('textarea');
        expect(fieldKind({ type: 'object' })).toBe('textarea');
        expect(fieldKind({ type: 'string' })).toBe('text');
        expect(fieldKind({})).toBe('text');
    });
});

describe('isTrueish', () => {
    it('accepts true/"true"/"1", rejects false/""/undefined', () => {
        expect(isTrueish(true)).toBe(true);
        expect(isTrueish('true')).toBe(true);
        expect(isTrueish('1')).toBe(true);
        expect(isTrueish('false')).toBe(false);
        expect(isTrueish('')).toBe(false);
        expect(isTrueish(undefined)).toBe(false);
    });
});

describe('runStatusByNode', () => {
    it('keeps the last step per node (retries)', () => {
        expect(
            runStatusByNode([
                { node_id: 'n1', status: 'failed' },
                { node_id: 'n1', status: 'success' },
                { node_id: 'n2', status: 'blocked' },
            ])
        ).toEqual({ n1: 'success', n2: 'blocked' });
    });
    it('handles undefined/empty', () => {
        expect(runStatusByNode(undefined)).toEqual({});
        expect(runStatusByNode([])).toEqual({});
    });
});

describe('toneFor/toneColor', () => {
    it('maps statuses to tones with stable colors', () => {
        expect(toneFor('success')).toBe('passed');
        expect(toneFor('blocked')).toBe('blocked');
        expect(toneFor('denied')).toBe('denied');
        expect(toneFor('weird')).toBe('idle');
        expect(toneColor('running')).toBe('#3b82f6');
        expect(toneColor('failed')).toBe('#ef4444');
        expect(toneColor(undefined)).toBe('#6b7280');
    });
});
