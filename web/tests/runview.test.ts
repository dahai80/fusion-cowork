import { describe, expect, it } from 'bun:test';
import { capabilitiesInUse, computeWaves, stepCapability } from '../src/runview';

// issue #224: unified run view pure helpers — wave grouping (M4 parallel
// branches) + capability annotation (M10) + capabilities-in-use rail.

describe('computeWaves', () => {
    it('groups nodes by topological depth', () => {
        const waves = computeWaves({
            nodes: [
                { id: 'a', name: 'n_a' },
                { id: 'b', name: 'n_b' },
                { id: 'c', name: 'n_c' },
            ],
            edges: [
                { source_id: 'a', target_id: 'b' },
                { source_id: 'b', target_id: 'c' },
            ],
        });
        expect(waves.map((w) => w.depth)).toEqual([0, 1, 2]);
        expect(waves[0].nodes.map((n) => n.id)).toEqual(['a']);
        expect(waves[2].nodes.map((n) => n.id)).toEqual(['c']);
    });

    it('puts parallel branches in one wave', () => {
        const waves = computeWaves({
            nodes: [
                { id: 'start', name: 'n_s' },
                { id: 'left', name: 'n_l' },
                { id: 'right', name: 'n_r' },
                { id: 'join', name: 'n_j' },
            ],
            edges: [
                { source_id: 'start', target_id: 'left' },
                { source_id: 'start', target_id: 'right' },
                { source_id: 'left', target_id: 'join' },
                { source_id: 'right', target_id: 'join' },
            ],
        });
        expect(waves).toHaveLength(3);
        const parallel = waves[1];
        expect(parallel.nodes.map((n) => n.id).sort()).toEqual(['left', 'right']);
    });

    it('diamond DAG gives join the deepest depth', () => {
        const waves = computeWaves({
            nodes: [
                { id: 's', name: 'n' },
                { id: 'x', name: 'n' },
                { id: 'y', name: 'n' },
                { id: 'j', name: 'n' },
                { id: 'z', name: 'n' },
            ],
            edges: [
                { source_id: 's', target_id: 'x' },
                { source_id: 'x', target_id: 'y' },
                { source_id: 's', target_id: 'j' },
                { source_id: 'y', target_id: 'z' },
            ],
        });
        const joinDepth = waves.find((w) => w.nodes.some((n) => n.id === 'j'))?.depth;
        expect(joinDepth).toBe(1);
    });

    it('no edges = single sequential wave', () => {
        const waves = computeWaves({ nodes: [{ id: 'a', name: 'n' }, { id: 'b', name: 'n' }] });
        expect(waves).toEqual([{ depth: 0, nodes: [{ id: 'a', name: 'n', label: undefined }, { id: 'b', name: 'n', label: undefined }] }]);
    });

    it('empty def = no waves', () => {
        expect(computeWaves({})).toEqual([]);
    });

    it('ignores edges to unknown nodes', () => {
        const waves = computeWaves({
            nodes: [{ id: 'a', name: 'n' }],
            edges: [{ source_id: 'ghost', target_id: 'a' }],
        });
        expect(waves[0].depth).toBe(0);
    });

    it('carries node labels through', () => {
        const waves = computeWaves({ nodes: [{ id: 'a', name: 'shell_exec', config: { label: '清理临时文件' } }] });
        expect(waves[0].nodes[0].label).toBe('清理临时文件');
    });
});

describe('stepCapability', () => {
    it('annotates mcp_client as connector with tool detail', () => {
        const cap = stepCapability({ node_name: 'mcp_client', input_data: { tool: 'bank.get_balance' } });
        expect(cap).toEqual({ kind: 'connector', label: 'mcp_client', detail: 'bank.get_balance' });
    });

    it('annotates http_request with url detail', () => {
        const cap = stepCapability({ node_name: 'http_request', input_data: { params: { url: 'http://x/y' } } });
        expect(cap?.kind).toBe('connector');
        expect(cap?.detail).toBe('http://x/y');
    });

    it('annotates studio_run as subagent with graph id', () => {
        const cap = stepCapability({ node_name: 'studio_run', input_data: { graph_id: 'g_1' } });
        expect(cap).toEqual({ kind: 'subagent', label: 'studio_run', detail: 'g_1' });
    });

    it('annotates skill.* as skill', () => {
        const cap = stepCapability({ node_name: 'skill.contract-review' });
        expect(cap?.kind).toBe('skill');
    });

    it('annotates AI nodes', () => {
        const cap = stepCapability({ node_name: 'ai_summarize' });
        expect(cap?.kind).toBe('ai');
    });

    it('returns null for plain system nodes', () => {
        expect(stepCapability({ node_name: 'file_copy' })).toBeNull();
    });
});

describe('capabilitiesInUse', () => {
    it('dedupes and counts across steps in first-seen order', () => {
        const caps = capabilitiesInUse([
            { node_name: 'mcp_client', input_data: { tool: 'a' } },
            { node_name: 'file_copy' },
            { node_name: 'mcp_client', input_data: { tool: 'a' } },
            { node_name: 'mcp_client', input_data: { tool: 'b' } },
        ]);
        expect(caps).toEqual([
            { kind: 'connector', label: 'mcp_client', detail: 'a', count: 2 },
            { kind: 'connector', label: 'mcp_client', detail: 'b', count: 1 },
        ]);
    });

    it('empty steps = empty rail', () => {
        expect(capabilitiesInUse([])).toEqual([]);
    });
});
