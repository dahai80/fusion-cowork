import { apiBase, runtimeApiKey, sdk } from './sdk';

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

export interface RunReview {
    status: 'pending_review' | 'approved' | 'rejected';
    note: string;
    reviewed_at: number | null;
}

export interface RunListItem {
    run_id: string;
    workflow_id: string;
    workflow_name: string;
    status: string;
    created_at: number;
    finished_at: number | null;
    error: string | null;
    review: RunReview | null;
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
    input_data?: Record<string, unknown>;
    tokens?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
}

export interface RunRecord extends RunListItem {
    steps: RunStep[];
    result: { summary?: string } | null;
}

// issue #150: 引用溯源 — 结构化引用 (文件路径 / 空间消息 id)
export interface CitationRef {
    type: string;
    path?: string;
    name?: string;
    message_id?: string;
}

// issue #148: chat-first 委托结果 — auto 直接执行的 run_id / plan 待批准
export interface ChatDelegateResult {
    mode: 'auto' | 'plan';
    workflow_id: string;
    run_id?: string;
    plan: { name: string; description: string; steps: { node: string; display: string; label: string }[] };
}

export interface FileReadResult {
    path: string;
    name: string;
    content: string;
    line_start: number;
    line_end: number;
    total_lines: number;
    truncated: boolean;
}

