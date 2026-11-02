import { describe, expect, it } from 'bun:test';
import type { ArtifactCardData } from '../src/api';
import {
    artifactRefChipLabel,
    artifactRefFailureKind,
    artifactRefFailureMessage,
    artifactRefFromCard,
    artifactRefPrompt,
    artifactRunContext,
    parseArtifactRefParam,
} from '../src/artifactRef';
import { buildContextChips } from '../src/composer';

// issue #237: Artifacts GUI 深链 — 参数解析 / chip / 上下文注入 / 失败提示契约

function card(over: Partial<ArtifactCardData> = {}): ArtifactCardData {
    return {
        artifact_id: 'art_1',
        name: 'Q3 Report',
        type: 'report',
        kind: 'document',
        current_version: 3,
        summary: '十月对账异常项汇总',
        source_module: 'fusion-doc',
        is_starred: false,
        deep_link: '/shell/fusion-artifacts/#/a/art_1',
        ref_text: '[Artifact: Q3 Report | ID: art_1 | Version: v3 | Type: report | Summary: 十月对账异常项汇总]',
        ...over,
    };
}

describe('parseArtifactRefParam', () => {
    it('reads a valid id and strips surrounding whitespace', () => {
        expect(parseArtifactRefParam('?artifact_ref=art_1')).toEqual({ id: 'art_1', present: true, valid: true });
        expect(parseArtifactRefParam('?x=1&artifact_ref=%20art_1%20')).toEqual({
            id: 'art_1',
            present: true,
            valid: true,
        });
    });

    it('reports absent params without flagging invalid', () => {
        expect(parseArtifactRefParam('?foo=bar')).toEqual({ id: '', present: false, valid: false });
        expect(parseArtifactRefParam('')).toEqual({ id: '', present: false, valid: false });
    });

    it('rejects ids outside the backend pattern', () => {
        expect(parseArtifactRefParam('?artifact_ref=').valid).toBe(false);
        expect(parseArtifactRefParam('?artifact_ref=../etc/passwd')).toEqual({
            id: '../etc/passwd',
            present: true,
            valid: false,
        });
        expect(parseArtifactRefParam(`?artifact_ref=${'a'.repeat(129)}`).valid).toBe(false);
    });
});

describe('artifact ref rendering', () => {
    it('builds prompt and chip from the resolved card', () => {
        const ref = artifactRefFromCard(card(), 'art_1');
        expect(ref.name).toBe('Q3 Report');
        expect(artifactRefPrompt(ref)).toContain('[Artifact: Q3 Report | ID: art_1');
        expect(artifactRefPrompt(ref)).toContain('新的要求：');
        expect(artifactRefChipLabel(ref)).toBe('▦ 来自 Artifacts · Q3 Report');
    });

    it('renders the removable context chip labelled "来自 Artifacts"', () => {
        let cleared = 0;
        const chips = buildContextChips({
            artifactRefName: 'Q3 Report',
            artifactFromArtifacts: true,
            onClearArtifact: () => {
                cleared += 1;
            },
        });
        expect(chips).toHaveLength(1);
        expect(chips[0].label).toBe('▦ 来自 Artifacts · Q3 Report');
        chips[0].onClick?.();
        expect(cleared).toBe(1);
    });

    it('falls back to the raw id when the card has no name', () => {
        const ref = artifactRefFromCard(card({ name: '' }), 'art_9');
        expect(ref.name).toBe('art_9');
        expect(ref.refText).toContain('ID: art_1');
    });
});

describe('artifactRunContext', () => {
    it('is empty when no artifact was resolved', () => {
        expect(artifactRunContext(null)).toEqual({});
    });

    it('carries labels for the run view and inputs for the first node', () => {
        const ref = artifactRefFromCard(card(), 'art_1');
        const ctx = artifactRunContext(ref);
        expect(ctx.labels).toEqual({
            artifact_ref: 'art_1',
            artifact_name: 'Q3 Report',
            artifact_deep_link: '/shell/fusion-artifacts/#/a/art_1',
        });
        expect(ctx.inputs).toEqual({
            artifact_ref: ref.refText,
            artifact_id: 'art_1',
            artifact_name: 'Q3 Report',
        });
    });
});

describe('failure notice', () => {
    it('maps HTTP statuses and error codes to failure kinds', () => {
        expect(artifactRefFailureKind({ status: 404, code: 'artifact_not_found' })).toBe('not_found');
        expect(artifactRefFailureKind({ status: 404, code: 'bad_id' })).toBe('bad_id');
        expect(artifactRefFailureKind({ status: 502 })).toBe('unavailable');
        expect(artifactRefFailureKind(new Error('network'))).toBe('unavailable');
    });

    it('only 404 is treated as missing; 401/403 stay unavailable (backend collapses them)', () => {
        expect(artifactRefFailureKind({ status: 401 })).toBe('unavailable');
        expect(artifactRefFailureKind({ status: 403 })).toBe('unavailable');
    });

    it('produces explicit bilingual notices instead of a silent no-op', () => {
        const t = (zh: string, en: string) => `${zh}|${en}`;
        expect(artifactRefFailureMessage('bad_id', t)).toContain('无效');
        expect(artifactRefFailureMessage('not_found', t)).toContain('不存在');
        expect(artifactRefFailureMessage('unavailable', t)).toContain('暂不可用');
        for (const kind of ['bad_id', 'not_found', 'unavailable'] as const) {
            expect(artifactRefFailureMessage(kind, t)).toContain('未注入任务上下文');
        }
    });
});
