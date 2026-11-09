// issue #224: unified run view — pure helpers (wave grouping + capability
// annotation), extracted so bun tests can pin them. No React imports.

export interface WaveNode {
    id: string;
    name: string;
    label?: string;
}

export interface Wave {
    depth: number;
    nodes: WaveNode[];
}

export type CapabilityKind = 'connector' | 'subagent' | 'ai' | 'skill' | 'system';

export interface CapabilityAnnotation {
    kind: CapabilityKind;
    label: string;
    detail?: string;
}

export interface DefNode {
    id: string;
    name: string;
    config?: { label?: string };
}

// issue #303: workflow_def.nodes arrives in BOTH shapes — dict keyed by node
// id (Workflow.to_dict, template launches) and list (template JSON). Normalize
// so wave grouping and the gate header never crash on either.
export function defNodes(def: { nodes?: unknown } | undefined | null): DefNode[] {
    const raw = def?.nodes;
    if (Array.isArray(raw)) return raw as DefNode[];
    if (raw && typeof raw === 'object') return Object.values(raw as Record<string, DefNode>);
    return [];
}

// Anthropic M4/M10: parallel branches render side-by-side — group workflow
// nodes by topological depth from the DAG (nodes at the same depth run in
// the same engine wave, v0.5.40 wave scheduler). Unknown/absent edges fall
// back to a single sequential wave so the timeline never breaks.
export function computeWaves(def: {
    nodes?: { id: string; name: string; config?: { label?: string } }[];
    edges?: { source_id: string; target_id: string }[];
}): Wave[] {
    const nodes = defNodes(def);
    if (nodes.length === 0) return [];
    const edges = def.edges ?? [];
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const depth = new Map<string, number>();
    nodes.forEach((n) => depth.set(n.id, 0));
    // Bellman-Ford style relax: |V| passes are enough for a DAG; cycle safety
    // capped by pass count (engine rejects cycles upstream anyway).
    for (let pass = 0; pass < nodes.length; pass += 1) {
        let changed = false;
        for (const e of edges) {
            const from = byId.get(e.source_id);
            const to = byId.get(e.target_id);
            if (!from || !to) continue;
            const next = (depth.get(e.source_id) ?? 0) + 1;
            if (next > (depth.get(e.target_id) ?? 0)) {
                depth.set(e.target_id, next);
                changed = true;
            }
        }
        if (!changed) break;
    }
    const waves = new Map<number, WaveNode[]>();
    for (const n of nodes) {
        const d = depth.get(n.id) ?? 0;
        const bucket = waves.get(d) ?? [];
        bucket.push({ id: n.id, name: n.name, label: n.config?.label });
        waves.set(d, bucket);
    }
    return [...waves.entries()].sort((a, b) => a[0] - b[0]).map(([d, ws]) => ({ depth: d, nodes: ws }));
}

const CONNECTOR_NODES = new Set(['mcp_client', 'http_request', 'web_search', 'fetch_url', 'cdp_navigate']);
const SUBAGENT_NODES = new Set(['studio_run']);
const AI_NODES = new Set(['ai_classify', 'ai_summarize', 'ai_generate_name']);

// M10: capability visibility — annotate a step with the capability class it
// exercised, derived from the node name + its inputs (connector target,
// sub-agent graph). Pure data derivation, no backend fields required.
export function stepCapability(step: {
    node_name: string;
    input_data?: Record<string, unknown>;
    output_data?: Record<string, unknown>;
}): CapabilityAnnotation | null {
    const name = step.node_name;
    const inputs = step.input_data ?? {};
    if (CONNECTOR_NODES.has(name)) {
        const target =
            str(inputs.tool) ||
            str(inputs.url) ||
            str((inputs.params as Record<string, unknown> | undefined)?.tool) ||
            str((inputs.params as Record<string, unknown> | undefined)?.url);
        return { kind: 'connector', label: name, detail: target || undefined };
    }
    if (SUBAGENT_NODES.has(name)) {
        const graph = str(inputs.graph_id) || str((inputs.params as Record<string, unknown> | undefined)?.graph_id);
        return { kind: 'subagent', label: name, detail: graph || undefined };
    }
    if (name.startsWith('skill.')) {
        return { kind: 'skill', label: name };
    }
    if (AI_NODES.has(name)) {
        return { kind: 'ai', label: name };
    }
    return null;
}

function str(v: unknown): string {
    return typeof v === 'string' && v.trim() !== '' ? v.trim() : '';
}

// Right-rail "capabilities in use" — dedupe annotations across steps,
// ordered by first appearance in the timeline.
export interface CapabilityInUse {
    kind: CapabilityKind;
    label: string;
    detail?: string;
    count: number;
}

export function capabilitiesInUse(
    steps: { node_name: string; input_data?: Record<string, unknown> }[]
): CapabilityInUse[] {
    const order: CapabilityInUse[] = [];
    const byKey = new Map<string, CapabilityInUse>();
    for (const s of steps) {
        const cap = stepCapability(s);
        if (!cap) continue;
        const key = `${cap.kind}:${cap.label}:${cap.detail ?? ''}`;
        const existing = byKey.get(key);
        if (existing) {
            existing.count += 1;
        } else {
            const entry: CapabilityInUse = { ...cap, count: 1 };
            byKey.set(key, entry);
            order.push(entry);
        }
    }
    return order;
}

