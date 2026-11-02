import type { ComposerActionItem, ComposerChip } from '@fusion/web-ui';

// issue #231 (FCS-1): 任务/运行 composer 共享接线 — 纯函数产出五区 props, 页面零重复

export type RefKind = 'directory' | 'task' | 'artifact';

export interface RefSource {
    kind: RefKind;
    id: string;
    name: string;
}

const REF_META: Record<RefKind, { icon: string; token: string }> = {
    directory: { icon: '📁', token: '@目录' },
    task: { icon: '☰', token: '@任务' },
    artifact: { icon: '▦', token: '@产物' },
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

// FCS-1 ① 上下文条 — 工作目录 + 一次性 chip (派生/产物注入) + 审批等待徽标
export interface ContextChipInput {
    dirLabel?: string;
    onSwitchDir?: () => void;
    deriveFrom?: string;
    deriveLabel?: string;
    onClearDerive?: () => void;
    artifactRefName?: string;
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
            label: `✚ ${input.deriveLabel ?? '派生自'} ${input.deriveFrom.slice(0, 8)}`,
            onClick: input.onClearDerive,
        });
    }
    if (input.artifactRefName && input.onClearArtifact) {
        chips.push({ key: 'artifact', label: `▦ ${input.artifactRefName}`, onClick: input.onClearArtifact });
    }
    if (input.waitingApproval) {
        chips.push({ key: 'hitl', label: `⏸ ${input.waitingLabel ?? '等待审批'}`, onClick: () => undefined });
    }
    return chips;
}

// issue #231 §6: 审批等待变体 — composer 禁用 + 徽标, 不另造替代 UI
export function composerApprovalVariant(hitlPending: boolean): { disabled: boolean; waitingApproval: boolean } {
    return { disabled: hitlPending, waitingApproval: hitlPending };
}

export function insertRefToken(prev: string, token: string): string {
    const sep = prev && !prev.endsWith(' ') && !prev.endsWith('\n') ? ' ' : '';
    return `${prev}${sep}${token} `;
}
