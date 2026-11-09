import { describe, expect, it } from 'bun:test';
import {
    applyMatchOverrides,
    capabilitiesInUse,
    computeWaves,
    findArtifactRefs,
    matchVerdictView,
    parseParamsDraft,
    stepArtifactRefs,
    stepCapability,
} from '../src/runview';

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
        const waves = computeWaves({
            nodes: [
                { id: 'a', name: 'n' },
                { id: 'b', name: 'n' },
            ],
        });
        expect(waves).toEqual([
            {
                depth: 0,
                nodes: [
                    { id: 'a', name: 'n', label: undefined },
                    { id: 'b', name: 'n', label: undefined },
                ],
            },
        ]);
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

// issue #225: 审批卡 "编辑参数" 草稿解析
describe('parseParamsDraft', () => {
    it('accepts a JSON object', () => {
        expect(parseParamsDraft('{"amount": 10, "payee": "acme"}')).toEqual({
            ok: true,
            params: { amount: 10, payee: 'acme' },
        });
    });

    it('accepts an empty object', () => {
        expect(parseParamsDraft('{}')).toEqual({ ok: true, params: {} });
    });

    it('rejects invalid JSON', () => {
        expect(parseParamsDraft('{amount: 10}')).toEqual({ ok: false, error: 'invalid_json' });
    });

    it('rejects non-object JSON', () => {
        expect(parseParamsDraft('[1, 2]')).toEqual({ ok: false, error: 'not_object' });
        expect(parseParamsDraft('42')).toEqual({ ok: false, error: 'not_object' });
        expect(parseParamsDraft('null')).toEqual({ ok: false, error: 'not_object' });
    });
});

// issue #233: artifact 轻量引用提取 (引擎 ref_text 格式)
describe('findArtifactRefs', () => {
    it('extracts refs in order', () => {
        const text = 'x [Artifact: Q3 Report | ID: art_1 | Version: v2 | Type: report | Size: 4096] y';
        expect(findArtifactRefs(text)).toEqual([{ artifact_id: 'art_1', name: 'Q3 Report' }]);
    });

    it('dedupes and preserves order', () => {
        const text = '[Artifact: B | ID: b_1 | v1] [Artifact: A | ID: a_1 | v1] [Artifact: B2 | ID: b_1 | v2]';
        expect(findArtifactRefs(text)).toEqual([
            { artifact_id: 'b_1', name: 'B' },
            { artifact_id: 'a_1', name: 'A' },
        ]);
    });

    it('returns empty for non-string / no refs', () => {
        expect(findArtifactRefs(undefined)).toEqual([]);
        expect(findArtifactRefs(42)).toEqual([]);
        expect(findArtifactRefs('plain text')).toEqual([]);
        expect(findArtifactRefs('[Artifact: no-id-here]')).toEqual([]);
    });
});

describe('stepArtifactRefs', () => {
    it('collects from summary and output string values, deduped', () => {
        const step = {
            summary: 'done [Artifact: Report | ID: r_1 | Version: v3]',
            output_data: { text: 'see [Artifact: Report | ID: r_1 | v3]', extra: 7 },
        };
        expect(stepArtifactRefs(step)).toEqual([{ artifact_id: 'r_1', name: 'Report' }]);
    });

    it('scans nested non-string output values', () => {
        const step = {
            summary: '',
            output_data: { payload: { note: '[Artifact: Chart | ID: c_9 | v1]' } },
        };
        expect(stepArtifactRefs(step)).toEqual([{ artifact_id: 'c_9', name: 'Chart' }]);
    });
});

describe('matchVerdictView (#422 F-1)', () => {
    it('returns null for non-match nodes', () => {
        expect(matchVerdictView({ node_name: 'repo_scaffold', output_data: { verdict: 'extend' } })).toBeNull();
        expect(matchVerdictView({ node_name: 'historical_match', output_data: {} })).toBeNull();
    });

    it('extracts verdict view from match output', () => {
        const view = matchVerdictView({
            node_name: 'historical_match',
            output_data: {
                verdict: 'extend',
                score: 0.87,
                threshold: 0.6,
                gaps: ['loyalty'],
                candidates_scanned: 3,
                matched_repo: 'o/ai-pet',
                matched_visible: { slug: 'ai-pet', capabilities: ['booking'] },
            },
        });
        expect(view).not.toBeNull();
        expect(view!.verdict).toBe('extend');
        expect(view!.score).toBe(0.87);
        expect(view!.gaps).toEqual(['loyalty']);
        expect(view!.matchedRepo).toBe('o/ai-pet');
    });

    it('keeps base-verdict privacy fields as-is', () => {
        const view = matchVerdictView({
            node_name: 'historical_match',
            output_data: { verdict: 'base', score: 0.9, matched_repo: '', matched_visible: {} },
        });
        expect(view!.verdict).toBe('base');
        expect(view!.matchedVisible).toEqual({});
    });
});

describe('applyMatchOverrides (#422 F-1 re-verdict)', () => {
    it('returns null view untouched', () => {
        expect(applyMatchOverrides(null, { verdict: 'new' })).toBeNull();
        expect(applyMatchOverrides(null, undefined)).toBeNull();
    });

    it('applies override_output merge to the view', () => {
        const view = matchVerdictView({
            node_name: 'historical_match',
            output_data: { verdict: 'extend', score: 0.87, matched_repo: 'o/ai-pet', matched_visible: { slug: 'ai-pet' } },
        });
        const flipped = applyMatchOverrides(view, { verdict: 'new', matched_repo: '' });
        expect(flipped!.verdict).toBe('new');
        expect(flipped!.matchedRepo).toBe('');
        // score 等未覆盖字段保留
        expect(flipped!.score).toBe(0.87);
    });

    it('drops the view when override verdict is invalid', () => {
        const view = matchVerdictView({
            node_name: 'historical_match',
            output_data: { verdict: 'extend', score: 0.87 },
        });
        expect(applyMatchOverrides(view, { verdict: 'bogus' })).toBeNull();
    });
});
