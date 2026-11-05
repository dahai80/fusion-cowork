// issue #300: render smoke test — TemplatesPage must survive the loading ->
// data transition. Pre-fix, the launch-form hooks were declared after the
// early returns, so the first render (loading skeleton) ran fewer hooks and
// the re-render crashed with React error #310 ("Rendered more hooks than
// during the previous render"), blanking the whole page.
import '@happy-dom/global-registrator/register';
import { beforeEach, describe, expect, it, mock } from 'bun:test';
import { createElement } from 'react';
import { act } from 'react-dom/test-utils';
import { createRoot } from 'react-dom/client';

let templatesPayload: unknown = { templates: [], count: 0 };
let templateDetailPayload: unknown = {};

mock.module('../src/api', () => ({
    api: {
        templates: async () => templatesPayload,
        template: async () => templateDetailPayload,
        publishTemplate: async () => ({ id: 'x', name: 'x' }),
        importTemplate: async () => ({ id: 'x', name: 'x' }),
        deleteTemplate: async () => ({}),
        templateExport: async () => '',
        launchTemplate: async () => ({ run_id: 'run_mock_300', status: 'running' }),
    },
}));

mock.module('../src/i18n', () => ({
    useT: () => (cn: string, en?: string) => en ?? cn,
}));

const { TemplatesPage } = await import('../src/pages/TemplatesPage');

(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true;

async function mountPage(): Promise<HTMLElement> {
    const container = document.createElement('div');
    document.body.appendChild(container);
    let root: ReturnType<typeof createRoot> | undefined;
    await act(async () => {
        root = createRoot(container);
        root.render(createElement(TemplatesPage));
    });
    return container;
}

describe('TemplatesPage render smoke (#300)', () => {
    beforeEach(() => {
        templatesPayload = { templates: [], count: 0 };
        templateDetailPayload = {};
    });

    it('survives loading -> data transition without hook-order crash', async () => {
        templatesPayload = {
            templates: [
                {
                    id: 'governance_issue_lifecycle',
                    name: 'gov lifecycle',
                    category: 'governance',
                    tags: ['gate'],
                },
            ],
            count: 1,
        };
        // first render: useAutoRetry starts loading -> early-return branch
        const container = await mountPage();
        expect(container.textContent).toContain('gov lifecycle');
        // data refresh re-render would throw #310 pre-fix
        await act(async () => {
            await new Promise((r) => setTimeout(r, 10));
        });
        expect(container.textContent).toContain('gov lifecycle');
    });

    it('renders empty catalog (skeleton branch) then remounts with a card', async () => {
        templatesPayload = { templates: [{ id: 't1', name: 'plain tpl' }], count: 1 };
        const container = await mountPage();
        expect(container.textContent).toContain('plain tpl');
    });
});
