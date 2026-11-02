// issue #226: 终态集合与 WorkflowStatus 对齐 — 页面不得各自维护不一致的子集。
// WorkflowStatus: pending / running / success / failed / cancelled / partial /
// completed_with_denials; completed/canceled 为历史兼容别名。

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

// issue #226: run 视图的两个历史动作共享同一终态门 — 一处判定, 两处使用。
export function runHistoryActions(status: string): { resumeFromStep: boolean; deriveNewTask: boolean } {
    const terminal = isTerminalRunStatus(status);
    return { resumeFromStep: terminal, deriveNewTask: terminal };
}
