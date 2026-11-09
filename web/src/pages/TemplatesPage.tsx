import { EmptyState, ErrorState, SkeletonLines } from '@fusion/web-ui';
import { useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type TemplateInfo, api } from '../api';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// W5: template catalog (builtin + custom). "Open in canvas" fetches the
// template's workflow body and saves a copy as a user workflow, then jumps
// to the canvas. Data: GET /api/templates (new in this change).

export function TemplatesPage() {
    const t = useT();
    const [category, setCategory] = useState('');
    const [creating, setCreating] = useState('');
    const retry = useAutoRetry(() => api.templates(category), [category]);
    const [importing, setImporting] = useState(false);

    // issue #294: 一键启动 — params 表单按模板 schema 生成, 服务端替换 ${param.X} 后起 run
    const [launchTpl, setLaunchTpl] = useState<TemplateInfo | null>(null);
    const [launchProps, setLaunchProps] = useState<
        Record<string, { type: string; description?: string; default?: unknown }>
    >({});
    const [launchRequired, setLaunchRequired] = useState<string[]>([]);
    const [launchValues, setLaunchValues] = useState<Record<string, string>>({});
    const [launching, setLaunching] = useState(false);
    // issue #400: intake 模式 — 默认只填 requirement, slug/私有仓/登记全自动派生;
    // 显式参数表单收进"高级"通道
    const [intakeMode, setIntakeMode] = useState(true);
    const [intakeRequirement, setIntakeRequirement] = useState('');
    const [intakeSlugHint, setIntakeSlugHint] = useState('');
    // issue #413: gate B (架构评审) intake 二选一 — 缺省不评审 (自动通过)
    const [intakeReviewArch, setIntakeReviewArch] = useState(false);
    const [intakePreview, setIntakePreview] = useState<{
        slug: string;
        suggested_from: string;
        repo: string;
    } | null>(null);

    // issue #300: all hooks above the early returns — hooks declared after a
    // conditional return throw "Rendered more hooks than during the previous
    // render" (#310) when the first render takes the early-exit branch
    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error) return <ErrorState message={retry.error.message} onRetry={retry.manualRetry} />;

    const templates = retry.data?.templates ?? [];
    const categories = Array.from(new Set(templates.map((x) => x.category ?? ''))).filter(Boolean);

    async function openLaunch(tpl: TemplateInfo) {
        if (launching) return;
        setLaunchTpl(tpl);
        setLaunchValues({});
        setIntakeMode(true);
        setIntakeRequirement('');
        setIntakeSlugHint('');
        setIntakeReviewArch(false);
        setIntakePreview(null);
        try {
            const detail = (await api.template(tpl.id)) as unknown as {
                params?: {
                    properties?: Record<string, { type: string; description?: string; default?: unknown }>;
                    required?: string[];
                };
            };
            const props = detail.params?.properties ?? {};
            setLaunchProps(props);
            setLaunchRequired(detail.params?.required ?? []);
            const init: Record<string, string> = {};
            for (const [k, spec] of Object.entries(props)) {
                if (spec.default !== undefined) {
                    init[k] = Array.isArray(spec.default) ? (spec.default as string[]).join(',') : String(spec.default);
                }
            }
            setLaunchValues(init);
        } catch (e) {
            console.error('[cowork-web] template detail failed', e);
            setLaunchTpl(null);
            sdk.toast.show({ level: 'error', message: t('模板详情获取失败', 'Failed to load template') });
        }
    }

    async function previewIntake() {
        if (!launchTpl || launching) return;
        if (intakeRequirement.trim().length < 10) {
            setIntakePreview(null);
            return;
        }
        try {
            const res = await api.launchTemplateIntake(launchTpl.id, {
                requirement: intakeRequirement,
                slug_hint: intakeSlugHint || undefined,
                dry_run: true,
                review_arch: intakeReviewArch,
            });
            setIntakePreview({
                slug: res.plan.slug,
                suggested_from: res.plan.suggested_from,
                repo: res.plan.repo,
            });
        } catch (e) {
            console.error('[cowork-web] intake preview failed', e);
            setIntakePreview(null);
        }
    }

    async function submitLaunchIntake() {
        if (!launchTpl || launching) return;
        setLaunching(true);
        try {
            const res = await api.launchTemplateIntake(launchTpl.id, {
                requirement: intakeRequirement,
                slug_hint: intakeSlugHint || undefined,
                review_arch: intakeReviewArch,
            });
            if (res.status !== 'started' || !res.run_id) {
                throw new Error(res.status);
            }
            const conflict = res.plan.suggested_from
                ? t(`slug 冲突, 已改用 ${res.plan.slug}`, `slug conflict — using ${res.plan.slug}`)
                : '';
            sdk.toast.show({
                level: 'success',
                message: t(`已启动: ${res.run_id}`, `Launched: ${res.run_id}`) + (conflict ? ` (${conflict})` : ''),
            });
            setLaunchTpl(null);
            window.location.hash = `#/runs/${res.run_id}`;
        } catch (e) {
            console.error('[cowork-web] intake launch failed', e);
            sdk.toast.show({
                level: 'error',
                message: t(
                    '启动失败 — 需求文本/GitHub token/registry 检查服务端日志',
                    'Launch failed — see server logs'
                ),
            });
        } finally {
            setLaunching(false);
        }
    }

    async function submitLaunch() {
        if (!launchTpl || launching) return;
        const params: Record<string, unknown> = {};
        for (const [k, spec] of Object.entries(launchProps)) {
            const raw = (launchValues[k] ?? '').trim();
            if (spec.type === 'integer' || spec.type === 'number') {
                if (raw !== '') params[k] = Number(raw);
            } else if (spec.type === 'array') {
                params[k] =
                    raw === ''
                        ? []
                        : raw
                              .split(',')
                              .map((s) => s.trim())
                              .filter(Boolean);
            } else if (raw !== '') {
                params[k] = raw;
            }
        }
        setLaunching(true);
        try {
            const res = await api.launchTemplate(launchTpl.id, params);
            sdk.toast.show({
                level: 'success',
                message: t(`已启动: ${res.run_id}`, `Launched: ${res.run_id}`),
            });
            setLaunchTpl(null);
            window.location.hash = `#/runs/${res.run_id}`;
        } catch (e) {
            console.error('[cowork-web] template launch failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('启动失败 — 检查必填参数', 'Launch failed — check required params'),
            });
        } finally {
            setLaunching(false);
        }
    }

    // issue #261: 导入模板 JSON 文件 → POST /api/templates/import → 刷新列表
    async function onImportFile(file: File) {
        if (importing) return;
        setImporting(true);
        try {
            const body = JSON.parse(await file.text());
            const res = await api.importTemplate(body);
            sdk.toast.show({
                level: 'success',
                message: t(`已导入模板: ${res.name}`, `Template imported: ${res.name}`),
            });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] template import failed', e);
            sdk.toast.show({
                level: 'error',
                message: t(
                    '导入失败 — 须为含 workflow.nodes 的 JSON',
                    'Import failed — JSON with workflow.nodes required'
                ),
            });
        } finally {
            setImporting(false);
        }
    }

    // issue #261: 导出模板 JSON (浏览器下载)
    async function exportTemplate(tpl: TemplateInfo) {
        try {
            const url = await api.templateExport(tpl.id);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${tpl.id}.json`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            console.error('[cowork-web] template export failed', e);
            sdk.toast.show({ level: 'error', message: t('导出失败', 'Export failed') });
        }
    }

    async function removeTemplate(tpl: TemplateInfo) {
        try {
            await api.deleteTemplate(tpl.id);
            sdk.toast.show({ level: 'success', message: t('已删除', 'Deleted') });
            retry.manualRetry();
        } catch (e) {
            console.error('[cowork-web] template delete failed', e);
            sdk.toast.show({
                level: 'error',
                message: t('删除失败 — 仅自定义模板可删除', 'Delete failed — custom templates only'),
            });
        }
    }

    function openInCanvas(tpl: TemplateInfo) {
        if (creating) return;
        setCreating(tpl.id);
        api.template(tpl.id)
            .then((detail) => {
                const body = (detail as unknown as { workflow?: unknown }).workflow ?? { nodes: [], edges: [] };
                return api.createWorkflow({ name: tpl.name, description: tpl.description ?? '', workflow: body });
            })
            .then((created) => {
                sdk.toast.show({
                    level: 'success',
                    message: t('已创建副本，请在画布中完善', 'Copy created — refine in canvas'),
                });
                window.location.hash = `#/canvas/${created.id}`;
            })
            .catch((e: Error) => {
                console.error('[cowork-web] template copy failed', e);
                sdk.toast.show({ level: 'error', message: t('创建副本失败', 'Failed to create copy') });
            })
            .finally(() => setCreating(''));
    }

    return (
        <div className="mx-auto max-w-4xl">
            <div className="flex items-center gap-2 pb-3">
                <select
                    className="rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                >
                    <option value="">{t('全部分类', 'All categories')}</option>
                    {categories.map((c) => (
                        <option key={c} value={c}>
                            {c}
                        </option>
                    ))}
                </select>
                <div className="text-xs text-text-muted">{templates.length}</div>
                <label className="ml-auto cursor-pointer rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted">
                    {importing ? t('导入中…', 'Importing…') : t('导入模板', 'Import JSON')}
                    <input
                        type="file"
                        accept=".json,application/json"
                        className="hidden"
                        disabled={importing}
                        onChange={(e) => {
                            const f = e.target.files?.[0];
                            if (f) void onImportFile(f);
                            e.target.value = '';
                        }}
                    />
                </label>
            </div>
            {templates.length === 0 ? (
                <EmptyState title={t('该分类暂无模板', 'No templates in this category')} />
            ) : (
                <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                    {templates.map((tpl) => (
                        <div key={tpl.id} className="flex flex-col rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="flex items-center gap-2">
                                <span className="text-sm font-medium text-text-primary">{tpl.name}</span>
                                {tpl.category ? (
                                    <span className="ml-auto rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                        {tpl.category}
                                    </span>
                                ) : null}
                            </div>
                            {tpl.description ? (
                                <div className="mt-1 text-xs text-text-secondary">{tpl.description}</div>
                            ) : null}
                            {tpl.tags && tpl.tags.length > 0 ? (
                                <div className="mt-1 flex flex-wrap gap-1">
                                    {tpl.tags.map((tag) => (
                                        <span
                                            key={tag}
                                            className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-muted"
                                        >
                                            #{tag}
                                        </span>
                                    ))}
                                </div>
                            ) : null}
                            {launchTpl?.id === tpl.id ? (
                                <div className="mt-2 space-y-2 rounded-md border border-border bg-bg p-2">
                                    <div className="flex items-center gap-2 text-xs">
                                        <button
                                            type="button"
                                            className={`rounded-md px-2 py-1 ${
                                                intakeMode
                                                    ? 'bg-bg-muted font-medium text-text-primary'
                                                    : 'text-text-secondary hover:bg-bg-muted'
                                            }`}
                                            onClick={() => setIntakeMode(true)}
                                        >
                                            {t('需求录入', 'Requirement intake')}
                                        </button>
                                        <button
                                            type="button"
                                            className={`rounded-md px-2 py-1 ${
                                                !intakeMode
                                                    ? 'bg-bg-muted font-medium text-text-primary'
                                                    : 'text-text-secondary hover:bg-bg-muted'
                                            }`}
                                            onClick={() => setIntakeMode(false)}
                                        >
                                            {t('高级 (显式参数)', 'Advanced (explicit params)')}
                                        </button>
                                    </div>
                                    {intakeMode ? (
                                        <>
                                            <label className="block text-xs text-text-secondary">
                                                <span>
                                                    {t(
                                                        '需求描述 (粘贴一段需求文本或文档内容)',
                                                        'Requirement (paste requirement text or document content)'
                                                    )}
                                                    {' *'}
                                                </span>
                                                <textarea
                                                    className="mt-0.5 min-h-24 w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                                                    value={intakeRequirement}
                                                    placeholder={t(
                                                        '例: 做一个宠物寄养预约小程序, 支持门店浏览、日期选择和微信通知…',
                                                        'e.g. Build a pet-boarding booking app with store browsing, date pick and notifications…'
                                                    )}
                                                    onChange={(e) => {
                                                        setIntakeRequirement(e.target.value);
                                                        setIntakePreview(null);
                                                    }}
                                                    onBlur={() => void previewIntake()}
                                                />
                                            </label>
                                            <label className="block text-xs text-text-secondary">
                                                <span>
                                                    {t('slug 提示 (可空, 高级)', 'Slug hint (optional, advanced)')}
                                                </span>
                                                <input
                                                    className="mt-0.5 w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                                                    value={intakeSlugHint}
                                                    placeholder={t('留空 = 自动派生', 'empty = auto-derived')}
                                                    onChange={(e) => {
                                                        setIntakeSlugHint(e.target.value);
                                                        setIntakePreview(null);
                                                    }}
                                                />
                                            </label>
                                            <div className="text-xs text-text-secondary">
                                                <span>{t('架构评审 (闸门 B)', 'Architecture review (gate B)')}</span>
                                                <div className="mt-0.5 flex gap-3">
                                                    <label className="flex items-center gap-1">
                                                        <input
                                                            type="radio"
                                                            name="intake-review-arch"
                                                            checked={!intakeReviewArch}
                                                            onChange={() => setIntakeReviewArch(false)}
                                                        />
                                                        {t('不评审 (默认, 自动通过)', 'No review (default, auto-pass)')}
                                                    </label>
                                                    <label className="flex items-center gap-1">
                                                        <input
                                                            type="radio"
                                                            name="intake-review-arch"
                                                            checked={intakeReviewArch}
                                                            onChange={() => setIntakeReviewArch(true)}
                                                        />
                                                        {t('需要评审', 'Review required')}
                                                    </label>
                                                </div>
                                            </div>
                                            {intakePreview ? (
                                                <div className="rounded-md bg-bg-muted px-2 py-1 text-xs text-text-secondary">
                                                    {intakePreview.suggested_from
                                                        ? t(
                                                              `slug 冲突 — 建议使用 ${intakePreview.slug}`,
                                                              `slug conflict — suggesting ${intakePreview.slug}`
                                                          )
                                                        : t('slug 预览', 'slug preview')}
                                                    :{' '}
                                                    <span className="font-medium text-text-primary">
                                                        {intakePreview.slug}
                                                    </span>
                                                    {' · '}
                                                    {intakePreview.repo} (private)
                                                </div>
                                            ) : null}
                                            <div className="flex justify-end gap-1">
                                                <button
                                                    type="button"
                                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                                    onClick={() => setLaunchTpl(null)}
                                                >
                                                    {t('取消', 'Cancel')}
                                                </button>
                                                <button
                                                    type="button"
                                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                                    disabled={launching || intakeRequirement.trim().length < 10}
                                                    onClick={() => void submitLaunchIntake()}
                                                >
                                                    {launching
                                                        ? t('启动中…', 'Launching…')
                                                        : t('启动运行', 'Launch run')}
                                                </button>
                                            </div>
                                        </>
                                    ) : (
                                        <>
                                            {Object.entries(launchProps).map(([key, spec]) => (
                                                <label key={key} className="block text-xs text-text-secondary">
                                                    <span>
                                                        {key}
                                                        {launchRequired.includes(key) ? ' *' : ''}
                                                        {spec.description ? ` — ${spec.description}` : ''}
                                                    </span>
                                                    <input
                                                        className="mt-0.5 w-full rounded-md border border-border bg-bg px-2 py-1 text-sm text-text-primary"
                                                        value={launchValues[key] ?? ''}
                                                        placeholder={
                                                            spec.type === 'array'
                                                                ? t('逗号分隔', 'comma-separated')
                                                                : String(spec.type)
                                                        }
                                                        onChange={(e) =>
                                                            setLaunchValues((v) => ({ ...v, [key]: e.target.value }))
                                                        }
                                                    />
                                                </label>
                                            ))}
                                            <div className="flex justify-end gap-1">
                                                <button
                                                    type="button"
                                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                                    onClick={() => setLaunchTpl(null)}
                                                >
                                                    {t('取消', 'Cancel')}
                                                </button>
                                                <button
                                                    type="button"
                                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                                    disabled={launching}
                                                    onClick={() => void submitLaunch()}
                                                >
                                                    {launching
                                                        ? t('启动中…', 'Launching…')
                                                        : t('启动运行', 'Launch run')}
                                                </button>
                                            </div>
                                        </>
                                    )}
                                </div>
                            ) : null}
                            <div className="mt-auto flex justify-end gap-1 pt-2">
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => void openLaunch(tpl)}
                                >
                                    {t('启动', 'Launch')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted"
                                    onClick={() => void exportTemplate(tpl)}
                                >
                                    {t('导出', 'Export')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-error hover:bg-error/10"
                                    onClick={() => void removeTemplate(tpl)}
                                >
                                    {t('删除', 'Delete')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-xs text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                    disabled={creating === tpl.id}
                                    onClick={() => openInCanvas(tpl)}
                                >
                                    {creating === tpl.id
                                        ? t('创建中…', 'Creating…')
                                        : t('用画布打开', 'Open in canvas')}
                                </button>
                            </div>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