export interface RunArtifact {
    artifact_id: string;
    node_id: string;
    node_name: string;
    name: string;
    type: string;
    path: string;
    content: string;
    truncated: boolean;
    citations?: CitationRef[];
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

export type SessionSource = 'engine' | 'space' | 'scheduler';

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
    source: SessionSource;
    title: string;
    space_id: string;
    space_name: string;
    href: string;
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

export interface ArtifactReview {
    status: 'approved' | 'rejected';
    reviewed_by: string;
    reviewed_at: string;
    note: string;
}

export interface SpaceArtifact {
    id: string;
    name: string;
    artifact_type: string;
    content: string;
    owner_user_id?: string;
    created_at?: string;
    metadata?: string | Record<string, unknown>;
}

export interface UsageInfo {
    tenant_id: string;
    usage: { spaces: number; messages: number; artifacts: number; agents: number };
    quotas: Record<string, number>;
    unlimited: boolean;
}

export interface MobileOverview {
    runs: { run_id: string; workflow_name: string; status: string; created_at: number; error: string | null }[];
    pending_approvals: GuardApproval[];
    generated_at: number;
}

export interface SpaceNotification {
    id: string;
    space_id: string;
    user_id: string;
    notification_type: string;
    title: string;
    content?: string;
    read?: number;
    created_at?: string;
}

export interface PermissionMatrix {
    roles: string[];
    matrix: Record<string, Record<string, boolean>>;
}

export interface KbSearchResult {
    document_id?: string;
    document_name?: string;
    content?: string;
    score?: number;
}

export interface KbStatusInfo {
    kb_id?: string | null;
    bound?: boolean;
    document_count?: number;
}

export interface KbDocumentInfo {
    id?: string;
    document_id?: string;
    file_name?: string;
    name?: string;
    created_at?: string;
}

export interface ModelInfo {
    id: string;
    engine_type?: string;
}

export interface PluginInfo {
    id: string;
    name: string;
    category: string;
    version: string;
    description?: string;
    enabled?: boolean;
    installed?: boolean;
}

export interface RpcResponse<T> {
    jsonrpc: string;
    id?: unknown;
    result?: T;
    error?: { code: number; message: string; data?: unknown };
}

// issue #142: 审批 diff 分块 — SequenceMatcher opcode 切片, 非(equal)块可独立接受/拒绝
export interface DiffBlock {
    index: number;
    tag: 'equal' | 'replace' | 'delete' | 'insert';
    old: string;
    new: string;
    old_start?: number;
    new_start?: number;
}

export interface GuardApproval {
    action_id: string;
    tool_name: string;
    source?: string;
    params?: Record<string, unknown>;
    ts?: number;
    workflow?: string;
    node_id?: string;
    diff_blocks?: DiffBlock[];
}

export interface ClusterNodeInfo {
    node_id: string;
    host?: string;
    port?: number;
    role?: string;
    vram_total_mb?: number;
    vram_used_mb?: number;
    last_heartbeat_at?: number;
    tags?: string[];
}

export interface ClusterStateInfo {
    cluster_enabled: boolean;
    nodes: ClusterNodeInfo[];
    vram: Record<string, number>;
    total_allocated_mb: number;
}

async function get<T>(path: string): Promise<T> {
    return (await sdk.http.get<T>(path)).data;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
    return (await sdk.http.post<T>(path, body)).data;
}

async function put<T>(path: string, body?: unknown): Promise<T> {
    return (await sdk.http.put<T>(path, body)).data;
}

async function del<T>(path: string): Promise<T> {
    return (await sdk.http.delete<T>(path)).data;
}

// issue #156: 管理台密钥只在浏览器本地 (localStorage), 随请求走 x-admin-key。
// sdk.http 不接受自定义头, 管理台单独走同源 fetch (嵌入式 shell 不承载管理台)。
const ADMIN_KEY_STORAGE = 'fusion-cowork-admin-key';

export function adminKey(): string {
    try {
        return localStorage.getItem(ADMIN_KEY_STORAGE) ?? '';
    } catch {
        return '';
    }
}

export function saveAdminKey(key: string): void {
    try {
        if (key) localStorage.setItem(ADMIN_KEY_STORAGE, key);
        else localStorage.removeItem(ADMIN_KEY_STORAGE);
    } catch {
        // storage unavailable — header simply omitted
    }
}

async function adminRequest<T>(path: string, method: 'GET' | 'POST' = 'GET', body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers['content-type'] = 'application/json';
    const key = runtimeApiKey();
    if (key) headers['x-api-key'] = key;
    const admin = adminKey();
    if (admin) headers['x-admin-key'] = admin;
    const res = await fetch(apiBase() + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`admin request failed: ${res.status}`);
    return (await res.json()) as T;
}

export interface AdminMember {
    user_id: string;
    role: string;
    tenant_id?: string;
    space_count?: number;
}

export interface AdminUsage {
    spaces: { spaces: number; messages: number; artifacts: number; agents: number };
    runs: { total: number; by_status: Record<string, number> };
    tokens: number;
}

export interface AdminAuditExport {
    resourceLogs: {
        scopeLogs: {
            scope: { name: string };
            logRecords: { severityText: string; body: { stringValue: string }; attributes: unknown[] }[];
        }[];
    }[];
}

export const adminApi = {
    members: () => adminRequest<{ mode: string; members: AdminMember[] }>('/api/admin/members'),
    usage: () => adminRequest<AdminUsage>('/api/admin/usage'),
    auditExport: (since = 0, limit = 200) =>
        adminRequest<AdminAuditExport>(`/api/admin/audit/export?since=${since}&limit=${limit}`),
    pushTest: () => adminRequest<{ enabled: boolean; delivered: boolean }>('/api/admin/push/test', 'POST', {}),
};

export interface SkillParamInfo {
    name: string;
    label: string;
    label_en: string;
    type: string;
    required: boolean;
    default: unknown;
    options: string[];
    placeholder: string;
}

export interface SkillPackageInfo {
    name: string;
    title: string;
    title_en: string;
    description: string;
    description_en: string;
    category: string;
    aliases: string[];
    params: SkillParamInfo[];
    instructions: string;
    output_template: string;
    handler: string;
    workflow: Record<string, unknown>;
    source: string;
}

export interface SkillRunResult {
    status?: string;
    summary?: string;
    data?: Record<string, unknown>;
    error?: string;
    errors?: string[];
}

// issue #170: Extensions console — Skills tab (JSON 用户包) + Knowledge tab (fusion-rag)。
export interface ExtensionSkill {
    name: string;
    description: string;
    type: string;
    category: string;
    capability: string;
    cap_status: string;
}

export interface ExtensionSkillInstallResult {
    installed: boolean;
    name?: string;
    capability?: string;
    problems?: string[];
}

export interface ExtensionSkillTestResult {
    ok: boolean;
    result?: unknown;
    duration_ms?: number;
    error?: string;
}

export interface RagHealth {
    ok: boolean;
    version?: string;
    status?: string;
    error?: string;
}

export interface RagKbRow {
    kb_id: string;
    name: string;
    description: string;
    document_count?: number | null;
    capability: string | null;
    cap_status: string | null;
}

export interface RagKbList {
    bases: RagKbRow[];
    total: number;
    page: number;
    page_size: number;
    error?: string;
}

export interface RagDocument {
    file_name?: string;
    doc_path?: string;
    name?: string;
    chunk_count?: number;
    chunks?: number;
    char_count?: number;
}

export interface RagHit {
    text: string;
    doc_path: string;
    score?: number;
}

export interface BrowserStatus {
    running: boolean;
    port?: number;
    profile_dir?: string;
    pid?: number;
    url?: string;
    headless?: boolean;
    cookies_imported?: number;
}

export interface FusionBrowserStatus {
    running: boolean;
    pid?: number | null;
    socket_path?: string | null;
    binary?: string | null;
    mode?: string;
    sessions?: Record<string, { session_id: string; run_id?: string; profile_id?: string }>;
    profiles?: Record<string, string>;
    allowed_origins?: string[];
}

async function authHeaders(json = false): Promise<Record<string, string>> {
    const headers: Record<string, string> = {};
    if (json) headers['content-type'] = 'application/json';
    const key = runtimeApiKey();
    if (key) headers['x-api-key'] = key;
    return headers;
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
    startRun: (body: { workflow?: unknown; workflow_id?: string; name?: string; memory?: boolean }) =>
        post<{ run_id: string; status: string }>('/api/runs', body),
    runs: () => get<{ runs: RunListItem[]; count: number }>('/api/runs'),
    run: (id: string) => get<RunRecord>(`/api/runs/${encodeURIComponent(id)}`),
    runArtifacts: (id: string) =>
        get<{ artifacts: RunArtifact[]; count: number; review: RunReview | null }>(
            `/api/runs/${encodeURIComponent(id)}/artifacts`
        ),
    reviewRun: (id: string, approved: boolean, note = '') =>
        post<{ run_id: string; review: RunReview; workflow?: unknown }>(`/api/runs/${encodeURIComponent(id)}/review`, {
            approved,
            note,
        }),
    cancelRun: (id: string) => post<{ status: string }>(`/api/runs/${encodeURIComponent(id)}/cancel`),
    pauseRun: (id: string) => post<{ status: string }>(`/api/runs/${encodeURIComponent(id)}/pause`),
    resumeRun: (id: string) => post<{ status: string }>(`/api/runs/${encodeURIComponent(id)}/resume`),
    directive: (
        id: string,
        body: {
            action: 'set_params' | 'insert_node' | 'skip_remaining';
            node_id?: string;
            params?: Record<string, unknown>;
            node?: Record<string, unknown>;
            after_node_id?: string;
            input?: Record<string, unknown>;
        }
    ) =>
        post<{ status: string; run_id: string; action: string }>(`/api/runs/${encodeURIComponent(id)}/directive`, body),
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
    generateWorkflow: (body: { prompt: string; model?: string; context?: Record<string, unknown> }) =>
        post<{ workflow: { name?: string; description?: string; nodes: CanvasNodeDef[]; edges: CanvasEdgeDef[] } }>(
            '/api/ai/generate-workflow',
            body
        ),
    // issue #148: chat-first 委托 — 简单任务直接执行 (auto), 复杂任务返回计划待批准 (plan)
    chatDelegate: (body: { prompt: string; model?: string; memory?: boolean }) =>
        post<ChatDelegateResult>('/api/ai/chat-delegate', body),
    // issue #150: 引用溯源 — 读取引用文件内容 (沙箱范围内)
    readFile: (path: string) => get<FileReadResult>(`/api/files/read?path=${encodeURIComponent(path)}`),
    // issue #149: 产物就地 AI 修订
    spaceAiRevise: (id: string, aid: string, body: { selection: string; instruction: string; model?: string }) =>
        post<{ selection: string; revised: string; diff_blocks: DiffBlock[]; model: string }>(
            `/api/space/spaces/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(aid)}/ai_revise`,
            body
        ),
    spaceApplyRevision: (
        id: string,
        aid: string,
        body: { selection: string; revised: string; model?: string; accepted_blocks?: number[] }
    ) =>
        post<{ id: string; applied: boolean; revision_count: number }>(
            `/api/space/spaces/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(aid)}/apply_revision`,
            body
        ),
    sessions: (status = '', source = '') =>
        get<{ sessions: SessionInfo[]; count: number }>(
            `/api/sessions?status=${encodeURIComponent(status)}&source=${encodeURIComponent(source)}`
        ),
    session: (id: string) => get<SessionInfo>(`/api/sessions/${encodeURIComponent(id)}`),
    // space surface (mounted sub-app /api/space)
    spaces: () => get<SpaceInfo[]>('/api/space/spaces'),
    createSpace: (body: { name: string; description?: string }) => post<SpaceInfo>('/api/space/spaces', body),
    deleteSpace: (id: string) => sdk.http.delete(`/api/space/spaces/${encodeURIComponent(id)}`),
    spaceMembers: (id: string) => get<SpaceMemberInfo[]>(`/api/space/spaces/${encodeURIComponent(id)}/members`),
    spaceMessages: (id: string) => get<SpaceMessage[]>(`/api/space/spaces/${encodeURIComponent(id)}/messages`),
    sendSpaceMessage: (id: string, content: string, model = '') =>
        post<SpaceMessage>(`/api/space/spaces/${encodeURIComponent(id)}/messages`, {
            user_id: 'local_user',
            content,
            model: model || undefined,
        }),
    spaceArtifacts: (id: string) => get<SpaceArtifact[]>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts`),
    spaceArtifact: (id: string, aid: string) =>
        get<SpaceArtifact>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(aid)}`),
    createSpaceArtifact: (id: string, body: { name: string; artifact_type?: string; content?: string }) =>
        post<SpaceArtifact>(`/api/space/spaces/${encodeURIComponent(id)}/artifacts`, body),
    reviewArtifact: (id: string, aid: string, approved: boolean, note = '') =>
        post<{ artifact_id: string; review: ArtifactReview }>(
            `/api/space/spaces/${encodeURIComponent(id)}/artifacts/${encodeURIComponent(aid)}/review`,
            { approved, note }
        ),
    usage: () => get<UsageInfo>('/api/usage'),
    mobileOverview: () => get<MobileOverview>('/api/mobile/overview'),
    deliver: (runId: string, targetDeviceId = '') =>
        post<{ delivered: boolean; run_id: string; receiver: string }>('/api/deliver', {
            run_id: runId,
            target_device_id: targetDeviceId || undefined,
        }),
    updateSpaceMemberRole: (id: string, userId: string, role: string) =>
        sdk.http
            .put<{ user_id: string; role: string }>(
                `/api/space/spaces/${encodeURIComponent(id)}/members/${encodeURIComponent(userId)}`,
                { role }
            )
            .then((r) => r.data),
    spacePermissions: (id: string) => get<PermissionMatrix>(`/api/space/spaces/${encodeURIComponent(id)}/permissions`),
    spaceNotifications: (unreadOnly = false) =>
        get<SpaceNotification[]>(`/api/space/notifications?unread_only=${unreadOnly}`),
    markSpaceNotificationRead: (nid: string) =>
        post<{ marked: boolean }>(`/api/space/notifications/${encodeURIComponent(nid)}/read`),
    kbSearch: (id: string, q: string, topK = 5) =>
        get<KbSearchResult[]>(
            `/api/space/spaces/${encodeURIComponent(id)}/kb/search?q=${encodeURIComponent(q)}&top_k=${topK}`
        ),
    kbStatus: (id: string) => get<KbStatusInfo>(`/api/space/spaces/${encodeURIComponent(id)}/kb/status`),
    kbDocuments: (id: string) => get<KbDocumentInfo[]>(`/api/space/spaces/${encodeURIComponent(id)}/kb/documents`),
    kbBind: (id: string) =>
        post<{ kb_id: string }>(`/api/space/spaces/${encodeURIComponent(id)}/kb/bind`, { operator_id: 'local_user' }),
    kbUnbind: (id: string) =>
        post<{ unbound: boolean }>(`/api/space/spaces/${encodeURIComponent(id)}/kb/unbind`, {
            operator_id: 'local_user',
        }),
    models: () => get<{ models: ModelInfo[]; count: number; total?: number; stale?: boolean }>('/api/models'),
    pendingApprovals: () => get<{ approvals: GuardApproval[] }>('/api/permissions/pending'),
    confirmApproval: (actionId: string, approved: boolean, scope?: string, acceptedBlocks?: number[]) =>
        post<{
            action_id: string;
            approved: boolean;
            confirmed: boolean;
            reason?: string;
            edited_params?: Record<string, unknown>;
        }>(`/api/permissions/pending/${encodeURIComponent(actionId)}/confirm`, {
            approved,
            ...(scope ? { scope } : {}),
            ...(acceptedBlocks ? { accepted_blocks: acceptedBlocks } : {}),
        }),
    spaceInstructions: (spaceId: string) =>
        get<{ space_id: string; instructions: string }>(
            `/api/space/spaces/${encodeURIComponent(spaceId)}/instructions`
        ),
    saveSpaceInstructions: (spaceId: string, instructions: string) =>
        put<{ space_id: string; instructions: string }>(
            `/api/space/spaces/${encodeURIComponent(spaceId)}/instructions`,
            { instructions }
        ),
    clusterState: () => get<ClusterStateInfo>('/api/cluster/state'),
    skillPackages: () => get<{ skills: SkillPackageInfo[]; count: number }>('/api/skills'),
    skillPackage: (name: string) => get<SkillPackageInfo>(`/api/skills/${encodeURIComponent(name)}`),
    runSkillPackage: async (name: string, params: Record<string, unknown>): Promise<SkillRunResult> => {
        const res = await fetch(`${apiBase()}/api/skills/${encodeURIComponent(name)}/run`, {
            method: 'POST',
            headers: await authHeaders(true),
            body: JSON.stringify({ params }),
        });
        const body = (await res.json().catch(() => ({}))) as SkillRunResult;
        if (!res.ok && !body.error) body.error = `run failed: ${res.status}`;
        return body;
    },
    browserStatus: () => get<BrowserStatus>('/api/browser/status'),
    // issue #170: Extensions console
    extensionSkills: () => get<{ skills: ExtensionSkill[]; count: number }>('/api/extensions/skills'),
    installExtensionSkill: (doc: Record<string, unknown>) =>
        post<ExtensionSkillInstallResult>('/api/extensions/skills', doc),
    testExtensionSkill: async (name: string, args: string): Promise<ExtensionSkillTestResult> => {
        const res = await fetch(`${apiBase()}/api/extensions/skills/${encodeURIComponent(name)}/test`, {
            method: 'POST',
            headers: await authHeaders(true),
            body: JSON.stringify({ args }),
        });
        const body = (await res.json().catch(() => ({}))) as ExtensionSkillTestResult;
        if (!res.ok && !body.error) body.error = `test failed: ${res.status}`;
        body.ok = res.ok && body.ok !== false;
        return body;
    },
    removeExtensionSkill: (name: string) =>
        del<{ deleted: boolean; capability_removed: boolean }>(`/api/extensions/skills/${encodeURIComponent(name)}`),
    ragHealth: () => get<RagHealth>('/api/extensions/rag/health'),
    ragKbs: (q: string, page: number, pageSize: number) =>
        get<RagKbList>(`/api/extensions/rag/kb?q=${encodeURIComponent(q)}&page=${page}&page_size=${pageSize}`),
    createRagKb: (name: string, description: string, wire: boolean) =>
        post<{ kb_id: string; capability: string | null; detail?: string }>('/api/extensions/rag/kb', {
            name,
            description,
            wire,
        }),
    wireRagKb: (kbId: string, wired: boolean) =>
        post<{ wired: boolean; capability: string | null; capabilities_disabled: string[] }>(
            `/api/extensions/rag/kb/${encodeURIComponent(kbId)}/wire`,
            { wired }
        ),
    ragDocuments: async (kbId: string): Promise<{ documents: RagDocument[] }> => {
        const res = await fetch(`${apiBase()}/api/extensions/rag/kb/${encodeURIComponent(kbId)}/documents`, {
            headers: await authHeaders(),
        });
        const body = (await res.json().catch(() => ({}))) as { documents?: RagDocument[]; detail?: string };
        if (!res.ok) throw new Error(body.detail ?? `documents failed: ${res.status}`);
        return { documents: body.documents ?? [] };
    },
    uploadRagDocument: async (kbId: string, file: File): Promise<{ chunks?: number | null }> => {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch(`${apiBase()}/api/extensions/rag/kb/${encodeURIComponent(kbId)}/documents`, {
            method: 'POST',
            headers: await authHeaders(),
            body: fd,
        });
        const body = (await res.json().catch(() => ({}))) as { chunks?: number | null; detail?: string };
        if (!res.ok) throw new Error(body.detail ?? `upload failed: ${res.status}`);
        return body;
    },
    searchRagKb: async (kbId: string, query: string): Promise<RagHit[]> => {
        const res = await fetch(`${apiBase()}/api/extensions/rag/kb/${encodeURIComponent(kbId)}/search`, {
            method: 'POST',
            headers: await authHeaders(true),
            body: JSON.stringify({ query, top_k: 5 }),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body?.detail ?? `search failed: ${res.status}`);
        return Array.isArray(body) ? body : [];
    },
    browserLaunch: (body: { url?: string; headless?: boolean; import_cookies?: string }) =>
        post<BrowserStatus>('/api/browser/launch', body),
    browserClose: () => post<{ closed: boolean }>('/api/browser/close'),
    browserScreenshot: async (): Promise<string> => {
        const res = await fetch(`${apiBase()}/api/browser/screenshot`, { headers: await authHeaders() });
        if (!res.ok) throw new Error(`screenshot failed: ${res.status}`);
        const blob = await res.blob();
        return URL.createObjectURL(blob);
    },
    fusionBrowserStatus: () => get<FusionBrowserStatus>('/api/fusion-browser/status'),
    fusionBrowserLaunch: (body: { url?: string; allowed_origins?: string[] }) =>
        post<FusionBrowserStatus>('/api/fusion-browser/launch', body),
    fusionBrowserSession: (body: { run_id?: string; initial_url?: string; mode?: string }) =>
        post<{ session_id: string; run_id?: string; profile_id?: string; initial_url?: string }>(
            '/api/fusion-browser/session',
            body
        ),
    fusionBrowserImportCookies: (body: { session_id: string; browser: string; profile?: string }) =>
        post<{ browser: string; imported: number; skipped_app_bound: number; skipped_other: number }>(
            '/api/fusion-browser/import-cookies',
            body
        ),
    fusionBrowserFinish: (body: { run_id?: string; keep_profile?: boolean }) =>
        post<{ closed_sessions: string[]; deleted_profiles: string[]; engine_running: boolean }>(
            '/api/fusion-browser/finish',
            body
        ),
    fusionBrowserClose: () => post<FusionBrowserStatus>('/api/fusion-browser/close'),
    rpc: <T>(method: string, params?: Record<string, unknown>) =>
        post<RpcResponse<T>>('/rpc', { jsonrpc: '2.0', id: Date.now(), method, params: params ?? {} }).then((resp) => {
            if (resp.error) throw new Error(resp.error.message || `rpc error ${resp.error.code}`);
            return resp.result as T;
        }),
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
        case 'paused':
            return 'warning';
        case 'failed':
            return 'error';
        case 'cancelled':
            return 'offline';
        default:
            return 'warning';
    }
}