// issue #225: 审批卡 "编辑参数" 草稿解析 — 只接受 JSON 对象。
export type ParamsDraftResult =
    | { ok: true; params: Record<string, unknown> }
    | { ok: false; error: 'invalid_json' | 'not_object' };

export function parseParamsDraft(text: string): ParamsDraftResult {
    let parsed: unknown;
    try {
        parsed = JSON.parse(text);
    } catch {
        return { ok: false, error: 'invalid_json' };
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { ok: false, error: 'not_object' };
    }
    return { ok: true, params: parsed as Record<string, unknown> };
}

// issue #233: artifact 轻量引用提取 — 引擎 ref_text 格式
// [Artifact: <name> | ID: <id> | Version: v<n> | Type: <type> | Size: <bytes> | Summary: …]
const ARTIFACT_REF_RE = /\[Artifact:\s*([^|\]]+?)\s*\|\s*ID:\s*([A-Za-z0-9_-]+)[^\]]*\]/g;

export interface ArtifactRef {
    artifact_id: string;
    name: string;
}

export function findArtifactRefs(text: unknown): ArtifactRef[] {
    if (typeof text !== 'string' || !text) return [];
    const seen = new Set<string>();
    const refs: ArtifactRef[] = [];
    for (const m of text.matchAll(ARTIFACT_REF_RE)) {
        const id = m[2];
        if (seen.has(id)) continue;
        seen.add(id);
        refs.push({ artifact_id: id, name: m[1].trim() });
    }
    return refs;
}

// issue #233: 步骤产物引用 — summary 优先, 其次输出里的字符串值 (输出保持轻量, 不做全文展开)
export function stepArtifactRefs(step: { summary?: string; output_data?: Record<string, unknown> }): ArtifactRef[] {
    const out: ArtifactRef[] = [];
    const seen = new Set<string>();
    for (const ref of findArtifactRefs(step.summary ?? '')) {
        seen.add(ref.artifact_id);
        out.push(ref);
    }
    if (step.output_data) {
        for (const v of Object.values(step.output_data)) {
            for (const ref of findArtifactRefs(typeof v === 'string' ? v : JSON.stringify(v))) {
                if (seen.has(ref.artifact_id)) continue;
                seen.add(ref.artifact_id);
                out.push(ref);
            }
        }
    }
    return out;
}

// issue #422 F-1: historical-match verdict card — pure view model extracted
// from a historical_match step so bun tests can pin it. Renders only when the
// step is a historical_match node with a verdict in its output.
export interface MatchVerdictView {
    verdict: 'extend' | 'base' | 'new';
    score: number;
    threshold: number;
    gaps: string[];
    candidatesScanned: number;
    matchedRepo: string;
    matchedVisible: Record<string, unknown>;
}

export function matchVerdictView(step: {
    node_name?: string;
    output_data?: Record<string, unknown>;
}): MatchVerdictView | null {
    if (step.node_name !== 'historical_match') return null;
    const out = step.output_data ?? {};
    const verdict = out.verdict;
    if (verdict !== 'extend' && verdict !== 'base' && verdict !== 'new') return null;
    return {
        verdict,
        score: typeof out.score === 'number' ? out.score : 0,
        threshold: typeof out.threshold === 'number' ? out.threshold : 0.6,
        gaps: Array.isArray(out.gaps) ? (out.gaps as unknown[]).map(String) : [],
        candidatesScanned: typeof out.candidates_scanned === 'number' ? out.candidates_scanned : 0,
        matchedRepo: typeof out.matched_repo === 'string' ? out.matched_repo : '',
        matchedVisible:
            out.matched_visible && typeof out.matched_visible === 'object'
                ? (out.matched_visible as Record<string, unknown>)
                : {},
    };
}

// issue #422 F-1: apply override_output directive payloads (re-verdict) on top
// of the step output view — record.directives is the source of truth for what
// the engine will honor at the next node boundary.
export function applyMatchOverrides(
    view: MatchVerdictView | null,
    merge: Record<string, unknown> | undefined
): MatchVerdictView | null {
    if (!view || !merge) return view;
    const merged = { ...view } as Record<string, unknown>;
    if ('verdict' in merge) merged.verdict = merge.verdict;
    if ('score' in merge) merged.score = merge.score;
    if ('threshold' in merge) merged.threshold = merge.threshold;
    if ('matched_repo' in merge) merged.matchedRepo = merge.matched_repo;
    if ('candidates_scanned' in merge) merged.candidatesScanned = merge.candidates_scanned;
    if (Array.isArray(merge.gaps)) merged.gaps = (merge.gaps as unknown[]).map(String);
    if (merge.matched_visible && typeof merge.matched_visible === 'object') {
        merged.matchedVisible = merge.matched_visible as Record<string, unknown>;
    }
    const v = merged.verdict;
    if (v !== 'extend' && v !== 'base' && v !== 'new') return null;
    return merged as unknown as MatchVerdictView;
}
