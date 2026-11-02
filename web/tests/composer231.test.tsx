import { describe, expect, it } from 'bun:test';
import { Composer } from '@fusion/web-ui';
import { renderToStaticMarkup } from 'react-dom/server';
import {
    type RefSource,
    buildContextChips,
    buildReferenceItems,
    composerApprovalVariant,
    insertRefToken,
} from '../src/composer';

// issue #231: cowork 接线契约 — FCS-1 五区 / 审批禁用变体 / ＠ 三类引用 / 固定发送标签

const BASE = { value: 'hello', onChange: () => {}, onSubmit: () => {} };

describe('buildReferenceItems (@ palette)', () => {
    const sources: RefSource[] = [
        { kind: 'directory', id: 'sp1', name: 'Finance' },
        { kind: 'task', id: 'run1', name: 'October reconciliation' },
        { kind: 'artifact', id: 'art1', name: 'Q3 Report' },
    ];

    it('offers directory, task and artifact references', () => {
        const items = buildReferenceItems(sources, () => {});
        expect(items.map((i) => i.label)).toEqual(['📁 Finance', '☰ October reconciliation', '▦ Q3 Report']);
        expect(items.map((i) => i.key)).toEqual(['directory:sp1', 'task:run1', 'artifact:art1']);
    });

    it('clicking an item inserts the typed token', () => {
        const tokens: string[] = [];
        const items = buildReferenceItems(sources, (t) => tokens.push(t));
        for (const item of items) item.onClick?.();
        expect(tokens).toEqual(['@目录:Finance', '@任务:October reconciliation', '@产物:Q3 Report']);
    });
});

describe('insertRefToken', () => {
    it('adds a separating space and trailing space only when needed', () => {
        expect(insertRefToken('', '@产物:A')).toBe('@产物:A ');
        expect(insertRefToken('do it', '@产物:A')).toBe('do it @产物:A ');
        expect(insertRefToken('do it ', '@产物:A')).toBe('do it @产物:A ');
        expect(insertRefToken('line\n', '@产物:A')).toBe('line\n@产物:A ');
    });
});

describe('buildContextChips (zone 1)', () => {
    it('working directory chip is switchable; one-shot chips clear on click', () => {
        let switched = 0;
        let deriveCleared = 0;
        let artifactCleared = 0;
        const chips = buildContextChips({
            dirLabel: 'Finance',
            onSwitchDir: () => switched++,
            deriveFrom: 'run-abcdef123456',
            deriveLabel: 'Derived from',
            onClearDerive: () => deriveCleared++,
            artifactRefName: 'Q3 Report',
            onClearArtifact: () => artifactCleared++,
        });
        expect(chips.map((c) => c.label)).toEqual(['📁 Finance', '✚ Derived from run-abcd', '▦ Q3 Report']);
        chips[0].onClick();
        chips[1].onClick();
        chips[2].onClick();
        expect([switched, deriveCleared, artifactCleared]).toEqual([1, 1, 1]);
    });

    it('no dir chip for run view (fixed context); approval badge only when pending', () => {
        const idle = buildContextChips({ waitingApproval: false });
        expect(idle).toEqual([]);
        const pending = buildContextChips({ waitingApproval: true, waitingLabel: 'Awaiting approval' });
        expect(pending).toEqual([{ key: 'hitl', label: '⏸ Awaiting approval', onClick: expect.any(Function) }]);
    });
});

describe('approval disabled variant (§6)', () => {
    it('disabled exactly while a run waits for approval', () => {
        expect(composerApprovalVariant(false)).toEqual({ disabled: false, waitingApproval: false });
        expect(composerApprovalVariant(true)).toEqual({ disabled: true, waitingApproval: true });
    });

    it('renders the badge in the context bar and a disabled textarea (no replacement UI)', () => {
        const hitl = composerApprovalVariant(true);
        const html = renderToStaticMarkup(
            <Composer
                {...BASE}
                value=""
                disabled={hitl.disabled}
                contextChips={buildContextChips({
                    waitingApproval: hitl.waitingApproval,
                    waitingLabel: 'Awaiting approval',
                })}
                ariaLabel="run instruction"
            />
        );
        expect(html).toContain('⏸ Awaiting approval');
        expect(html).toContain('aria-label="run instruction"');
        expect(html).toMatch(/<textarea[^>]*disabled/);
        expect(html).toContain('disabled:cursor-not-allowed');
    });
});

describe('task composer five zones', () => {
    it('zones 1-5 render with cowork wiring', () => {
        const html = renderToStaticMarkup(
            <Composer
                {...BASE}
                contextChips={buildContextChips({ dirLabel: 'Finance', onSwitchDir: () => {} })}
                attachments={[{ key: 'f1', label: 'report.xlsx' }]}
                onRemoveAttachment={() => {}}
                attach={{ onFiles: () => {} }}
                references={{ items: buildReferenceItems([{ kind: 'artifact', id: 'a1', name: 'Q3' }], () => {}) }}
                params={{ model: { value: 'm', onChange: () => {}, options: [] } }}
                sendVariant="pill"
                sendLabel="Plan"
                hint="Enter to plan · Shift+Enter new line"
            />
        );
        expect(html).toContain('📁 Finance'); // zone 1
        expect(html).toContain('report.xlsx'); // zone 2
        expect(html).toContain('aria-label="remove report.xlsx"');
        expect(html).toContain('<textarea'); // zone 3
        expect(html).toContain('aria-label="attach files"'); // zone 4 left
        expect(html).toContain('aria-label="reference"');
        expect(html).toContain('aria-label="parameters"'); // zone 4 right
        expect(html).toContain('aria-label="Plan"');
        // send is rightmost in the tool row
        expect(html.lastIndexOf('aria-label="Plan"')).toBeGreaterThan(html.lastIndexOf('aria-label="parameters"'));
        expect(html).toContain('Enter to plan · Shift+Enter new line'); // zone 5
        expect(html).not.toContain('<select');
    });

    it('fixed pill label never swaps with state (§2)', () => {
        const idle = renderToStaticMarkup(<Composer {...BASE} sendVariant="pill" sendLabel="Plan" stopLabel="Stop" />);
        expect(idle).toContain('aria-label="Plan"');
        const busy = renderToStaticMarkup(
            <Composer {...BASE} busy onStop={() => {}} sendVariant="pill" sendLabel="Plan" stopLabel="Stop" />
        );
        expect(busy).toContain('⏹');
        expect(busy).toContain('aria-label="Stop"');
        expect(busy).toContain('>Plan</span>');
    });
});
