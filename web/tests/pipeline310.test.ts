import { describe, expect, test } from 'bun:test';
import { PIPELINE_LOOPS, PIPELINE_STAGES, isPipelineRun, stageLinks, stageStateOf } from '../src/pipeline';

// issue #310: pipeline dashboard topology data + state binding helpers.

describe('pipeline topology', () => {
    test('10 stages with unique ids', () => {
        expect(PIPELINE_STAGES.length).toBe(10);
        expect(new Set(PIPELINE_STAGES.map((s) => s.id)).size).toBe(10);
    });
    test('5 rejection loops', () => {
        expect(PIPELINE_LOOPS.length).toBe(5);
        expect(PIPELINE_LOOPS).toContainEqual([3, 2]);
        expect(PIPELINE_LOOPS).toContainEqual([10, 9]);
    });
    test('gates bind to approval_gate node ids', () => {
        const gate3 = PIPELINE_STAGES[2];
        const gate5 = PIPELINE_STAGES[4];
        const gate10 = PIPELINE_STAGES[9];
        expect(gate3.nodeIds).toEqual(['n2']);
        expect(gate5.nodeIds).toEqual(['n4']);
        expect(gate10.nodeIds).toEqual(['n10']);
    });
});

describe('stageStateOf', () => {
    test('done when any bound node succeeded', () => {
        const stage = PIPELINE_STAGES[5]; // n5+n6
        expect(stageStateOf(stage, [{ node_id: 'n5', status: 'success' }])).toBe('done');
    });
    test('active on non-success steps', () => {
        const stage = PIPELINE_STAGES[6]; // n7
        expect(stageStateOf(stage, [{ node_id: 'n7', status: 'blocked' }])).toBe('active');
    });
    test('off when no steps', () => {
        expect(stageStateOf(PIPELINE_STAGES[9], [])).toBe('off');
    });
});

describe('stageLinks', () => {
    test('extracts pr_url from nested result', () => {
        expect(stageLinks({ result: { pr_url: 'https://github.com/o/r/pull/1' } })).toEqual([
            'https://github.com/o/r/pull/1',
        ]);
    });
    test('empty on null', () => {
        expect(stageLinks(null)).toEqual([]);
    });
});

// issue #318: pipeline runs discoverable — matcher drives nav picker + run-row links
describe('isPipelineRun', () => {
    test('matches bilingual template names', () => {
        expect(isPipelineRun('交付-产品生命周期 (A/B/C 三闸门)')).toBe(true);
        expect(isPipelineRun('product_lifecycle')).toBe(true);
        expect(isPipelineRun('governance_issue_lifecycle')).toBe(false);
        expect(isPipelineRun('')).toBe(false);
    });
});
