import { describe, expect, it } from 'bun:test';
import { Composer } from '@fusion/web-ui';
import { renderToStaticMarkup } from 'react-dom/server';
import type { RunRecord } from '../src/api';
import { buildContextChips, buildReferenceItems, composerApprovalVariant } from '../src/composer';
import { isTerminalRunStatus } from '../src/runstatus';

// issue #240 R2: paused-run feed 契约 — runs_get 的 hitl_pending 字段一路映射到
// composer 禁用 + ⏸ 徽标。后端「真实暂停的 run 返回 hitl_pending=true」由
// tests/test_issue_187_hitl_default.py 覆盖; 这里锁定前端消费端不吞字段。

function record(over: Partial<RunRecord>): RunRecord {
    return {
        run_id: 'run_240',
        workflow_id: 'wf_240',
        workflow_name: 'wf',
        status: 'paused',
        created_at: 0,
        finished_at: null,
        error: null,
        review: null,
        steps: [],
        result: null,
        ...over,
    } as RunRecord;
}

const BASE = { value: '', onChange: () => {}, onSubmit: () => {} };

describe('paused-run hitl feed (#240 R2)', () => {
    it('a live paused record with hitl_pending disables the composer with the badge', () => {
        const rec = record({ hitl_pending: true, hitl_deadline: 1893456000 });
        const variant = composerApprovalVariant(!!rec.hitl_pending);
        expect(variant).toEqual({ disabled: true, waitingApproval: true });
        const html = renderToStaticMarkup(
            <Composer
                {...BASE}
                disabled={variant.disabled}
                contextChips={buildContextChips({ waitingApproval: variant.waitingApproval, waitingLabel: '等待审批' })}
                ariaLabel="run instruction"
            />
        );
        expect(html).toContain('⏸ 等待审批');
        expect(html).toMatch(/<textarea[^>]*disabled/);
    });

    it('the same record without hitl_pending stays editable (feed break is visible)', () => {
        const rec = record({});
        const variant = composerApprovalVariant(!!rec.hitl_pending);
        expect(variant).toEqual({ disabled: false, waitingApproval: false });
        expect(buildContextChips({ waitingApproval: variant.waitingApproval })).toEqual([]);
    });

    it('a gated paused run is not terminal — history actions stay closed', () => {
        const rec = record({ status: 'paused', hitl_pending: true });
        expect(isTerminalRunStatus(rec.status)).toBe(false);
    });
});

describe('run-view @ palette offers all three kinds (#240 R1)', () => {
    it('directory, task and artifact sources map to the shared tokens', () => {
        const tokens: string[] = [];
        const items = buildReferenceItems(
            [
                { kind: 'directory', id: 'sp1', name: 'Finance' },
                { kind: 'task', id: 'run_9', name: 'October reconciliation' },
                { kind: 'artifact', id: 'art_1', name: 'Q3 Report' },
            ],
            (t) => tokens.push(t)
        );
        expect(items.map((i) => i.key)).toEqual(['directory:sp1', 'task:run_9', 'artifact:art_1']);
        for (const item of items) item.onClick?.();
        expect(tokens).toEqual(['@目录:Finance', '@任务:October reconciliation', '@产物:Q3 Report']);
    });
});
