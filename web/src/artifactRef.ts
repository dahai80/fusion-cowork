import type { ArtifactCardData } from './api';

// issue #237: Artifacts GUI 深链 ?artifact_ref=<id> — 一次性消费, 失败显式提示

export interface ArtifactRef {
    id: string;
    name: string;
    refText: string;
    deepLink: string;
}

export interface ArtifactRefParam {
    id: string;
    present: boolean;
    valid: boolean;
}

export type ArtifactRefFailureKind = 'bad_id' | 'not_found' | 'unavailable';

const ARTIFACT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

export function parseArtifactRefParam(search: string): ArtifactRefParam {
    let raw: string | null = null;
    try {
        raw = new URLSearchParams(search).get('artifact_ref');
    } catch {
        return { id: '', present: false, valid: false };
    }
    if (raw === null) return { id: '', present: false, valid: false };
    const id = raw.trim();
    return { id, present: true, valid: ARTIFACT_ID_RE.test(id) };
}

export function artifactRefFromCard(card: ArtifactCardData, fallbackId: string): ArtifactRef {
    const id = card.artifact_id || fallbackId;
    return {
        id,
        name: (card.name || '').trim() || fallbackId,
        refText:
            card.ref_text ||
            `[Artifact: ${card.name || fallbackId} | ID: ${id} | Version: v${card.current_version || 1}]`,
        deepLink: card.deep_link || '',
    };
}

export function artifactRefPrompt(ref: ArtifactRef): string {
    return `基于产物「${ref.name}」继续任务：\n${ref.refText}\n\n新的要求：`;
}

export function artifactRefChipLabel(ref: ArtifactRef): string {
    return `▦ 来自 Artifacts · ${ref.name}`;
}

export function artifactRefFailureKind(error: unknown): ArtifactRefFailureKind {
    const status = (error as { status?: number } | null)?.status;
    const code = (error as { code?: string } | null)?.code;
    if (code === 'bad_id') return 'bad_id';
    if (status === 404 || code === 'artifact_not_found') return 'not_found';
    return 'unavailable';
}

export function artifactRefFailureMessage(kind: ArtifactRefFailureKind, t: (zh: string, en: string) => string): string {
    if (kind === 'bad_id') {
        return t('产物引用无效，未注入任务上下文', 'Invalid artifact reference; nothing was added to the task');
    }
    if (kind === 'not_found') {
        return t(
            '产物不存在或没有访问权限，未注入任务上下文',
            'Artifact not found or access denied; nothing was added to the task'
        );
    }
    return t(
        'Artifacts 引擎暂不可用，产物未注入任务上下文',
        'Artifacts engine unavailable; the artifact was not added to the task'
    );
}

// run 上下文: labels 供运行视图展示, inputs 注入首节点供节点读取 ref。
export function artifactRunContext(ref: ArtifactRef | null): {
    labels?: Record<string, string>;
    inputs?: Record<string, unknown>;
} {
    if (!ref) return {};
    const labels: Record<string, string> = { artifact_ref: ref.id, artifact_name: ref.name };
    if (ref.deepLink) labels.artifact_deep_link = ref.deepLink;
    return {
        labels,
        inputs: { artifact_ref: ref.refText, artifact_id: ref.id, artifact_name: ref.name },
    };
}
