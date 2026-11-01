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

// Anthropic M4/M10: parallel branches render side-by-side — group workflow
// nodes by topological depth from the DAG (nodes at the same depth run in
// the same engine wave, v0.5.40 wave scheduler). Unknown/absent edges fall
// back to a single sequential wave so the timeline never breaks.
export function computeWaves(def: {
    nodes?: { id: string; name: string; config?: { label?: string } }[];
    edges?: { source_id: string; target_id: string }[];
}): Wave[] {
    const nodes = def.nodes ?? [];
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
