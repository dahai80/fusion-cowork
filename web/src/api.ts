import { sdk } from './sdk';

// Typed client over the web API surface (port 11461 direct, or through the
// gateway /shell/fusion-cowork/ proxy when embedded). All calls go through
// sdk.http so auth is handled by the host bridge / same-origin gateway
// injection — this module holds no keys (spec S8).

export interface NodeInfo {
    name: string;
    display_name: string;
    category: string;
    description: string;
    icon: string;
    default_label: string;
    params_schema: Record<string, unknown>;
}

export interface WorkflowListItem {
    id: string;
    name: string;
    description: string;
    updated_at: number;
    node_count: number;
}

export interface WorkflowRecord {
    id: string;
    name: string;
    description: string;
    updated_at: number;
    workflow: {
        id?: string;
        name?: string;
        nodes: CanvasNodeDef[];
        edges: CanvasEdgeDef[];
    };
}

export interface CanvasNodeDef {
    id: string;
    name: string;
    category?: string;
    position?: { x: number; y: number };
    config?: { label?: string; params?: Record<string, unknown> };
}

export interface CanvasEdgeDef {
    source_id: string;
    target_id: string;
    source_output?: string;
    target_input?: string;
    label?: string;
}

export interface RunListItem {
    run_id: string;
    workflow_id: string;
    workflow_name: string;
    status: string;
    created_at: number;
    finished_at: number | null;
    error: string | null;
}

export interface RunStep {
    node_id: string;
    node_name: string;
    node_display_name: string;
    status: string;
    execution_time: number;
    summary: string;
    error: string | null;
    output_data: Record<string, unknown>;
}

export interface RunRecord extends RunListItem {
    steps: RunStep[];
    result: { summary?: string } | null;
}

export interface TemplateInfo {
    id: string;
    name: string;
    description?: string;
    category?: string;
    tags?: string[];
    needs_ai?: boolean;
}

export interface SchedulerTask {
    id: string;
    name: string;
    workflow_id: string;
    trigger_type: string;
    trigger_config: Record<string, unknown>;
    status: string;
    created_at: number;
    last_run: number | null;
    next_run: number | null;
    run_count: number;
    fail_count: number;
    description: string;
    tags: string[];
}

export interface SessionInfo {
    id: string;
    workflow_id: string;
    workflow_name: string;
    status: string;
    initial_input: Record<string, unknown>;
    execution_id: string;
    created_at: number;
    updated_at: number;
    completed_at: number | null;
    metadata: Record<string, unknown>;
}

export interface SpaceInfo {
    id: string;
    name: string;
    description: string;
    owner_id: string;
    status: string;
    created_at?: string;
}

export interface SpaceMemberInfo {
    user_id: string;
    display_name?: string;
    role?: string;
}

export interface SpaceMessage {
    id: string;
    user_id: string;
    content: string;
    content_type?: string;
    created_at?: string;
}

export interface SpaceArtifact {
    id: string;
    name: string;
    artifact_type: string;
    content: string;
    owner_user_id?: string;
    created_at?: string;
}

async function get<T>(path: string): Promise<T> {
    return (await sdk.http.get<T>(path)).data;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
    return (await sdk.http.post<T>(path, body)).data;
}

