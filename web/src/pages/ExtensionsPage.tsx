import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import {
    type ExtensionSkill,
    type RagDocument,
    type RagHit,
    type RagKbList,
    api,
} from '../api';
import { useT } from '../i18n';

// issue #170: Extensions 控制台 — f-ecommerce extensions.html E2 迁移 (Skills +
// Knowledge 两个页签)。布局/交互对齐源页: 页签栏 + 表格 + 安装面板 + 诊断行 +
// 测试面板; 数据源端点换成本模块 /api/extensions/*。

const SKILL_SAMPLE = JSON.stringify(
    {
        name: 'weekly-report',
        description: 'Weekly sales summary',
        type: 'workflow',
        category: 'reporting',
        template_id: 'weekly-sales',
    },
    null,
    2
);

export function ExtensionsPage() {
    const t = useT();
    const [tab, setTab] = useState<'skills' | 'knowledge'>('skills');
    const skills = useAutoRetry(() => api.extensionSkills(), []);
    const rag = useAutoRetry(() => api.ragHealth(), []);

    const [skillDoc, setSkillDoc] = useState(SKILL_SAMPLE);
    const [skillDiag, setSkillDiag] = useState<{ cls: 'ok' | 'err'; msg: string } | null>(null);
    const [testSkill, setTestSkill] = useState<string | null>(null);
    const [testArgs, setTestArgs] = useState('');
    const [testOut, setTestOut] = useState('');
    const [testErr, setTestErr] = useState(false);
    const [testing, setTesting] = useState(false);
    const [pageMsg, setPageMsg] = useState('');

    // Knowledge tab 状态
    const [kbHealth, setKbHealth] = useState('');
    const [kbName, setKbName] = useState('');
    const [kbDesc, setKbDesc] = useState('');
    const [kbFilter, setKbFilter] = useState('');
    const [kbDiag, setKbDiag] = useState<{ cls: 'ok' | 'err'; msg: string } | null>(null);
    const [kbList, setKbList] = useState<RagKbList | null>(null);
    const [kbPage, setKbPage] = useState(1);
    const [kbOpen, setKbOpen] = useState<string | null>(null);
    const [kbDocs, setKbDocs] = useState<RagDocument[]>([]);
    const [kbUpDiag, setUpDiag] = useState<{ cls: 'ok' | 'err'; msg: string } | null>(null);
    const [kbFile, setKbFile] = useState<File | null>(null);
    const [kbQuery, setKbQuery] = useState('');
    const [kbHits, setKbHits] = useState('');
    const [kbErr, setKbErr] = useState(false);

    useEffect(() => {
        if (rag.data) {
            setKbHealth(
                rag.data.ok
                    ? t('知识库服务已连接', 'knowledge service connected') + (rag.data.version ? ` (fusion-rag ${rag.data.version})` : '')
                    : t('知识库服务未连接', 'knowledge service down') + (rag.data.error ? ` — ${rag.data.error}` : '')
            );
        }
    }, [rag.data, t]);

    async function loadKbs(q: string, page: number) {
        try {
            const data = await api.ragKbs(q, page, 10);
            if (data.error) {
                setKbList({ bases: [], total: 0, page: 1, page_size: 10, error: data.error });
                return;
            }
            setKbList(data);
        } catch (e) {
            setPageMsg(e instanceof Error ? e.message : String(e));
        }
    }

    useEffect(() => {
        if (tab === 'knowledge') {
            void rag.manualRetry();
            void loadKbs('', 1);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [tab]);

    useEffect(() => {
        const timer = setTimeout(() => {
            if (tab === 'knowledge') void loadKbs(kbFilter.trim(), 1);
            setKbPage(1);
        }, 300);
        return () => clearTimeout(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [kbFilter]);

    async function installSkill() {
        try {
            const doc = JSON.parse(skillDoc) as Record<string, unknown>;
            const out = await api.installExtensionSkill(doc);
            if (!out.installed) {
                setSkillDiag({ cls: 'err', msg: '✗ ' + (out.problems ?? []).join(' | ') });
                return;
            }
            setSkillDiag({ cls: 'ok', msg: `✓ installed — capability ${out.capability} auto-listed` });
            void skills.manualRetry();
        } catch (e) {
            setSkillDiag({ cls: 'err', msg: '✗ ' + (e instanceof Error ? e.message : String(e)) });
        }
    }

    async function runSkillTest() {
        if (!testSkill) return;
        setTesting(true);
        setTestOut('');
        try {
            const r = await api.testExtensionSkill(testSkill, testArgs);
            setTestErr(!r.ok);
            setTestOut(JSON.stringify(r.result ?? r, null, 2).slice(0, 2000));
        } catch (e) {
            setTestErr(true);
            setTestOut(e instanceof Error ? e.message : String(e));
        } finally {
            setTesting(false);
        }
    }

    async function removeSkill(name: string) {
        if (!window.confirm(t(`移除技能 "${name}"? 其能力同时注销。`, `Remove skill "${name}"? Its capability is removed too.`))) return;
        try {
            await api.removeExtensionSkill(name);
            void skills.manualRetry();
        } catch (e) {
            setPageMsg(e instanceof Error ? e.message : String(e));
        }
    }

    async function createKb() {
        try {
            const out = await api.createRagKb(kbName.trim(), kbDesc.trim(), true);
            setKbDiag({ cls: 'ok', msg: `✓ created — capability ${out.capability ?? '(unwired)'}` });
            setKbName('');
            setKbDesc('');
            void loadKbs(kbFilter.trim(), 1);
        } catch (e) {
            setKbDiag({ cls: 'err', msg: '✗ ' + (e instanceof Error ? e.message : String(e)) });
        }
    }

    async function openKb(kbId: string) {
        setKbOpen(kbId);
        setKbDocs([]);
        try {
            const docs = await api.ragDocuments(kbId);
            setKbDocs(docs.documents);
        } catch (e) {
            setPageMsg(e instanceof Error ? e.message : String(e));
        }
    }

    async function wireKb(kbId: string, wired: boolean) {
        try {
            const out = await api.wireRagKb(kbId, wired);
            setPageMsg(
                out.wired
                    ? t(`已接线 — ${out.capability} 现在是 agent 工具。`, `Wired — ${out.capability} is now an agent tool.`)
                    : t(`已断开 — ${out.capabilities_disabled.join(', ')} 停用。`, `Unwired — ${out.capabilities_disabled.join(', ')} disabled.`)
            );
            void loadKbs(kbFilter.trim(), kbPage);
        } catch (e) {
            setPageMsg(e instanceof Error ? e.message : String(e));
        }
    }

    async function uploadDoc() {
        if (!kbOpen) return;
        if (!kbFile) {
            setUpDiag({ cls: 'err', msg: '✗ ' + t('请先选择文件', 'pick a file first') });
            return;
        }
        setUpDiag({ cls: 'ok', msg: t('摄取中…', 'ingesting…') });
        try {
            const out = await api.uploadRagDocument(kbOpen, kbFile);
            setUpDiag({ cls: 'ok', msg: `✓ ingested — ${out.chunks ?? '?'} chunks indexed` });
            void openKb(kbOpen);
            void loadKbs(kbFilter.trim(), kbPage);
        } catch (e) {
            setUpDiag({ cls: 'err', msg: '✗ ' + (e instanceof Error ? e.message : String(e)) });
        }
    }

    async function searchKb() {
        if (!kbOpen) return;
        setKbErr(false);
        setKbHits(t('检索中…', 'searching…'));
        try {
            const hits: RagHit[] = await api.searchRagKb(kbOpen, kbQuery.trim());
            setKbHits(
                (hits.length ? hits : [{ text: '(no hits)', doc_path: '' }])
                    .slice(0, 5)
                    .map((h, i) => `${i + 1}. ${h.doc_path || '?'}  score ${h.score ?? '—'}\n   ${String(h.text || '').slice(0, 160)}`)
                    .join('\n')
            );
        } catch (e) {
            setKbErr(true);
            setKbHits(e instanceof Error ? e.message : String(e));
        }
    }

    if (skills.loading && !skills.data) return <SkeletonLines rows={6} />;
    if (skills.error && !skills.data)
        return <ErrorState message={skills.error?.message ?? 'no data'} onRetry={skills.manualRetry} />;
    const skillRows: ExtensionSkill[] = skills.data?.skills ?? [];
    const kbPages = kbList ? Math.max(1, Math.ceil(kbList.total / kbList.page_size)) : 1;

    return (
        <div className="mx-auto max-w-4xl space-y-3">
            <div className="text-sm font-medium text-text-primary">{t('扩展', 'Extensions')}</div>
            <p className="text-xs text-text-muted">
                {t(
                    '安装一次技能与知识库 — 之后每个 agent 的工具清单自动可见。',
                    'Install skills and knowledge once — they surface in every agent\'s tool checklist automatically.'
                )}
            </p>

            <div className="rounded-lg border border-border bg-bg-elevated p-3 text-xs">
                <b className="text-text-primary">{t('各部分如何连接', 'How things connect')}</b>
                <div className="mt-1 font-mono text-[10px] text-text-secondary">
                    {t(
                        'Skills 安装即注册 → SkillRegistry (MCP skill_run 可调) · Knowledge 接线 → knowledge.<kb>.search 能力',
                        'Skills install-registered → SkillRegistry (callable via MCP skill_run) · Knowledge wired → knowledge.<kb>.search capability'
                    )}
                </div>
                <div className="mt-2 flex flex-wrap gap-4">
                    <span className="font-semibold">{skillRows.length} {t('技能', 'skills')}</span>
                    <span>{kbList ? `${kbList.total} KB` : '—'}</span>
                </div>
            </div>

            <div className="flex gap-1">
                <button
                    type="button"
                    className={`rounded px-4 py-1.5 text-xs ${tab === 'skills' ? 'bg-brand text-white' : 'border border-border text-text-secondary hover:bg-bg-muted'}`}
                    onClick={() => setTab('skills')}
                >
                    {t('技能', 'Skills')}
                </button>
                <button
                    type="button"
                    className={`rounded px-4 py-1.5 text-xs ${tab === 'knowledge' ? 'bg-brand text-white' : 'border border-border text-text-secondary hover:bg-bg-muted'}`}
                    onClick={() => setTab('knowledge')}
                >
                    {t('知识库', 'Knowledge')}
                </button>
            </div>

            {tab === 'skills' && (
                <div className="space-y-3">
                    <table className="w-full text-left text-xs">
                        <thead>
                            <tr className="border-b border-border text-text-secondary">
                                <th className="py-1.5 pr-2 font-medium">{t('名称', 'Name')}</th>
                                <th className="py-1.5 pr-2 font-medium">{t('类型', 'Type')}</th>
                                <th className="py-1.5 pr-2 font-medium">{t('描述', 'Description')}</th>
                                <th className="py-1.5 pr-2 font-medium">{t('能力', 'Capability')}</th>
                                <th className="py-1.5 font-medium">{t('操作', 'Actions')}</th>
                            </tr>
                        </thead>
                        <tbody>
                            {skillRows.length === 0 ? (
                                <tr>
                                    <td colSpan={5} className="py-2 text-text-muted">
                                        {t('暂无 skill 包。', 'No skill packs installed.')}
                                    </td>
                                </tr>
                            ) : (
                                skillRows.map((s) => (
                                    <tr key={s.name} className="border-b border-border">
                                        <td className="py-1.5 pr-2 font-mono text-text-primary">{s.name}</td>
                                        <td className="py-1.5 pr-2">
                                            <span
                                                className={`rounded-full px-2 py-0.5 text-[10px] ${s.type === 'script' ? 'bg-bg-muted text-warning' : 'bg-bg-muted text-success'}`}
                                            >
                                                {s.type}
                                            </span>
                                        </td>
                                        <td className="py-1.5 pr-2 text-text-muted">{s.description}</td>
                                        <td className="py-1.5 pr-2 font-mono text-text-muted">
                                            {s.capability}
                                            {s.cap_status === 'missing' && (
                                                <span className="ml-1 rounded-full bg-bg-muted px-2 py-0.5 text-[10px] text-text-muted">
                                                    {t('未注册', 'missing')}
                                                </span>
                                            )}
                                        </td>
                                        <td className="py-1.5">
                                            <button type="button" className="text-text-secondary hover:underline" onClick={() => { setTestSkill(s.name); setTestArgs(''); setTestOut(''); setTestErr(false); }}>
                                                {t('测试', 'Test')}
                                            </button>
                                            <button type="button" className="ml-2 text-text-secondary hover:underline" onClick={() => void removeSkill(s.name)}>
                                                {t('移除', 'Remove')}
                                            </button>
                                        </td>
                                    </tr>
                                ))
                            )}
                        </tbody>
                    </table>

                    <div className="rounded-lg border border-border bg-bg-elevated p-3">
                        <b className="text-xs text-text-primary">{t('安装 skill 包', 'Install a skill pack')}</b>
                        <p className="mt-1 text-[10px] text-text-muted">
                            {t(
                                'cowork 包格式: name / description / type (workflow|node|script) / template_id 或 node。脚本包仅管理员 (任意代码)。',
                                'cowork pack format: name / description / type (workflow|node|script) / template_id or node. Script packs are admin-only (arbitrary code).'
                            )}
                        </p>
                        <textarea
                            aria-label="skill-doc"
                            spellCheck={false}
                            className="mt-2 min-h-52 w-full rounded-md border border-border bg-bg-elevated p-2 font-mono text-xs text-text-primary"
                            value={skillDoc}
                            onChange={(e) => setSkillDoc(e.target.value)}
                        />
                        <div className="mt-2 flex items-center gap-3">
                            <button type="button" className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90" onClick={() => void installSkill()}>
                                {t('校验并安装', 'Validate & install')}
                            </button>
                            <span className="text-[10px] text-text-muted">
                                {t('安装后能力自动列出 → 每个 agent 的工具清单', 'installs → capability auto-listed → every agent\'s tool checklist')}
                            </span>
                        </div>
                        {skillDiag && (
                            <p className={`mt-2 whitespace-pre-wrap font-mono text-[10px] ${skillDiag.cls === 'ok' ? 'text-success' : 'text-error'}`}>
                                {skillDiag.msg}
                            </p>
                        )}
                    </div>

                    {testSkill && (
                        <div className="rounded-lg border border-border bg-bg-elevated p-3">
                            <b className="text-xs text-text-primary">
                                {t('测试: ', 'Test: ')}
                                <span className="font-mono">{testSkill}</span>
                            </b>
                            <div className="mt-2 flex gap-2">
                                <input
                                    aria-label="skill-test-args"
                                    className="flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                                    placeholder={t('args (JSON 或文本, handler 契约)', 'args (JSON or text, handler contract)')}
                                    value={testArgs}
                                    onChange={(e) => setTestArgs(e.target.value)}
                                />
                                <button type="button" className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted" disabled={testing} onClick={() => void runSkillTest()}>
                                    {t('运行', 'Run')}
                                </button>
                                <button type="button" className="text-xs text-text-secondary hover:underline" onClick={() => setTestSkill(null)}>
                                    {t('关闭', 'close')}
                                </button>
                            </div>
                            {testOut && (
                                <p className={`mt-2 whitespace-pre-wrap font-mono text-[10px] ${testErr ? 'text-error' : 'text-success'}`}>{testOut}</p>
                            )}
                        </div>
                    )}
                </div>
            )}

            {tab === 'knowledge' && (
                <div className="space-y-3">
                    <div className="rounded-lg border border-border bg-bg-elevated p-3">
                        <b className="text-xs text-text-primary">{t('知识库', 'Knowledge bases')}</b>
                        <p className={`mt-1 text-xs ${rag.data?.ok ? 'text-success' : 'text-error'}`}>
                            {rag.data?.ok ? '● ' : '● '}
                            {kbHealth}
                        </p>
                        <p className="mt-1 text-[10px] text-text-muted">
                            {t(
                                '文档存这里, agent 运行时取回原文并引用来源 — 事实常新, 风格交给模型。检索经 SpaceKB 同款 fusion-rag 绑定 (:11436)。',
                                'Feed documents here; agents retrieve the original text at run time and cite its source. Retrieval flows through the SpaceKB fusion-rag binding (:11436).'
                            )}
                        </p>
                        <div className="mt-2 flex gap-2">
                            <input
                                aria-label="kb-name"
                                className="flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                                placeholder={t('新知识库名 (如 product-docs)', 'new KB name (e.g. product-docs)')}
                                value={kbName}
                                onChange={(e) => setKbName(e.target.value)}
                            />
                            <input
                                aria-label="kb-desc"
                                className="flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                                placeholder={t('描述 (可选)', 'description (optional)')}
                                value={kbDesc}
                                onChange={(e) => setKbDesc(e.target.value)}
                            />
                            <button type="button" className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90" onClick={() => void createKb()}>
                                {t('创建并接线', 'Create & wire')}
                            </button>
                        </div>
                        {kbDiag && (
                            <p className={`mt-2 font-mono text-[10px] ${kbDiag.cls === 'ok' ? 'text-success' : 'text-error'}`}>{kbDiag.msg}</p>
                        )}
                        <input
                            aria-label="kb-filter"
                            className="mt-2 w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                            placeholder={t('按名称 / 描述过滤…', 'filter by name / description…')}
                            value={kbFilter}
                            onChange={(e) => setKbFilter(e.target.value)}
                        />
                        <table className="mt-2 w-full text-left text-xs">
                            <thead>
                                <tr className="border-b border-border text-text-secondary">
                                    <th className="py-1.5 pr-2 font-medium">KB</th>
                                    <th className="py-1.5 pr-2 font-medium">{t('文档', 'Docs')}</th>
                                    <th className="py-1.5 pr-2 font-medium">{t('能力', 'Capability')}</th>
                                    <th className="py-1.5 font-medium">{t('操作', 'Actions')}</th>
                                </tr>
                            </thead>
                            <tbody>
                                {!kbList ? (
                                    <tr>
                                        <td colSpan={4} className="py-2 text-text-muted">…</td>
                                    </tr>
                                ) : kbList.error ? (
                                    <tr>
                                        <td colSpan={4} className="py-2 text-text-muted">{kbList.error}</td>
                                    </tr>
                                ) : kbList.bases.length === 0 ? (
                                    <tr>
                                        <td colSpan={4} className="py-2 text-text-muted">
                                            {kbFilter ? t('无匹配知识库。', 'No knowledge bases match.') : t('尚无知识库 — 在上方创建。', 'No knowledge bases — create one above.')}
                                        </td>
                                    </tr>
                                ) : (
                                    kbList.bases.map((b) => (
                                        <tr key={b.kb_id} className="border-b border-border">
                                            <td className="py-1.5 pr-2">
                                                <b className="font-mono text-text-primary">{b.kb_id}</b>
                                                <div className="text-[10px] text-text-muted">{b.description}</div>
                                            </td>
                                            <td className="py-1.5 pr-2">{b.document_count ?? '—'}</td>
                                            <td className="py-1.5 pr-2 font-mono text-text-muted">
                                                {b.capability ? (
                                                    <>
                                                        {b.capability}{' '}
                                                        <span className="rounded-full bg-bg-muted px-2 py-0.5 text-[10px] text-success">on</span>
                                                    </>
                                                ) : (
                                                    <span className="rounded-full bg-bg-muted px-2 py-0.5 text-[10px] text-text-muted">
                                                        {t('未接线', 'not wired')}
                                                    </span>
                                                )}
                                            </td>
                                            <td className="py-1.5">
                                                <button type="button" className="text-text-secondary hover:underline" onClick={() => void openKb(b.kb_id)}>
                                                    {t('打开', 'Open')}
                                                </button>
                                                <button type="button" className="ml-2 text-text-secondary hover:underline" onClick={() => void wireKb(b.kb_id, !b.capability)}>
                                                    {b.capability ? t('断开', 'Unwire') : t('接线', 'Wire')}
                                                </button>
                                            </td>
                                        </tr>
                                    ))
                                )}
                            </tbody>
                        </table>
                        {kbList && (
                            <p className="mt-1 text-[10px] text-text-muted">
                                {kbList.total} KB — {t('页', 'page')} {kbList.page} / {kbPages}
                                {kbList.page > 1 && (
                                    <button type="button" className="ml-1 text-text-secondary hover:underline" onClick={() => { const p = kbPage - 1; setKbPage(p); void loadKbs(kbFilter.trim(), p); }}>
                                        ‹ {t('上一页', 'prev')}
                                    </button>
                                )}
                                {kbList.page < kbPages && (
                                    <button type="button" className="ml-1 text-text-secondary hover:underline" onClick={() => { const p = kbPage + 1; setKbPage(p); void loadKbs(kbFilter.trim(), p); }}>
                                        {t('下一页', 'next')} ›
                                    </button>
                                )}
                            </p>
                        )}
                    </div>

                    {kbOpen && (
                        <div className="rounded-lg border border-border bg-bg-elevated p-3">
                            <b className="text-xs text-text-primary">
                                KB: <span className="font-mono">{kbOpen}</span>
                            </b>
                            <div className="mt-2 flex items-center gap-2">
                                <input
                                    aria-label="kb-file"
                                    type="file"
                                    accept=".md,.txt"
                                    className="flex-1 text-xs text-text-secondary"
                                    onChange={(e) => setKbFile(e.target.files?.[0] ?? null)}
                                />
                                <button type="button" className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90" onClick={() => void uploadDoc()}>
                                    {t('上传并摄取', 'Upload & ingest')}
                                </button>
                            </div>
                            {kbUpDiag && (
                                <p className={`mt-1 font-mono text-[10px] ${kbUpDiag.cls === 'ok' ? 'text-success' : 'text-error'}`}>{kbUpDiag.msg}</p>
                            )}
                            <table className="mt-2 w-full text-left text-xs">
                                <thead>
                                    <tr className="border-b border-border text-text-secondary">
                                        <th className="py-1.5 pr-2 font-medium">{t('文档', 'Document')}</th>
                                        <th className="py-1.5 pr-2 font-medium">{t('分块', 'Chunks')}</th>
                                        <th className="py-1.5 font-medium">{t('大小', 'Size')}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {kbDocs.length === 0 ? (
                                        <tr>
                                            <td colSpan={3} className="py-2 text-text-muted">
                                                {t('暂无文档 — 在上方上传。', 'No documents — upload one above.')}
                                            </td>
                                        </tr>
                                    ) : (
                                        kbDocs.map((d, i) => (
                                            <tr key={i} className="border-b border-border">
                                                <td className="py-1.5 pr-2 font-mono text-text-primary">{d.file_name || d.doc_path || d.name || '?'}</td>
                                                <td className="py-1.5 pr-2">{d.chunk_count ?? d.chunks ?? '—'}</td>
                                                <td className="py-1.5 text-text-muted">{d.char_count != null ? `${d.char_count} chars` : '—'}</td>
                                            </tr>
                                        ))
                                    )}
                                </tbody>
                            </table>
                            <b className="mt-2 block text-xs text-text-primary">{t('试一次检索', 'Try a search')}</b>
                            <div className="mt-1 flex gap-2">
                                <input
                                    aria-label="kb-test-q"
                                    className="flex-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                                    placeholder={t('如: 这份文档讲了什么?', 'e.g. What does this document cover?')}
                                    value={kbQuery}
                                    onChange={(e) => setKbQuery(e.target.value)}
                                />
                                <button type="button" className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted" onClick={() => void searchKb()}>
                                    {t('检索', 'Search')}
                                </button>
                            </div>
                            {kbHits && (
                                <p className={`mt-2 whitespace-pre-wrap font-mono text-[10px] ${kbErr ? 'text-error' : 'text-success'}`}>{kbHits}</p>
                            )}
                        </div>
                    )}
                </div>
            )}

            {pageMsg && <p className="text-xs text-text-muted">{pageMsg}</p>}
            {skillRows.length === 0 && tab === 'skills' && !skills.loading && <EmptyState title={t('暂无扩展', 'No extensions')} />}
        </div>
    );
}
