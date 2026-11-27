// issue #310: delivery pipeline dashboard data — 10-stage topology for the
// product_lifecycle template (#307), node states bound to real run steps,
// per-stage input/output artifact contract from the issue table.

export interface PipelineStage {
    id: number;
    labelZh: string;
    labelEn: string;
    // product_lifecycle node ids whose steps feed this stage
    nodeIds: string[];
    inputsZh: string[];
    inputsEn: string[];
    outputsZh: string[];
    outputsEn: string[];
}

export const PIPELINE_STAGES: PipelineStage[] = [
    {
        id: 1,
        // i18n: user-facing
        labelZh: '需求登记',
        labelEn: 'Requirement intake',
        nodeIds: [],
        // i18n: user-facing
        inputsZh: ['需求文本 / 文档上传'],
        inputsEn: ['Requirement text / document upload'],
        // i18n: user-facing
        outputsZh: ['需求登记记录 (run 输入快照)'],
        outputsEn: ['Requirement registration record (run input snapshot)'],
    },
    {
        id: 2,
        // i18n: user-facing
        labelZh: '可行性 + PRD',
        labelEn: 'Feasibility + PRD',
        nodeIds: ['n1'],
        // i18n: user-facing
        inputsZh: ['需求文本 + fusion-* 能力矩阵'],
        inputsEn: ['Requirement text + fusion-* capability matrix'],
        // i18n: user-facing
        outputsZh: ['可行性结论 + PRD v1'],
        outputsEn: ['Feasibility verdict + PRD v1'],
    },
    {
        id: 3,
        // i18n: user-facing
        labelZh: '闸门 A (PRD 评审)',
        labelEn: 'Gate A (PRD review)',
        nodeIds: ['n2'],
        // i18n: user-facing
        inputsZh: ['PRD + 评审清单'],
        inputsEn: ['PRD + review checklist'],
        // i18n: user-facing
        outputsZh: ['闸门 A 决定 + 结构化意见'],
        outputsEn: ['Gate A decision + structured opinions'],
    },
    {
        id: 4,
        // i18n: user-facing
        labelZh: '架构设计',
        labelEn: 'Architecture',
        nodeIds: ['n3'],
        // i18n: user-facing
        inputsZh: ['已批准 PRD'],
        inputsEn: ['Approved PRD'],
        // i18n: user-facing
        outputsZh: ['架构图 / 集成文档 / UI 原型 / 项目计划'],
        outputsEn: ['Architecture diagram / integration doc / UI prototype / project plan'],
    },
    {
        id: 5,
        // i18n: user-facing
        labelZh: '闸门 B (架构评审)',
        labelEn: 'Gate B (arch review)',
        nodeIds: ['n4'],
        // i18n: user-facing
        inputsZh: ['四项设计交付物 + UX 契约清单'],
        inputsEn: ['Four design deliverables + UX contract checklist'],
        // i18n: user-facing
        outputsZh: ['闸门 B 决定 + 结构化意见'],
        outputsEn: ['Gate B decision + structured opinions'],
    },
    {
        id: 6,
        // i18n: user-facing
        labelZh: '脚手架 + 任务分解',
        labelEn: 'Scaffold + decomposition',
        nodeIds: ['n5', 'n6'],
        // i18n: user-facing
        inputsZh: ['已批准架构 + 计划'],
        inputsEn: ['Approved architecture + plan'],
        // i18n: user-facing
        outputsZh: ['business/ai-* 脚手架 + 任务 issue (state:ready) + 派发记录'],
        outputsEn: ['business/ai-* scaffold + task issues (state:ready) + dispatch records'],
    },
    {
        id: 7,
        // i18n: user-facing
        labelZh: 'Coder 派发',
        labelEn: 'Coder dispatch',
        nodeIds: ['n7'],
        // i18n: user-facing
        inputsZh: ['任务 issue + 脚手架'],
        inputsEn: ['Task issue + scaffold'],
        // i18n: user-facing
        outputsZh: ['PR + findings[] + 自测证据'],
        outputsEn: ['PR + findings[] + self-test evidence'],
    },
    {
        id: 8,
        // i18n: user-facing
        labelZh: '机器验收',
        labelEn: 'Machine acceptance',
        nodeIds: ['n8'],
        // i18n: user-facing
        inputsZh: ['PR + CI + findings 汇总'],
        inputsEn: ['PR + CI + findings summary'],
        // i18n: user-facing
        outputsZh: ['机器验收报告 + 发布/回滚指令'],
        outputsEn: ['Machine acceptance report + release/rollback directive'],
    },
    {
        id: 9,
        labelZh: 'Mock + Staging',
        labelEn: 'Mock + staging',
        nodeIds: ['n9'],
        // i18n: user-facing
        inputsZh: ['发布指令 + PRD/架构'],
        inputsEn: ['Release directive + PRD/arch'],
        // i18n: user-facing
        outputsZh: ['mock 包 + staging 部署记录 + doctor 健康报告'],
        outputsEn: ['Mock pack + staging deploy record + doctor health report'],
    },
    {
        id: 10,
        // i18n: user-facing
        labelZh: '闸门 C (人工验收)',
        labelEn: 'Gate C (acceptance)',
        nodeIds: ['n10'],
        // i18n: user-facing
        inputsZh: ['staging + 完整证据表'],
        inputsEn: ['Staging + full evidence table'],
        // i18n: user-facing
        outputsZh: ['闸门 C 决定 (逐项部分批准) + tag/发布记录'],
        outputsEn: ['Gate C decision (per-task partial approval) + tag/release record'],
    },
];

// forward edges (topology) + rejection loops (red dashed)
export const PIPELINE_LOOPS: [number, number][] = [
    [3, 2], // gate A -> PRD
    [5, 4], // gate B -> architecture
    [8, 7], // leader -> coder
    [10, 9], // gate C -> mock/staging
    [7, 6], // self-test fail -> decomposition
];

export type StageState = 'done' | 'active' | 'off';

export interface StageArtifacts {
    inputs: string[];
    outputs: string[];
}

export function stageStateOf(stage: PipelineStage, steps: { node_id: string; status: string }[]): StageState {
    const statuses = (steps ?? []).filter((s) => stage.nodeIds.includes(s.node_id)).map((s) => String(s.status ?? ''));
    if (statuses.includes('success')) return 'done';
    if (statuses.length > 0) return 'active';
    return 'off';
}

// deep links extracted from step output_data (PR / issue / report refs)
export function stageLinks(output: Record<string, unknown> | null | undefined): string[] {
    if (!output) return [];
    const links: string[] = [];
    const inner = (output.result ?? {}) as Record<string, unknown>;
    for (const obj of [output, inner]) {
        const pr = String(obj.pr_url ?? '');
        if (pr) links.push(pr);
    }
    return links;
}

// issue #318: runs whose workflow is the product_lifecycle delivery pipeline get
// the purpose-built pipeline visualization — match by template name (bilingual)
export function isPipelineRun(workflowName: string): boolean {
    const n = workflowName ?? '';
    // i18n: user-facing
    return n.includes('产品生命周期') || n.includes('product_lifecycle') || n.includes('交付-产品');
}
