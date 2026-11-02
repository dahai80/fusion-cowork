import { EmptyState, ErrorState, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { type SkillPackageInfo, type SkillRunResult, api } from '../api';
import { useT } from '../i18n';

// issue #155: markdown skill 包 — 列表 + 参数表单 (由 frontmatter params 渲染) + 执行结果。
// issue #255: 用户目录包 (~/.fusion-cowork/skills) 新建/编辑/删除; 内置包只读。

interface EditorState {
    markdown: string;
    name: string | null;
}

export function SkillsPage() {
    const t = useT();
    const retry = useAutoRetry(() => api.skillPackages(), []);
    const [selected, setSelected] = useState<SkillPackageInfo | null>(null);
    const [values, setValues] = useState<Record<string, unknown>>({});
    const [result, setResult] = useState<SkillRunResult | null>(null);
    const [running, setRunning] = useState(false);
    const [editor, setEditor] = useState<EditorState | null>(null);
    const [editorError, setEditorError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);

    if (retry.loading && !retry.data) return <SkeletonLines rows={6} />;
    if (retry.error || !retry.data)
        return <ErrorState message={retry.error?.message ?? 'no data'} onRetry={retry.manualRetry} />;
    const packages = retry.data.skills;

    function open(pkg: SkillPackageInfo) {
        setSelected(pkg);
        setResult(null);
        const initial: Record<string, unknown> = {};
        for (const p of pkg.params) initial[p.name] = p.default ?? '';
        setValues(initial);
    }

    async function run() {
        if (!selected) return;
        setRunning(true);
        setResult(null);
        try {
            setResult(await api.runSkillPackage(selected.name, values));
        } catch (e) {
            console.error('[cowork-web] skill run failed', e);
            setResult({ error: e instanceof Error ? e.message : String(e) });
        } finally {
            setRunning(false);
        }
    }

    async function editPkg(pkg: SkillPackageInfo) {
        setEditorError(null);
        try {
            const src = await api.skillSource(pkg.name);
            setEditor({ markdown: src.markdown || '', name: pkg.name });
        } catch (e) {
            setEditorError(e instanceof Error ? e.message : String(e));
        }
    }

    async function saveEditor() {
        if (!editor) return;
        setSaving(true);
        setEditorError(null);
        try {
            const saved = await api.saveSkillPackage(editor.markdown);
            if (saved.problems?.length) {
                setEditorError(saved.problems.join(' / '));
            } else {
                setEditor(null);
                retry.manualRetry();
            }
        } catch (e) {
            setEditorError(e instanceof Error ? e.message : String(e));
        } finally {
            setSaving(false);
        }
    }

    async function removePkg(pkg: SkillPackageInfo) {
        if (!window.confirm(t(`删除技能 ${pkg.name}?`, `Delete skill ${pkg.name}?`))) return;
        setEditorError(null);
        try {
            await api.deleteSkillPackage(pkg.name);
            retry.manualRetry();
        } catch (e) {
            setEditorError(e instanceof Error ? e.message : String(e));
        }
    }

    if (editor) {
        return (
            <div className="mx-auto max-w-3xl space-y-3">
                <div className="flex items-center justify-between">
                    <div className="text-sm font-medium text-text-primary">
                        {editor.name === null
                            ? t('新建技能', 'New skill')
                            : t(`编辑 ${editor.name}`, `Edit ${editor.name}`)}
                    </div>
                    <button
                        type="button"
                        className="text-xs text-text-secondary hover:underline"
                        onClick={() => setEditor(null)}
                    >
                        {t('返回技能列表', 'Back to skills')}
                    </button>
                </div>
                <div className="text-xs text-text-muted">
                    {t(
                        'markdown + frontmatter (name/title/description/params 等), 保存到 ~/.fusion-cowork/skills 后立即生效',
                        'Markdown with frontmatter (name/title/description/params); saved to ~/.fusion-cowork/skills and effective immediately'
                    )}
                </div>
                <textarea
                    aria-label={t('技能包内容', 'Skill pack source')}
                    className="h-96 w-full rounded-lg border border-border bg-bg-elevated p-3 font-mono text-xs text-text-primary"
                    spellCheck={false}
                    value={editor.markdown}
                    onChange={(e) => setEditor({ ...editor, markdown: e.target.value })}
                />
                {editorError ? <div className="text-xs text-error">{editorError}</div> : null}
                <div className="flex gap-2">
                    <button
                        type="button"
                        className="rounded-md bg-bg-elevated px-3 py-1.5 text-xs text-text-primary shadow-sm border border-border hover:bg-bg-muted"
                        disabled={saving || !editor.markdown.trim()}
                        onClick={() => void saveEditor()}
                    >
                        {saving ? t('保存中…', 'Saving…') : t('保存', 'Save')}
                    </button>
                    <button
                        type="button"
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted"
                        onClick={() => setEditor(null)}
                    >
                        {t('取消', 'Cancel')}
                    </button>
                </div>
            </div>
        );
    }

    if (selected) {
        return (
            <div className="mx-auto max-w-2xl space-y-3">
                <button
                    type="button"
                    className="text-xs text-text-secondary hover:underline"
                    onClick={() => setSelected(null)}
                >
                    {t('返回技能列表', 'Back to skills')}
                </button>
                <div className="text-sm font-medium text-text-primary">
                    {sdkText(selected.title, selected.title_en, t)}
                </div>
                <div className="text-xs text-text-muted">
                    {sdkText(selected.description, selected.description_en, t)}
                </div>
                {selected.params.map((p) => (
                    <div key={p.name} className="block">
                        <span className="text-xs text-text-secondary">
                            {sdkText(p.label, p.label_en, t)}
                            {p.required ? ' *' : ''}
                        </span>
                        {p.type === 'bool' ? (
                            <input
                                type="checkbox"
                                aria-label={p.name}
                                className="ml-2 align-middle"
                                checked={Boolean(values[p.name])}
                                onChange={(e) => setValues({ ...values, [p.name]: e.target.checked })}
                            />
                        ) : p.options.length > 0 ? (
                            <select
                                aria-label={p.name}
                                className="mt-1 w-full rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs text-text-primary"
                                value={String(values[p.name] ?? '')}
                                onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
                            >
                                {p.options.map((o) => (
                                    <option key={o} value={o}>
                                        {o}
                                    </option>
                                ))}
                            </select>
                        ) : (
                            <input
                                aria-label={p.name}
                                type={p.type === 'int' || p.type === 'float' ? 'number' : 'text'}
                                className="mt-1 w-full rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-xs text-text-primary"
                                placeholder={p.placeholder}
                                value={String(values[p.name] ?? '')}
                                onChange={(e) => setValues({ ...values, [p.name]: e.target.value })}
                            />
                        )}
                    </div>
                ))}
                <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted"
                    disabled={running}
                    onClick={() => void run()}
                >
                    {running ? t('执行中…', 'Running…') : t('执行', 'Run')}
                </button>
                {result ? (
                    result.error ? (
                        <ErrorState
                            message={[result.error, ...(result.errors ?? [])].join(' / ')}
                            onRetry={() => void run()}
                        />
                    ) : (
                        <div className="space-y-1 rounded-lg border border-border bg-bg-elevated p-3">
                            <div className="text-xs text-text-primary">{result.summary ?? 'done'}</div>
                            <pre className="max-h-60 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] text-text-muted">
                                {JSON.stringify(result.data ?? {}, null, 2)}
                            </pre>
                        </div>
                    )
                ) : null}
            </div>
        );
    }

    return (
        <div className="mx-auto max-w-2xl space-y-2">
            <div className="flex items-center justify-between">
                <div className="text-sm font-medium text-text-primary">
                    {t('技能', 'Skills')} ({packages.length})
                </div>
                <button
                    type="button"
                    className="rounded-md bg-bg-elevated px-3 py-1.5 text-xs text-text-primary shadow-sm border border-border hover:bg-bg-muted"
                    onClick={() => {
                        setEditorError(null);
                        setEditor({
                            markdown:
                                '---\nname: /my-skill\ntitle: \ndescription: \nversion: "1.0"\ncategory: custom\n---\n\n',
                            name: null,
                        });
                    }}
                >
                    ＋ {t('新建技能', 'New skill')}
                </button>
            </div>
            {editorError ? <div className="text-xs text-error">{editorError}</div> : null}
            {packages.length === 0 ? (
                <EmptyState title={t('暂无技能', 'No skills')} />
            ) : (
                packages.map((pkg) => (
                    <div
                        key={pkg.name}
                        className="flex w-full items-stretch rounded-lg border border-border bg-bg-elevated p-3"
                    >
                        <button
                            type="button"
                            className="min-w-0 flex-1 text-left hover:opacity-80"
                            onClick={() => open(pkg)}
                        >
                            <div className="flex items-center justify-between">
                                <span className="font-mono text-sm text-text-primary">{pkg.name}</span>
                                <span className="rounded bg-bg-muted px-1.5 py-0.5 text-[10px] text-text-secondary">
                                    {pkg.category}
                                </span>
                            </div>
                            <div className="pt-1 text-xs text-text-muted">
                                {sdkText(pkg.description, pkg.description_en, t)}
                            </div>
                        </button>
                        {pkg.editable ? (
                            <div className="ml-2 flex shrink-0 items-center gap-1">
                                <button
                                    type="button"
                                    className="rounded-md border border-border px-2 py-1 text-[10px] text-text-secondary hover:bg-bg-muted"
                                    onClick={() => void editPkg(pkg)}
                                >
                                    {t('编辑', 'Edit')}
                                </button>
                                <button
                                    type="button"
                                    className="rounded-md border border-error/40 px-2 py-1 text-[10px] text-error hover:bg-bg-muted"
                                    onClick={() => void removePkg(pkg)}
                                >
                                    {t('删除', 'Delete')}
                                </button>
                            </div>
                        ) : null}
                    </div>
                ))
            )}
        </div>
    );
}

function sdkText(zh: string, en: string, t: (zh: string, en: string) => string): string {
    return zh ? t(zh, en || zh) : en;
}
