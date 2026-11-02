import { describe, expect, it } from 'bun:test';
import { TERMINAL_RUN_STATUSES, isTerminalRunStatus, runHistoryActions } from '../src/runstatus';

// issue #226: 终态门必须与 backend WorkflowStatus 一致 — success/partial 是真实终态。

describe('terminal run status', () => {
    it('covers every WorkflowStatus terminal value', () => {
        for (const status of ['success', 'failed', 'cancelled', 'partial', 'completed_with_denials']) {
            expect(isTerminalRunStatus(status)).toBe(true);
        }
        expect(TERMINAL_RUN_STATUSES.has('completed')).toBe(true);
        expect(TERMINAL_RUN_STATUSES.has('canceled')).toBe(true);
    });

    it('keeps active states non-terminal', () => {
        for (const status of ['pending', 'running', 'paused', 'cancelling', '']) {
            expect(isTerminalRunStatus(status)).toBe(false);
        }
    });

    it('opens both history actions for success and partial runs', () => {
        for (const status of ['success', 'partial']) {
            expect(runHistoryActions(status)).toEqual({ resumeFromStep: true, deriveNewTask: true });
        }
    });

    it('keeps both history actions closed while a run can still be steered', () => {
        for (const status of ['running', 'paused']) {
            expect(runHistoryActions(status)).toEqual({ resumeFromStep: false, deriveNewTask: false });
        }
    });

    it('opens both history actions for failure states', () => {
        for (const status of ['failed', 'cancelled', 'completed_with_denials']) {
            expect(runHistoryActions(status)).toEqual({ resumeFromStep: true, deriveNewTask: true });
        }
    });
});
