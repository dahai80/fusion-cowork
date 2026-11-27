// issue #261: Canvas pure-function helpers — schema form field derivation + run status to node coloring.
// No DOM dependency; shared by CanvasPage and bun unit tests.

export interface SchemaProp {
    type?: string;
    description?: string;
    enum?: unknown[];
    default?: unknown;
    items?: { type?: string };
}

export interface SchemaField {
    key: string;
    required: boolean;
    prop: SchemaProp;
}

// params_schema (JSON Schema) → ordered form fields; required list pinned to top
export function schemaFields(schema: unknown): SchemaField[] {
    const s = schema as { properties?: Record<string, SchemaProp>; required?: string[] } | null | undefined;
    if (!s || typeof s !== 'object' || !s.properties) return [];
    const required = new Set(Array.isArray(s.required) ? s.required : []);
    return Object.entries(s.properties).map(([key, prop]) => ({ key, required: required.has(key), prop }));
}

// Field input kind: checkbox (boolean) / number / select (enum) / textarea (array|object) / text
export function fieldKind(prop: SchemaProp): 'checkbox' | 'number' | 'select' | 'textarea' | 'text' {
    if (prop.enum && prop.enum.length > 0) return 'select';
    if (prop.type === 'boolean') return 'checkbox';
    if (prop.type === 'integer' || prop.type === 'number') return 'number';
    if (prop.type === 'array' || prop.type === 'object') return 'textarea';
    return 'text';
}

// checkbox display value — params carried as strings, "true"/"false" coerced by the engine
export function isTrueish(v: unknown): boolean {
    return v === true || v === 'true' || v === 'True' || v === '1';
}

export type NodeRunTone = 'idle' | 'queued' | 'running' | 'passed' | 'failed' | 'blocked' | 'denied';

// run steps → node_id → latest execution status (retry takes the last one)
export function runStatusByNode(steps: { node_id: string; status: string }[] | undefined): Record<string, string> {
    const out: Record<string, string> = {};
    for (const s of steps ?? []) {
        if (s?.node_id) out[s.node_id] = s.status;
    }
    return out;
}

// node status → border color (Tailwind arbitrary values not usable here, direct style)
export const TONE_COLOR: Record<NodeRunTone, string> = {
    idle: '#6b7280',
    queued: '#94a3b8',
    running: '#3b82f6',
    passed: '#10b981',
    failed: '#ef4444',
    blocked: '#f59e0b',
    denied: '#ef4444',
};

export function toneFor(status: string | undefined): NodeRunTone {
    switch (status) {
        case 'success':
        case 'passed':
            return 'passed';
        case 'running':
            return 'running';
        case 'failed':
            return 'failed';
        case 'blocked':
            return 'blocked';
        case 'denied':
            return 'denied';
        case 'pending':
        case 'queued':
            return 'queued';
        default:
            return 'idle';
    }
}

export function toneColor(status: string | undefined): string {
    return TONE_COLOR[toneFor(status)];
}
