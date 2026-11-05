import type { RunRecord, RunStep } from '../api';
import { useT } from '../i18n';
import { defNodes } from '../runview';

// issue #303: gate progress strip — G1✓ G4● G5○ derived from approval_gate
// nodes in the run's workflow_def plus their step states. Shared by the run
// detail header (#303) and the governance hub list (#302).

export interface GateProgressItem {
    id: string;
    label: string;
    state: 'done' | 'waiting' | 'failed' | 'pending';
}

export function gateProgress(def: RunRecord['workflow_def'], steps: RunStep[]): GateProgressItem[] {
    return defNodes(def ?? {})
        .filter((n) => n.name === 'approval_gate')
        .map((n, i) => {
            const nodeSteps = (steps ?? []).filter((s) => s.node_id === n.id && !s.user_correction);
            const blocked = nodeSteps.some((s) => s.status === 'blocked');
            const terminal = nodeSteps.find((s) => s.status !== 'blocked');
            let state: GateProgressItem['state'] = 'pending';
            if (terminal) state = terminal.status === 'success' ? 'done' : 'failed';
            else if (blocked) state = 'waiting';
            return { id: n.id, label: n.config?.label || `G${i + 1}`, state };
        });
}

export function GateProgress({ gates }: { gates: GateProgressItem[] }) {
    const t = useT();
    if (gates.length === 0) return null;
    const mark = { done: '✓', waiting: '●', failed: '✗', pending: '○' } as const;
    const cls = {
        done: 'border-success/40 text-success',
        waiting: 'border-warning/40 text-warning',
        failed: 'border-error/40 text-error',
        pending: 'border-border text-text-muted',
    } as const;
    return (
        <div className="flex flex-wrap items-center gap-1" title={t('闸门进度', 'Gate progress')}>
            {gates.map((g) => (
                <span key={g.id} className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${cls[g.state]}`}>
                    {mark[g.state]} {g.label}
                </span>
            ))}
        </div>
    );
}
