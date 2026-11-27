// issue #226: terminal-status set aligned with WorkflowStatus — pages must not each maintain inconsistent subsets.
// WorkflowStatus: pending / running / success / failed / cancelled / partial /
// completed_with_denials; completed/canceled are legacy compatibility aliases.

export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set([
    'success',
    'completed',
    'failed',
    'cancelled',
    'canceled',
    'partial',
    'completed_with_denials',
]);

export function isTerminalRunStatus(status: string): boolean {
    return TERMINAL_RUN_STATUSES.has(status);
}

// issue #226: the run view's two history actions share one terminal-status gate — judged in one place, used in two.
export function runHistoryActions(status: string): { resumeFromStep: boolean; deriveNewTask: boolean } {
    const terminal = isTerminalRunStatus(status);
    return { resumeFromStep: terminal, deriveNewTask: terminal };
}
