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
