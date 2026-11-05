import { describe, expect, test } from 'bun:test';
import { govAuditSummary, govGateProgress, gateStateOf, isGovernanceRun } from '../src/governance';

// issue #302: governance hub helper logic — deterministic gate mapping,
// governance run filter, audit summary aggregation.

describe('gateStateOf', () => {
    test('success wins over blocked', () => {
        expect(gateStateOf(['blocked', 'success'])).toBe('done');
    });
    test('blocked-only is waiting', () => {
        expect(gateStateOf(['blocked'])).toBe('waiting');
    });
    test('failed terminal', () => {
        expect(gateStateOf(['failed'])).toBe('failed');
    });
    test('no steps pending', () => {
        expect(gateStateOf([])).toBe('pending');
    });
});

describe('govGateProgress', () => {
    test('n2/n5/n7 mapping', () => {
        const steps = [
            { node_id: 'n1', status: 'success' },
            { node_id: 'n2', status: 'blocked' },
            { node_id: 'n2', status: 'success' },
            { node_id: 'n5', status: 'blocked' },
        ];
        const gates = govGateProgress(steps as never);
        expect(gates.map((g) => `${g.gate}${g.state}`)).toEqual(['G1done', 'G4waiting', 'G5pending']);
    });
    test('empty steps all pending', () => {
        expect(govGateProgress([]).every((g) => g.state === 'pending')).toBe(true);
    });
});

describe('isGovernanceRun', () => {
    test('matches lifecycle + batch audit names', () => {
        expect(isGovernanceRun('G1-G5 gate pipeline for one issue')).toBe(true);
        expect(isGovernanceRun('治理-议题生命周期 (G1-G4-G5 闸门)')).toBe(true);
        expect(isGovernanceRun('governance batch audit')).toBe(true);
        expect(isGovernanceRun('desktop_daily_cleanup')).toBe(false);
    });
});

describe('govAuditSummary', () => {
    test('aggregates decisions by decided_by', () => {
        const steps = [
            {
                node_id: 'n2',
                status: 'success',
                output_data: { hitl_attribution: { decided_by: 'web', approval_id: 'a1' } },
            },
            {
                node_id: 'n5',
                status: 'success',
                output_data: { hitl_attribution: { decided_by: 'webhook', approval_id: 'a2' } },
            },
            { node_id: 'n7', status: 'pending', output_data: null },
        ];
        const s = govAuditSummary(steps as never);
        expect(s.decisions).toBe(2);
        expect(s.byDecidedBy).toEqual({ web: 1, webhook: 1 });
    });
});
