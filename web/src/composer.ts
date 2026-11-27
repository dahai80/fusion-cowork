import type { ComposerActionItem, ComposerChip } from '@fusion/web-ui';

// issue #231 (FCS-1): task/run composer shared wiring — pure functions produce the five-zone props, zero duplication across pages

export type RefKind = 'directory' | 'task' | 'artifact';

export interface RefSource {
    kind: RefKind;
    id: string;
    name: string;
}

const REF_META: Record<RefKind, { icon: string; token: string }> = {
    directory: { icon: '📁', token: '@目录' }, // i18n: user-facing
    task: { icon: '☰', token: '@任务' }, // i18n: user-facing
    artifact: { icon: '▦', token: '@产物' }, // i18n: user-facing
};

export function referenceToken(source: RefSource): string {
    return `${REF_META[source.kind].token}:${source.name}`;
}

export function buildReferenceItems(sources: RefSource[], insert: (token: string) => void): ComposerActionItem[] {
    return sources.map((s) => ({
        key: `${s.kind}:${s.id}`,
        label: `${REF_META[s.kind].icon} ${s.name}`,
        onClick: () => insert(referenceToken(s)),
    }));
}

// FCS-1 (1) context bar — working directory + one-shot chips (derive / artifact injection) + approval-waiting badge
export interface ContextChipInput {
    dirLabel?: string;
    onSwitchDir?: () => void;
    deriveFrom?: string;
    deriveLabel?: string;
    onClearDerive?: () => void;
    artifactRefName?: string;
    artifactFromArtifacts?: boolean;
    onClearArtifact?: () => void;
    waitingApproval?: boolean;
    waitingLabel?: string;
}

export function buildContextChips(input: ContextChipInput): ComposerChip[] {
    const chips: ComposerChip[] = [];
    if (input.dirLabel && input.onSwitchDir) {
        chips.push({ key: 'dir', label: `📁 ${input.dirLabel}`, onClick: input.onSwitchDir });
    }
    if (input.deriveFrom && input.onClearDerive) {
        chips.push({
            key: 'derive',
            label: `✚ ${input.deriveLabel ?? '派生自'} ${input.deriveFrom.slice(0, 8)}`, // i18n: user-facing
            onClick: input.onClearDerive,
        });
    }
    if (input.artifactRefName && input.onClearArtifact) {
        const label = input.artifactFromArtifacts
            ? `▦ 来自 Artifacts · ${input.artifactRefName}` // i18n: user-facing
            : `▦ ${input.artifactRefName}`;
        chips.push({ key: 'artifact', label, onClick: input.onClearArtifact });
    }
    if (input.waitingApproval) {
        chips.push({ key: 'hitl', label: `⏸ ${input.waitingLabel ?? '等待审批'}`, onClick: () => undefined }); // i18n: user-facing
    }
    return chips;
}

// issue #231 §6: approval-waiting variant — composer disabled + badge, no alternate UI
export function composerApprovalVariant(hitlPending: boolean): { disabled: boolean; waitingApproval: boolean } {
    return { disabled: hitlPending, waitingApproval: hitlPending };
}

export function insertRefToken(prev: string, token: string): string {
    const sep = prev && !prev.endsWith(' ') && !prev.endsWith('\n') ? ' ' : '';
    return `${prev}${sep}${token} `;
}