export const api = {
    nodes: () => get<{ nodes: NodeInfo[]; count: number }>('/api/nodes'),
    workflows: () => get<{ workflows: WorkflowListItem[]; count: number }>('/api/workflows'),
    workflow: (id: string) => get<WorkflowRecord>(`/api/workflows/${encodeURIComponent(id)}`),
    createWorkflow: (record: { id?: string; name: string; description: string; workflow: unknown }) =>
        post<WorkflowRecord>('/api/workflows', record),
    updateWorkflow: (id: string, record: { name: string; description: string; workflow: unknown }) =>
        sdk.http.put<WorkflowRecord>(`/api/workflows/${encodeURIComponent(id)}`, record).then((r) => r.data),
    deleteWorkflow: (id: string) => sdk.http.delete(`/api/workflows/${encodeURIComponent(id)}`),
    startRun: (body: { workflow?: unknown; workflow_id?: string; name?: string }) =>
        post<{ run_id: string; status: string }>('/api/runs', body),
    runs: () => get<{ runs: RunListItem[]; count: number }>('/api/runs'),
    run: (id: string) => get<RunRecord>(`/api/runs/${encodeURIComponent(id)}`),
    cancelRun: (id: string) => post<{ status: string }>(`/api/runs/${encodeURIComponent(id)}/cancel`),
    templates: (category = '') =>
        get<{ templates: TemplateInfo[]; count: number }>(`/api/templates?category=${encodeURIComponent(category)}`),
    template: (id: string) => get<TemplateInfo>(`/api/templates/${encodeURIComponent(id)}`),
    schedulerTasks: () => get<{ tasks: SchedulerTask[]; count: number }>('/api/scheduler/tasks'),
    createSchedulerTask: (body: {
        name: string;
        workflow_id: string;
        cron?: string;
        interval_minutes?: number;
        description?: string;
    }) => post<{ id: string; status: string }>('/api/scheduler/tasks', body),
    pauseSchedulerTask: (id: string) =>
        post<{ id: string; status: string }>(`/api/scheduler/tasks/${encodeURIComponent(id)}/pause`),
    resumeSchedulerTask: (id: string) =>
        post<{ id: string; status: string }>(`/api/scheduler/tasks/${encodeURIComponent(id)}/resume`),
    deleteSchedulerTask: (id: string) => sdk.http.delete(`/api/scheduler/tasks/${encodeURIComponent(id)}`),
    generateWorkflow: (body: { prompt: string; context?: Record<string, unknown> }) =>
        post<{ workflow: { name?: string; description?: string; nodes: CanvasNodeDef[]; edges: CanvasEdgeDef[] } }>(
            '/api/ai/generate-workflow',
            body
        ),
    sessions: (status = '') =>
        get<{ sessions: SessionInfo[]; count: number }>(`/api/sessions?status=${encodeURIComponent(status)}`),
    session: (id: string) => get<SessionInfo>(`/api/sessions/${encodeURIComponent(id)}`),
    // space surface (mounted sub-app /api/space)
    spaces: () => get<SpaceInfo[]>('/api/space/spaces'),
    createSpace: (body: { name: string; description?: string }) => post<SpaceInfo>('/api/space/spaces', body),
    deleteSpace: (id: string) => sdk.http.delete(`/api/space/spaces/${encodeURIComponent(id)}`),
    spaceMembers: (id: string) => get<SpaceMemberInfo[]>(`/api/space/spaces/${encodeURIComponent(id)}/members`),
    spaceMessages: (id: string) => get<SpaceMessage[]>(`/api/space/spaces/${encodeURIComponent(id)}/messages`),
    sendSpaceMessage: (id: string, content: string) =>
        post<SpaceMessage>(`/api/space/spaces/${encodeURIComponent(id)}/messages`, { user_id: 'local_user', content }),
    spaceArtifacts: (id: string) => get<SpaceArtifact[]>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts`),
    spaceArtifact: (id: string, aid: string) =>
        get<SpaceArtifact>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(aid)}`),
    createSpaceArtifact: (id: string, body: { name: string; artifact_type?: string; content?: string }) =>
        post<SpaceArtifact>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts`, body),
};

export function statusKind(status: string): 'success' | 'running' | 'error' | 'offline' | 'warning' {
    switch (status) {
        case 'success':
        case 'completed':
            return 'success';
        case 'running':
        case 'pending':
        case 'cancelling':
            return 'running';
        case 'failed':
            return 'error';
        case 'cancelled':
            return 'offline';
        default:
            return 'warning';
    }
}
