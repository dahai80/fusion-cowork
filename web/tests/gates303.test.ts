import { describe, expect, it } from 'bun:test';
import { computeWaves, defNodes } from '../src/runview';
import { gateProgress } from '../src/components/GateProgress';
import type { RunStep } from '../src/api';

// issue #303: gate progress + blocked→terminal step merge helpers.

const defDict = {
    nodes: {
        n1: { id: 'n1', name: 'github_api' },
        n2: { id: 'n2', name: 'approval_gate', config: { label: 'G1 提案闸门' } },
        n3: { id: 'n3', name: 'http_request' },
    },
    edges: [],
};

const defList = {
    nodes: [
        { id: 'n1', name: 'github_api' },
        { id: 'n2', name: 'approval_gate', config: { label: 'G1' } },
    ],
    edges: [],
};

function step(nodeId: string, status: string): RunStep {
    return {
        node_id: nodeId,
        node_name: 'x',
        node_display_name: nodeId,
        status,
        execution_time: 0,
        summary: '',
        error: null,
        output_data: {},
    };
}

describe('defNodes (issue #303 shape normalization)', () => {
    it('handles dict-shaped nodes (Workflow.to_dict)', () => {
        expect(defNodes(defDict).map((n) => n.id)).toEqual(['n1', 'n2', 'n3']);
    });

    it('handles list-shaped nodes (template JSON)', () => {
        expect(defNodes(defList).map((n) => n.id)).toEqual(['n1', 'n2']);
    });

    it('returns empty for missing/invalid nodes', () => {
        expect(defNodes(undefined)).toEqual([]);
        expect(defNodes({ nodes: 'x' })).toEqual([]);
    });

    it('computeWaves survives dict-shaped nodes', () => {
        const waves = computeWaves(defDict as never);
        expect(waves[0].nodes.map((n) => n.id)).toEqual(['n1', 'n2', 'n3']);
    });
});

describe('gateProgress (issue #303)', () => {
    it('derives done/waiting/pending states from steps', () => {
        const steps = [step('n2', 'blocked'), step('n2', 'success')];
        const gates = gateProgress(defDict as never, steps);
        expect(gates).toEqual([{ id: 'n2', label: 'G1 提案闸门', state: 'done' }]);
    });

    it('blocked-only node counts as waiting', () => {
        const gates = gateProgress(defDict as never, [step('n2', 'blocked')]);
        expect(gates[0].state).toBe('waiting');
    });

    it('failed terminal counts as failed', () => {
        const gates = gateProgress(defDict as never, [step('n2', 'blocked'), step('n2', 'denied')]);
        expect(gates[0].state).toBe('failed');
    });

    it('no steps counts as pending and falls back to G<n> label', () => {
        const gates = gateProgress(
            { nodes: [{ id: 'n9', name: 'approval_gate' }] } as never,
            []
        );
        expect(gates).toEqual([{ id: 'n9', label: 'G1', state: 'pending' }]);
    });

    it('ignores non-gate nodes', () => {
        expect(gateProgress(defDict as never, [])).toHaveLength(1);
    });
});
