// issue #302: governance hub helpers — deterministic gate mapping n2->G1,
// n5->G4, n7->G5 over run steps (zero LLM), governance run filter, and
// audit summary aggregation from #303 hitl_attribution records.

export interface GovStep {
    node_id: string;
    status: string;
    output_data?: Record<string, unknown> | null;
}

export type GateState = 'done' | 'waiting' | 'failed' | 'pending';

export interface GateProgressItem {
    gate: 'G1' | 'G4' | 'G5';
    node: string;
    state: GateState;
}

const GATE_NODES: { gate: 'G1' | 'G4' | 'G5'; node: string }[] = [
    { gate: 'G1', node: 'n2' },
    { gate: 'G4', node: 'n5' },
    { gate: 'G5', node: 'n7' },
];

export function gateStateOf(statuses: string[]): GateState {
    // a node may appear multiple times (blocked gate step + later terminal
    // step): terminal success wins, blocked-only = waiting, other terminal = failed
    if (statuses.includes('success')) return 'done';
    if (statuses.length > 0 && statuses.every((s) => s === 'blocked')) return 'waiting';
    if (statuses.length > 0) return 'failed';
    return 'pending';
}

export function govGateProgress(steps: GovStep[]): GateProgressItem[] {
    const byNode = new Map<string, string[]>();
    for (const s of steps ?? []) {
        const list = byNode.get(s.node_id) ?? [];
        list.push(String(s.status ?? ''));
        byNode.set(s.node_id, list);
    }
    return GATE_NODES.map(({ gate, node }) => ({
        gate,
        node,
        state: gateStateOf(byNode.get(node) ?? []),
    }));
}

export function isGovernanceRun(workflowName: string): boolean {
    const n = workflowName ?? '';
    return n.includes('G1-G5') || n.includes('governance') || n.includes('治理');
}

export interface AuditSummary {
    decisions: number;
    byDecidedBy: Record<string, number>;
}

export function govAuditSummary(steps: GovStep[]): AuditSummary {
    const by: Record<string, number> = {};
    let decisions = 0;
    for (const s of steps ?? []) {
        const attr = s.output_data?.hitl_attribution as Record<string, unknown> | undefined;
        if (!attr) continue;
        decisions += 1;
        const who = String(attr.decided_by ?? 'unknown');
        by[who] = (by[who] ?? 0) + 1;
    }
    return { decisions, byDecidedBy: by };
}
