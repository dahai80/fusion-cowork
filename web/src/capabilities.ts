import type { CapabilityCatalog, CapabilityItem, CapabilityType } from './api';

export const CAPABILITY_TABS: Array<{ key: CapabilityType; zh: string; en: string; icon: string }> = [
    { key: 'skill', zh: '技能', en: 'Skills', icon: '⚙' },
    { key: 'connector', zh: '连接器', en: 'Connectors', icon: '🔌' },
    { key: 'subagent', zh: '子代理', en: 'Subagents', icon: '🤖' },
];

export function capabilityTypeFromRoute(arg: string): CapabilityType {
    return arg === 'connector' || arg === 'subagent' ? arg : 'skill';
}

export function capabilityItems(catalog: CapabilityCatalog | null, type: CapabilityType): CapabilityItem[] {
    if (!catalog) return [];
    if (type === 'connector') return catalog.connectors ?? [];
    if (type === 'subagent') return catalog.subagents ?? [];
    return catalog.skills ?? [];
}

export function filterCapabilities(items: CapabilityItem[], query: string): CapabilityItem[] {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((i) =>
        [i.title, i.title_en, i.name, i.description, i.description_en]
            .filter(Boolean)
            .join(' ')
            .toLowerCase()
            .includes(q)
    );
}

export function usableCapability(item: CapabilityItem): boolean {
    return item.installed && item.enabled;
}

// Installed skills first; when none are installed the capability gate is inert,
// so the picker falls back to the full catalog.
export function poolSkillNames(catalog: CapabilityCatalog | null): string[] {
    const skills = catalog?.skills ?? [];
    const installed = skills.filter(usableCapability);
    return (installed.length > 0 ? installed : skills).map((s) => s.name);
}

export function poolExtraCapabilities(catalog: CapabilityCatalog | null): CapabilityItem[] {
    return [...(catalog?.connectors ?? []), ...(catalog?.subagents ?? [])].filter(usableCapability);
}

// issue #244 R1: 首访 pitch 卡 — 三类能力一句话定位, 安装任意能力后永久消失。
export const PITCH_DISMISS_KEY = 'cowork.capabilities.pitchDismissed';

export interface CapabilityPitch {
    key: CapabilityType;
    icon: string;
    title: { zh: string; en: string };
    desc: { zh: string; en: string };
}

export function capabilityPitches(): CapabilityPitch[] {
    return [
        {
            key: 'skill',
            icon: '⚙',
            title: { zh: '技能', en: 'Skills' },
            desc: { zh: '领域知识与最佳实践', en: 'Domain knowledge and best practices' },
        },
        {
            key: 'connector',
            icon: '🔌',
            title: { zh: '连接器', en: 'Connectors' },
            desc: { zh: '接入你已在用的工具', en: 'Bring the tools you already use' },
        },
        {
            key: 'subagent',
            icon: '🤖',
            title: { zh: '子代理', en: 'Subagents' },
            desc: { zh: '端到端处理专项任务', en: 'Handle specialized tasks end to end' },
        },
    ];
}

export function pitchesDismissed(): boolean {
    try {
        return localStorage.getItem(PITCH_DISMISS_KEY) === '1';
    } catch {
        return true;
    }
}

export function dismissPitches(): void {
    try {
        localStorage.setItem(PITCH_DISMISS_KEY, '1');
    } catch {
        // 无 localStorage (隐私模式) — 会话内 state 兜底
    }
}

export function hasInstalledCapabilities(
    catalog: { skills?: CapabilityItem[]; connectors?: CapabilityItem[]; subagents?: CapabilityItem[] } | null
): boolean {
    if (!catalog) return false;
    for (const list of [catalog.skills, catalog.connectors, catalog.subagents]) {
        if ((list ?? []).some(usableCapability)) return true;
    }
    return false;
}
