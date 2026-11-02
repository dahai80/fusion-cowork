import { describe, expect, it } from 'bun:test';
import type { CapabilityCatalog, CapabilityItem } from '../src/api';
import {
    capabilityItems,
    capabilityPitches,
    capabilityTypeFromRoute,
    filterCapabilities,
    hasInstalledCapabilities,
    poolExtraCapabilities,
    poolSkillNames,
    usableCapability,
} from '../src/capabilities';

// issue #228: capability marketplace — pool/tab/filter contracts shared by the
// CapabilitiesPage, HomePage picker and the ⌘K search route.

function item(over: Partial<CapabilityItem>): CapabilityItem {
    return {
        id: 'skill.echo',
        type: 'skill',
        name: 'echo',
        title: 'Echo',
        description: '',
        source: 'user_pack',
        installed: false,
        enabled: true,
        usage: { count: 0, last_used_at: null },
        audit_count: 0,
        granted_scopes: [],
        connected: false,
        requires_approval: false,
        sensitive_scopes: [],
        scopes: [],
        ...over,
    };
}

const CATALOG: CapabilityCatalog = {
    workspace_id: 'sp_a',
    skills: [
        item({ id: 'skill.echo', name: 'echo', title: 'Echo', installed: true }),
        item({ id: 'skill.report', name: 'report', title: 'Weekly Report', installed: false }),
    ],
    connectors: [
        item({
            id: 'connector.sheets',
            type: 'connector',
            name: 'sheets',
            title: 'Google Sheets',
            source: 'connector_config',
            installed: true,
        }),
    ],
    subagents: [
        item({
            id: 'subagent.graph.g1',
            type: 'subagent',
            name: 'g1',
            title: 'Researcher',
            source: 'agent_studio',
            installed: true,
        }),
    ],
    sources: [{ name: 'agent_studio', available: true }],
    count: 4,
};

describe('capabilityTypeFromRoute', () => {
    it('maps deep links and defaults to skills', () => {
        expect(capabilityTypeFromRoute('connector')).toBe('connector');
        expect(capabilityTypeFromRoute('subagent')).toBe('subagent');
        expect(capabilityTypeFromRoute('skill')).toBe('skill');
        expect(capabilityTypeFromRoute('')).toBe('skill');
        expect(capabilityTypeFromRoute('bogus')).toBe('skill');
    });
});

describe('capabilityItems tabs', () => {
    it('selects the tab bucket', () => {
        expect(capabilityItems(CATALOG, 'skill').map((i) => i.id)).toEqual(['skill.echo', 'skill.report']);
        expect(capabilityItems(CATALOG, 'connector').map((i) => i.id)).toEqual(['connector.sheets']);
        expect(capabilityItems(CATALOG, 'subagent').map((i) => i.id)).toEqual(['subagent.graph.g1']);
        expect(capabilityItems(null, 'skill')).toEqual([]);
    });
});

describe('filterCapabilities', () => {
    const skills = capabilityItems(CATALOG, 'skill');
    it('matches title, name and description case-insensitively', () => {
        expect(filterCapabilities(skills, 'WEEKLY').map((i) => i.id)).toEqual(['skill.report']);
        expect(filterCapabilities(skills, 'echo').map((i) => i.id)).toEqual(['skill.echo']);
        expect(filterCapabilities(skills, '  ')).toHaveLength(2);
    });
    it('returns empty for no match', () => {
        expect(filterCapabilities(skills, 'zzz')).toEqual([]);
    });
});

describe('workspace pool', () => {
    it('usable requires installed and enabled', () => {
        expect(usableCapability(item({ installed: true, enabled: true }))).toBe(true);
        expect(usableCapability(item({ installed: true, enabled: false }))).toBe(false);
        expect(usableCapability(item({ installed: false }))).toBe(false);
    });

    it('prefers installed skills, falls back to full catalog when pool empty', () => {
        expect(poolSkillNames(CATALOG)).toEqual(['echo']);
        const empty: CapabilityCatalog = {
            ...CATALOG,
            skills: CATALOG.skills.map((s) => ({ ...s, installed: false })),
        };
        expect(poolSkillNames(empty)).toEqual(['echo', 'report']);
        expect(poolSkillNames(null)).toEqual([]);
    });

    it('extra picker entries are installed connectors and subagents only', () => {
        expect(poolExtraCapabilities(CATALOG).map((i) => i.id)).toEqual(['connector.sheets', 'subagent.graph.g1']);
        const noInstall: CapabilityCatalog = {
            ...CATALOG,
            connectors: CATALOG.connectors.map((c) => ({ ...c, installed: false })),
        };
        expect(poolExtraCapabilities(noInstall).map((i) => i.id)).toEqual(['subagent.graph.g1']);
    });
});

describe('pitch cards (#244 R1)', () => {
    it('offers the three anthropic-style pitches', () => {
        const pitches = capabilityPitches();
        expect(pitches.map((p) => p.key)).toEqual(['skill', 'connector', 'subagent']);
        expect(pitches.map((p) => p.title.zh)).toEqual(['技能', '连接器', '子代理']);
        expect(pitches[0].desc.zh).toContain('领域知识');
        expect(pitches[1].desc.zh).toContain('已在用的工具');
        expect(pitches[2].desc.zh).toContain('专项任务');
    });

    it('hides pitches once anything is installed in the workspace', () => {
        const empty = { skills: [], connectors: [], subagents: [] };
        expect(hasInstalledCapabilities(empty)).toBe(false);
        expect(hasInstalledCapabilities(null)).toBe(false);
        const installed = {
            skills: [{ ...item({}), installed: true }],
            connectors: [],
            subagents: [],
        };
        expect(hasInstalledCapabilities(installed)).toBe(true);
    });
});
